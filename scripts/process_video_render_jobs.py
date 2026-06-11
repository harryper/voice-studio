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
UPLOAD_SCRIPT = SKILL_DIR / "scripts" / "upload_to_cos.py"
IMAGE_GEN_SCRIPT = SKILL_DIR / "scripts" / "minimax_image_gen.py"
PEXELS_IMAGE_SCRIPT = SKILL_DIR / "scripts" / "pexels_image.py"

# 2026-06-11: 16:9 horizontal (1920x1080) for landscape video
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080

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
    """Upload to COS and return the pre-signed URL."""
    filename = f"video-{slug}-{short_id}-{kind}.mp4"
    object_key = f"{datetime.now().strftime('%Y-%m-%d')}/video-studio/{filename}"
    cmd = [
        "python3", str(UPLOAD_SCRIPT),
        "--file", str(local_path),
        "--key", object_key,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"upload failed: {(result.stderr or result.stdout)[:500]}")
    return result.stdout.strip()


def render_placeholder(job_id, render_dir, script_text="", theme=""):
    """Generate images + HTML composition for 抖音-style video (P3 v1).

    Splits the script into 10 chunks, then for each chunk:
    1. Try Pexels stock photos (real, fast)
    2. Fall back to MiniMax image generation (AI, slower)
    3. Final fallback: gradient placeholder

    Builds an HTML composition with:
    - Image background per scene
    - Ken Burns effect (slow zoom + pan)
    - Subtitle overlay at bottom
    - Total: 30s (10 scenes x 3s)
    """
    render_dir.mkdir(parents=True, exist_ok=True)
    html_path = render_dir / "index.html"

    # 1. Generate scene images (Pexels primary, MiniMax fallback, gradient last)
    images_dir = render_dir / "images"
    images_dir.mkdir(exist_ok=True)
    # Smart scene count: ~30 chars per scene, capped 6-10
    n_scenes = min(10, max(6, len(script_text) // 30)) if script_text else 6
    chunks = split_script_to_cards(script_text, n_cards=n_scenes)
    log(f"  using {n_scenes} scenes for {len(script_text)} chars")
    log(f"  generating {len(chunks)} scene images (Pexels → MiniMax → gradient)...")
    image_paths = []
    for i, chunk in enumerate(chunks):
        img_path = images_dir / f"scene_{i+1}.jpg"
        if img_path.exists() and img_path.stat().st_size > 5000:
            log(f"  scene {i+1}: cached")
            image_paths.append(img_path)
            continue
        # 1. Try Pexels
        query = extract_pexels_query(chunk, theme, i)
        if try_pexels_image(query, img_path):
            log(f"  scene {i+1}: pexels (q={query!r})")
            image_paths.append(img_path)
            continue
        # 2. Fall back to MiniMax
        log(f"  scene {i+1}: pexels miss, trying MiniMax...")
        prompt = build_visual_prompt(chunk, theme, scene_index=i, total=len(chunks))
        if try_minimax_image(prompt, img_path):
            log(f"  scene {i+1}: minimax")
            image_paths.append(img_path)
            continue
        # 3. Gradient fallback
        log(f"  scene {i+1}: both miss, using gradient")
        create_fallback_image(img_path, scene_index=i, total=len(chunks))
        image_paths.append(img_path)

    # 2. Build HTML with images + Ken Burns + subtitles
    html = build_image_composition_html(image_paths, chunks, total_duration=30)
    # build_image_composition_html uses its own n_scenes from image_paths
    html_path.write_text(html, encoding="utf-8")
    log(f"  generated HTML with {len(chunks)} scenes ({len(script_text)} chars)")

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


def try_pexels_image(query, out_path, timeout=30):
    """Try to fetch a Pexels image. Returns True on success."""
    try:
        result = subprocess.run(
            ["python3", str(PEXELS_IMAGE_SCRIPT),
             "--query", query,
             "--out", str(out_path),
             "--w", str(DEFAULT_WIDTH), "--h", str(DEFAULT_HEIGHT),
             "--per-page", "3"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000:
            return True
        return False
    except Exception:
        return False


def try_minimax_image(prompt, out_path, timeout=120):
    """Try to generate a MiniMax image. Returns True on success."""
    try:
        result = subprocess.run(
            ["python3", str(IMAGE_GEN_SCRIPT),
             "--prompt", prompt,
             "--aspect", "16:9",
             "--n", "1",
             "--out", str(out_path)],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000
    except Exception:
        return False


def extract_pexels_query(chunk_text, theme, scene_index):
    """Extract a Pexels search query from a script chunk.

    Strategy: use theme as the primary anchor (consistent visual style),
    enrich with chunk keywords for variety, add per-scene hint.
    Pexels handles Chinese queries (老人 → 2500+ results).
    """
    import re as _re
    text = _re.sub(r"[，。！？、,.!?\s\d]+", " ", chunk_text).strip()
    cn_chars = [c for c in text if '\u4e00' <= c <= '\u9fff']
    # Per-scene variation hints (different angles/subjects to avoid all-same-photo)
    scene_hints = [
        "portrait", "close-up", "wide", "detail", "outdoor",
        "candid", "interior", "still life", "landscape", "urban",
    ]
    hint = scene_hints[scene_index % len(scene_hints)]

    if cn_chars:
        # Take 2-4 chinese chars as the core subject
        cn_part = "".join(cn_chars[:4])
        # Mix subject + theme + hint
        parts = [cn_part]
        if theme:
            theme_chars = [c for c in theme if '\u4e00' <= c <= '\u9fff']
            if theme_chars:
                theme_short = "".join(theme_chars[:3])
                if theme_short != cn_part:
                    parts.append(theme_short)
        parts.append(hint)
        return " ".join(parts)[:35]
    # No Chinese in chunk: use theme + hint
    if theme:
        return f"{theme} {hint}"[:35]
    return hint


def build_visual_prompt(chunk_text, theme, scene_index, total):
    """Build a visual prompt for MiniMax image generation.

    Strategy: use theme as global style anchor, chunk as scene subject.
    Add per-scene visual variation (wide / close-up / object / person)
    so consecutive scenes don't look identical.
    """
    # Style: cinematic 抖音-quality, vertical
    style = (
        "cinematic photography, professional quality, "
        "warm natural lighting, shallow depth of field, "
        "35mm film grain, 9:16 vertical composition"
    )

    # Per-scene shot variation
    shot_variations = [
        "wide establishing shot",
        "medium shot with context",
        "close-up detail shot",
        "extreme close-up texture",
        "wide shot from a different angle",
        "over-the-shoulder perspective",
        "low angle dramatic shot",
        "portrait framing centered",
        "object still life composition",
        "wide cinematic vista",
    ]
    shot = shot_variations[scene_index % len(shot_variations)]

    # Clean up chunk for prompt use
    subject = chunk_text.strip()[:60].rstrip("。，！？,.!? ")
    if not subject:
        subject = theme[:30] if theme else "atmospheric scene"

    # Translate/keep chinese: MiniMax image gen supports mixed
    prompt = f"{shot}, {subject}, {style}"
    # Cap prompt length (API limit ~2000 chars)
    if len(prompt) > 500:
        prompt = prompt[:500]
    return prompt


def create_fallback_image(out_path, scene_index, total):
    """Generate a gradient PNG as fallback when image gen fails."""
    try:
        from PIL import Image, ImageDraw
        palette = [
            (30, 58, 138), (124, 58, 237), (236, 72, 153),
            (245, 158, 11), (16, 185, 129), (14, 165, 233),
            (99, 102, 241), (220, 38, 38), (217, 119, 6),
            (5, 150, 105),
        ]
        c1, c2 = palette[scene_index % len(palette)], palette[(scene_index + 1) % len(palette)]
        img = Image.new("RGB", (720, 1280), c1)
        draw = ImageDraw.Draw(img)
        for y in range(1280):
            t = y / 1280
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
            draw.line([(0, y), (720, y)], fill=(r, g, b))
        img.save(out_path, "JPEG", quality=85)
    except ImportError:
        # PIL not available: just write empty
        out_path.write_bytes(b"")


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


def build_image_composition_html(image_paths, chunks, total_duration=30):
    """Build hyperframes composition HTML with image backgrounds + Ken Burns + subtitles.

    Each scene:
    - image_path: background image (1080x1920)
    - chunk: caption text
    - per-scene duration: total_duration / n_scenes
    - Ken Burns: slow zoom in (scale 1.0 → 1.12) + slight pan
    - Subtitle: large text at bottom with dark gradient overlay
    """
    n = len(image_paths)
    per = total_duration / n
    # Ken Burns direction varies per scene (avoids all-same motion)
    kb_variants = [
        {"scale": 1.12, "x": -20, "y": -10},   # 0: zoom + left-up
        {"scale": 1.15, "x": 20, "y": 0},       # 1: zoom + right
        {"scale": 1.10, "x": 0, "y": -20},      # 2: zoom + up
        {"scale": 1.13, "x": -15, "y": 10},     # 3: zoom + left-down
        {"scale": 1.18, "x": 10, "y": -10},     # 4: zoom + right-up
        {"scale": 1.12, "x": -25, "y": 0},      # 5: zoom + left
        {"scale": 1.15, "x": 15, "y": 15},      # 6: zoom + right-down
        {"scale": 1.10, "x": 0, "y": 20},       # 7: zoom + down
        {"scale": 1.13, "x": -10, "y": -15},    # 8: zoom + left-up2
        {"scale": 1.16, "x": 20, "y": -5},      # 9: zoom + right-up
    ]

    scenes_html = []
    timeline_tweens = []
    for i, (img_path, chunk) in enumerate(zip(image_paths, chunks)):
        start = i * per
        img_filename = Path(img_path).name  # just the basename
        kb = kb_variants[i % len(kb_variants)]
        # Wrap caption into lines (max ~10 chars per line for 抖音-style)
        lines = wrap_caption_lines(chunk, max_chars=10, max_lines=3)
        caption_html = "".join(f'<div class="cap-line">{escape_html(line)}</div>' for line in lines)
        scenes_html.append(
            f'    <div id="scene-{i+1}" class="clip" data-track-index="0" '
            f'data-start="{start}" data-duration="{per}">\n'
            f'      <div class="bg" id="bg-{i+1}" style="background-image: url(\'images/{img_filename}\');"></div>\n'
            f'      <div class="subtitle" id="sub-{i+1}">{caption_html}</div>\n'
            f'    </div>'
        )
        # Ken Burns: scale + x/y pan over the scene duration
        timeline_tweens.append(
            f"tl.to('#bg-{i+1}', {{ scale: {kb['scale']}, x: {kb['x']}, y: {kb['y']}, "
            f"ease: 'none', duration: {per} }}, {start});"
        )
        # Subtitle fade in/out (fast, 0.2s in, hold, 0.3s out)
        timeline_tweens.append(
            f"tl.fromTo('#sub-{i+1}', {{ opacity: 0 }}, {{ opacity: 1, duration: 0.2 }}, {start});"
        )
        if i < n - 1:
            timeline_tweens.append(
                f"tl.to('#sub-{i+1}', {{ opacity: 0, duration: 0.3 }}, {start + per - 0.3});"
            )

    scenes_str = "\n".join(scenes_html)
    tweens_str = "\n    ".join(timeline_tweens)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>video composition</title>
  <style>
    [data-composition-id="dynamic"] {{
      width: {DEFAULT_WIDTH}px; height: {DEFAULT_HEIGHT}px; background: #0a0e1a; color: #fff;
      font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
      overflow: hidden;
    }}
    .clip {{
      width: 100%; height: 100%; position: relative; overflow: hidden;
    }}
    .bg {{
      position: absolute; inset: 0;
      background-size: cover; background-position: center;
      transform: scale(1.0) translate(0, 0);
      will-change: transform;
    }}
    .subtitle {{
      position: absolute; left: 0; right: 0; bottom: 0;
      padding: 40px 80px 60px 80px;
      background: linear-gradient(180deg, transparent 0%, rgba(0,0,0,0.4) 30%, rgba(0,0,0,0.85) 100%);
      display: flex; flex-direction: column; align-items: center; gap: 8px;
      opacity: 0;
    }}
    .cap-line {{
      font-size: 64px; font-weight: bold; line-height: 1.3; text-align: center;
      letter-spacing: 2px;
      text-shadow: 0 4px 20px rgba(0,0,0,0.9), 0 0 8px rgba(0,0,0,0.6);
    }}
  </style>
</head>
<body>
  <div data-composition-id="dynamic"
       data-width="{DEFAULT_WIDTH}" data-height="{DEFAULT_HEIGHT}"
       data-start="0" data-duration="{total_duration}">
{scenes_str}
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


def wrap_caption_lines(text, max_chars=10, max_lines=3):
    """Wrap caption text for 抖音-style subtitles (fewer chars, more lines visible).

    Chunks are 1-3 sentences, so we want them split to fit 2-3 lines.
    """
    if not text:
        return [""]
    text = text.strip().rstrip("。！？.!?")
    chars = list(text)
    if len(chars) <= max_chars:
        return [text]
    # Try to split at sentence boundary if short enough
    import re as _re
    parts = _re.split(r'(?<=[。！？,，;；])', text)
    if len(parts) > 1 and all(len(p) <= max_chars * 2 for p in parts if p.strip()):
        # Already split into natural phrases
        result = [p.strip() for p in parts if p.strip()][:max_lines]
        if len(result) <= max_lines:
            return result
    # Fallback: char-based wrapping
    lines = []
    for i in range(0, len(chars), max_chars):
        lines.append("".join(chars[i:i + max_chars]))
        if len(lines) >= max_lines:
            break
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
