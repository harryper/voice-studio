#!/usr/bin/env python3
"""Host-side writer for voice-studio theme jobs.

The Flask app runs in Docker and cannot reliably call `openclaw agent` because
the gateway and auth profiles live on the host. This script is run by a host
systemd path/service pair whenever the Web app creates a pending theme job.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
JOBS_DIR = SKILL_DIR / "jobs"
RUNS_DIR = SKILL_DIR / "runs"
LOCK_PATH = SKILL_DIR / ".writer.lock"
NODE = SKILL_DIR / "scripts" / "node"
OPENCLAW = Path("/usr/lib/node_modules/openclaw/openclaw.mjs")
MIN_SCRIPT_CHARS = 3300


def job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def load_job(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_job(job):
    job["updated_at"] = datetime.now().isoformat()
    tmp = job_path(job["id"]).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, job_path(job["id"]))


def pending_jobs():
    jobs = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = load_job(path)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("mode") == "theme" and job.get("status") in {"pending", "retry_pending"}:
            jobs.append(job)
    return sorted(jobs, key=lambda j: j.get("created_at", ""))


def build_prompt(job):
    job_id = job["id"]
    theme = job.get("theme") or ""
    return (
        f"为 voice-studio Web 项目写一篇老波宇宙科普旁白文稿。主题：{theme}\n\n"
        "先读并严格吸收这些文件：\n"
        "A. skills/voice-studio/SKILL.md 的 Web job script workflow 和 Script style\n"
        "B. skills/voice-studio/reference-style.md 的声音人格、开头钩子、结构模板、句法技巧、结尾方式、自检清单\n"
        "C. skills/voice-studio/reference-scripts/quantum-death-bubble.txt\n"
        "D. skills/voice-studio/reference-scripts/why-space-is-cold.txt\n"
        "E. skills/voice-studio/reference-scripts/solar-system-vertical-flight.txt\n\n"
        "要求：\n"
        "1. 必须仿照 reference-style.md 和三篇参考稿的结构、节奏、句法和钩子逻辑，但不要照抄原句\n"
        "2. 开头前三句话必须见刀：反常识判断 + 悬念标签 + 核心意象；不要先铺床、呼吸、夜色\n"
        "3. 全文至少使用 4 次参考稿式句法：你以为…但… / 要想…你得先… / 你想想看… / 有没有…真有… / 只要…\n"
        "4. 要有明确判断、强比喻、可感数字和黑色幽默，不要写成温柔散文、治愈文或泛泛助眠稿\n"
        "5. 知识点低负荷但情绪张力要高；一个核心知识点配一个画面或比喻，数字用于震惊，不用于堆资料\n"
        "6. 结尾优先用 reference-style.md 的递进收尾或引线式收尾，并使用老波品牌锚点；不要写普通总结\n"
        "7. 目标 20 分钟，最低 15 分钟；按约 220 中文字/分钟估算，写 4200-4800 中文字，绝对不能低于 3300 中文字\n"
        "8. 旁白身份是老波，适合睡前听，不要使用标题、编号、小节名或 Markdown 格式\n"
        f"9. 将文稿写入 skills/voice-studio/runs/{job_id}/script.txt\n"
        f"10. 更新 skills/voice-studio/jobs/{job_id}.json：status=\"ready\", script=<全文>, edited_script=null, error=null\n"
        f"11. job_id={job_id}\n\n"
        "写完后按 reference-style.md 的自检清单自查一遍；如果开头像散文、缺少反常识钩子，或全文低于 3300 中文字，必须先重写再保存。\n"
        "只做以上步骤。不要生成音频，不要发布，不要给用户发消息。"
    )


def run_agent(job):
    prompt = build_prompt(job)
    cmd = [
        str(NODE),
        str(OPENCLAW),
        "agent",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:voice-studio-writer-{job['id']}",
        "--message",
        prompt,
        "--json",
        "--timeout",
        "900",
    ]
    return subprocess.run(
        cmd,
        cwd=str(WORKSPACE_DIR),
        text=True,
        capture_output=True,
        timeout=960,
    )


def finalize_from_script_file(job):
    script_path = RUNS_DIR / job["id"] / "script.txt"
    if not script_path.exists():
        return False
    script = script_path.read_text(encoding="utf-8").strip()
    if not script:
        return False
    if len(script) < MIN_SCRIPT_CHARS:
        job["status"] = "error"
        job["error"] = f"script too short: {len(script)} chars, minimum is {MIN_SCRIPT_CHARS}"
        save_job(job)
        return False
    job["status"] = "ready"
    job["script"] = script
    job["edited_script"] = None
    job["error"] = None
    save_job(job)
    return True


def process_one(job):
    job["status"] = "writing"
    job["error"] = None
    save_job(job)

    try:
        result = run_agent(job)
    except subprocess.TimeoutExpired:
        current = load_job(job_path(job["id"]))
        current["status"] = "error"
        current["error"] = "openclaw agent timed out after 960s"
        save_job(current)
        print(f"[voice-writer] {job['id']} timed out", file=sys.stderr)
        return False

    updated = load_job(job_path(job["id"]))
    if updated.get("status") == "ready" and (updated.get("script") or "").strip():
        if len(updated["script"]) < MIN_SCRIPT_CHARS:
            updated["status"] = "error"
            updated["error"] = f"script too short: {len(updated['script'])} chars, minimum is {MIN_SCRIPT_CHARS}"
            save_job(updated)
            print(f"[voice-writer] {job['id']} failed length check: {len(updated['script'])}", file=sys.stderr)
            return False
        print(f"[voice-writer] {job['id']} ready ({len(updated['script'])} chars)")
        return True

    if result.returncode == 0 and finalize_from_script_file(updated):
        print(f"[voice-writer] {job['id']} ready from script file")
        return True

    err = (result.stderr or result.stdout or "unknown error").strip()
    updated["status"] = "error"
    updated["error"] = f"openclaw agent failed on host: {err[:800]}"
    save_job(updated)
    print(f"[voice-writer] {job['id']} failed: {err[:300]}", file=sys.stderr)
    return False


def main():
    JOBS_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)

    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[voice-writer] another writer is running")
            return 0

        processed = 0
        while True:
            jobs = pending_jobs()
            if not jobs:
                break
            processed += 1
            process_one(jobs[0])
            time.sleep(0.2)

        print(f"[voice-writer] processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
