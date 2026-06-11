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


def render_placeholder(job_id, render_dir):
    """Use the static placeholder template for v1."""
    render_dir.mkdir(parents=True, exist_ok=True)
    html_path = render_dir / "index.html"
    shutil.copy(PLACEHOLDER_HTML, html_path)
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
        out_mp4 = render_placeholder(job_id, render_dir)
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
