#!/usr/bin/env python3
"""Host-side render writer for video-studio jobs (mode='video').

Mirrors process_pending_voice_jobs.py structure, but:
- Listens on .video-render-trigger
- Reads jobs/video/ for ready_script jobs
- Renders hyperframes HTML composition to mp4
- Uploads to R2
- On success: status -> rendered, touches .video-narrate-trigger

v1 uses the static placeholder template (templates/video_placeholder.html).
P2 will replace the placeholder with LLM-generated HTML.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
JOBS_DIR = SKILL_DIR / "jobs" / "video"
VIDEO_RUNS_DIR = Path("/root/.openclaw/workspace/skills/video-studio/runs")
PLACEHOLDER_HTML = SKILL_DIR / "templates" / "video_placeholder.html"
VIDEO_STYLE_HELPER = Path("/root/.openclaw/workspace/skills/video-studio/reference-style-video.md")
UPLOAD_SCRIPT = SKILL_DIR / "scripts" / "upload_to_oss.py"

LOCK_PATH = SKILL_DIR / ".video-render-writer.lock"
RENDER_TRIGGER = SKILL_DIR / ".video-render-trigger"
NARRATE_TRIGGER = SKILL_DIR / ".video-narrate-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".video-render-writer.lastrun"
LOG_FILE = Path("/var/log/voice-studio/video-render-watcher.log")

# Render can be slow on this VM (~5min for 30s video). 10 min is safe for 60s+ placeholders.
RENDER_TIMEOUT_SEC = 600
# 30fps is hyperframes default; do not lower for v1.
FPS = 30


def log(msg):
    line = f"[video-render-writer] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def load_job(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_job(job):
    job["updated_at"] = now_iso()
    tmp = job_path(job["id"]).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, job_path(job["id"]))


def pending_jobs():
    jobs = []
    if not JOBS_DIR.exists():
        return jobs
    for path in JOBS_DIR.glob("v_*.json"):
        try:
            job = load_job(path)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("mode") == "video" and job.get("status") == "ready_script":
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("updated_at", ""))


def safe_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower())[:30].strip("-")


def upload_mp4(local_path, slug, short_id, kind):
    """Upload via upload_to_oss.py. Returns the pre-signed URL."""
    cmd = [
        "python3", str(UPLOAD_SCRIPT),
        "--file", str(local_path),
        "--theme", "video-studio",
        "--name", f"video-{slug}-{short_id}-{kind}.mp4",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"upload failed: {(result.stderr or result.stdout)[:500]}")
    return result.stdout.strip()


def render_placeholder(job_id, render_dir, script_text=""):
    """Generate a simple HTML composition from the script text (P2 v1).

    Splits the script into ~5 chunks, each shown on its own card with fade
    transitions. Total composition length is 30s (5 cards x 6s).
    P2.5+ will replace this with LLM-generated compositions.
    """
    render_dir.mkdir(parents=True, exist_ok=True)
    html_path = render_dir / "index.html"

    # Build the HTML dynamically
    chunks = split_script_to_cards(script_text, n_cards=5)
    html = build_card_composition_html(chunks, total_duration=30)
    html_path.write_text(html, encoding="utf-8")
    log(f"  generated HTML with {len(chunks)} cards ({len(script_text)} chars)")

    out_mp4 = render_dir / "video-only.mp4"

    for cmd in [
        ["npx", "--yes", "hyperframes@0.6.89", "lint"],
        ["npx", "--yes", "hyperframes@0.6.89", "validate"],
    ]:
        result = subprocess.run(
            cmd, cwd=render_dir, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {(result.stderr or result.stdout)[-500:]}")

    result = subprocess.run(
        ["npx", "--yes", "hyperframes@0.6.89", "render", "--output", str(out_mp4)],
        cwd=render_dir, capture_output=True, text=True,
        timeout=RENDER_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hyperframes render failed: {(result.stderr or result.stdout)[-1000:]}")
    if not out_mp4.exists():
        raise RuntimeError("render exit 0 but video-only.mp4 missing")
    return out_mp4


def split_script_to_cards(script_text, n_cards=5):
    """Split script into ~n_cards chunks by sentence boundaries.

    Tries to keep chunks roughly balanced in length.
    """
    if not script_text:
        return [f"Card {i+1}" for i in range(n_cards)]

    # Split by Chinese sentence delimiters (。！？)
    import re as _re
    sentences = [s.strip() for s in _re.split(r'(?<=[。！？])', script_text) if s.strip()]
    if not sentences:
        sentences = [script_text]

    # If we have many sentences, group them into n_cards
    if len(sentences) <= n_cards:
        # Pad with empty
        return sentences + [""] * (n_cards - len(sentences))

    # Roughly equal distribution
    per = len(sentences) / n_cards
    chunks = []
    for i in range(n_cards):
        start = int(i * per)
        end = int((i + 1) * per) if i < n_cards - 1 else len(sentences)
        chunks.append("".join(sentences[start:end]))
    return chunks


def build_card_composition_html(chunks, total_duration=30):
    """Build hyperframes composition HTML for the chunks.

    Each chunk gets total_duration/n_cards seconds, with a fade transition.
    """
    n = len(chunks)
    per = total_duration / n
    palette = [
        ("#1e3a8a", "#7c3aed"),  # 1: blue → purple
        ("#7c3aed", "#ec4899"),  # 2: purple → pink
        ("#ec4899", "#f59e0b"),  # 3: pink → amber
        ("#f59e0b", "#10b981"),  # 4: amber → emerald
        ("#10b981", "#1e3a8a"),  # 5: emerald → blue
        ("#0ea5e9", "#6366f1"),
        ("#dc2626", "#f59e0b"),
    ]

    cards_html = []
    timeline_tweens = []
    for i, chunk in enumerate(chunks):
        start = i * per
        c1, c2 = palette[i % len(palette)]
        bg = f"linear-gradient(135deg, {c1} 0%, {c2} 100%)"
        # Wrap text by character (~13 per line for the 1080 width with padding)
        lines = wrap_text_to_lines(chunk, max_chars=13, max_lines=4)
        text_html = "".join(f'<div class="line">{escape_html(line)}</div>' for line in lines)
        cards_html.append(
            f'    <div id="card-{i+1}" class="clip" data-track-index="0" '
            f'data-start="{start}" data-duration="{per}" style="background:{bg};">\n'
            f'      {text_html}\n'
            f'    </div>'
        )
        # Fade in
        timeline_tweens.append(f"tl.to('#card-{i+1}', {{ opacity: 1, duration: 0.3 }}, {start});")
        # Fade out (except last card, which we just leave)
        if i < n - 1:
            timeline_tweens.append(f"tl.to('#card-{i+1}', {{ opacity: 0, duration: 0.3 }}, {start + per - 0.3});")

    cards_str = "\n".join(cards_html)
    tweens_str = "\n    ".join(timeline_tweens)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>video composition</title>
  <style>
    [data-composition-id="dynamic"] {{
      width: 1080px; height: 1920px; background: #0a0e1a; color: #fff;
      font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
      overflow: hidden;
    }}
    .clip {{
      width: 100%; height: 100%;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
      gap: 24px; padding: 100px 80px; box-sizing: border-box;
      opacity: 0;
    }}
    .line {{
      font-size: 80px; font-weight: bold; line-height: 1.3; text-align: center;
      letter-spacing: 4px;
    }}
  </style>
</head>
<body>
  <div data-composition-id="dynamic"
       data-width="1080" data-height="1920"
       data-start="0" data-duration="{total_duration}">
{cards_str}
  </div>

  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    {tweens_str}
    window.__timelines["dynamic"] = tl;
  </script>
</body>
</html>
"""


def wrap_text_to_lines(text, max_chars=13, max_lines=4):
    """Break Chinese text into lines of ~max_chars, max max_lines."""
    if not text:
        return [""]
    chars = list(text)
    lines = []
    for i in range(0, len(chars), max_chars):
        lines.append("".join(chars[i:i + max_chars]))
        if len(lines) >= max_lines:
            break
    # If there's leftover, append "..." to last line
    if len(chars) > max_lines * max_chars and lines:
        last = lines[-1]
        if len(last) >= max_chars - 1:
            lines[-1] = last[:-1] + "…"
        else:
            lines[-1] = last + "…"
    return lines


def escape_html(s):
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


def get_duration_sec(mp4_path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp4_path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def process_one(job):
    job_id = job["id"]
    theme = job.get("theme", "")
    log(f"rendering {job_id}: theme={theme!r}")

    job["status"] = "rendering"
    job["error"] = None
    job.setdefault("render", {})
    job["render"]["render_started_at"] = now_iso()
    save_job(job)

    run_dir = VIDEO_RUNS_DIR / job_id
    render_dir = run_dir / "composition"
    video_dir = run_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)

    try:
        out_mp4 = render_placeholder(job_id, render_dir, job.get("script", ""))
        final_raw = video_dir / "raw.mp4"
        shutil.move(str(out_mp4), str(final_raw))

        short_id = job_id.split("_")[-1] if "_" in job_id else job_id[-6:]
        slug = safe_slug(theme) or "untitled"
        r2_url = upload_mp4(final_raw, slug, short_id, "rendered")
        duration = get_duration_sec(final_raw)

        job["render"]["mp4_path"] = str(final_raw)
        job["render"]["mp4_url"] = r2_url
        job["render"]["render_completed_at"] = now_iso()
        job["status"] = "rendered"
        job.setdefault("logs", []).append(
            f"{now_iso()} render done ({duration:.1f}s, {final_raw.stat().st_size} bytes), uploaded"
        )
        save_job(job)
        log(f"{job_id} -> rendered, duration={duration:.1f}s")

        NARRATE_TRIGGER.touch()
        log(f"touched {NARRATE_TRIGGER.name}")
        return True

    except Exception as e:
        log(f"{job_id} RENDER FAILED: {e}")
        job["status"] = "error"
        job["error"] = f"render daemon: {type(e).__name__}: {e}"
        job.setdefault("logs", []).append(f"{now_iso()} RENDER FAILED: {e}")
        save_job(job)
        return False


def main():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another writer is running, skipping")
            return 0

        # Debounce
        if RENDER_TRIGGER.exists():
            deadline = time.time() + 12
            while time.time() < deadline:
                mtime = RENDER_TRIGGER.stat().st_mtime
                age = time.time() - mtime
                if age >= 3:
                    break
                time.sleep(min(3, max(0.2, 3 - age)))

        # Throttle (render is expensive; 60s gap between runs)
        if LAST_RUN_MARKER.exists():
            try:
                last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0
            gap = time.time() - last
            if gap < 60 and last:
                wait = 60 - gap
                log(f"throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
                time.sleep(wait)

        processed = 0
        for _ in range(1):  # max 1 per run
            jobs = pending_jobs()
            if not jobs:
                break
            process_one(jobs[0])
            processed += 1

        LAST_RUN_MARKER.write_text(f"{time.time()}\n", encoding="utf-8")
        log(f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
