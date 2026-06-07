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
OPENCLAW_ROOT = WORKSPACE_DIR.parent
JOBS_DIR = SKILL_DIR / "jobs" / "voice"
RUNS_DIR = SKILL_DIR / "runs"
LOCK_PATH = SKILL_DIR / ".writer.lock"
NODE = Path("/usr/bin/node")
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
        f"10. 更新 skills/voice-studio/jobs/voice/{job_id}.json：status=\"ready\", script=<全文>, edited_script=null, error=null\n"
        f"11. job_id={job_id}\n\n"
        "关键执行纪律：不要把全文写在 thinking / analysis / 最终回复里；必须使用可用的文件写入或编辑工具落盘。"
        "最终回复只允许一句话说明已写入文件。\n"
        "写完后按 reference-style.md 的自检清单自查一遍；如果开头像散文、缺少反常识钩子，或全文低于 3300 中文字，必须先重写再保存。\n"
        "只做以上步骤。不要生成音频，不要发布，不要给用户发消息。"
    )


def run_agent(job):
    prompt = build_prompt(job)
    # Use a fresh session per attempt so the model can't see its prior
    # "I already wrote it" reply and short-circuit on retry with 0 tool
    # calls. The job id stays in the key for traceability, but we append a
    # monotonic attempt counter that is bumped each time the writer picks
    # the job up.
    attempt = int(job.get('writer_attempt') or 0) + 1
    job['writer_attempt'] = attempt
    save_job(job)
    cmd = [
        str(NODE),
        str(OPENCLAW),
        "agent",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:voice-studio-writer-{job['id']}-a{attempt}",
        "--message",
        prompt,
        "--thinking",
        "off",
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


def _session_jsonl_path(job_id):
    """Resolve the session jsonl written by the openclaw agent CLI run.

    The agent uses --session-key agent:main:voice-studio-writer-<job_id>;
    the session id is recorded in agents/main/sessions/sessions.json.
    """
    sessions_index = OPENCLAW_ROOT / "agents" / "main" / "sessions" / "sessions.json"
    if not sessions_index.exists():
        return None
    try:
        with sessions_index.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    needle = f"agent:main:voice-studio-writer-{job_id}"
    info = data.get(needle) or {}
    session_file = info.get("sessionFile")
    if not session_file:
        return None
    return Path(session_file)


def scrape_session_error(job_id, result):
    """Pull the actual failure cause out of the openclaw session jsonl.

    The CLI's stderr/stdout is mostly startup noise (Doctor warnings, plugin
    auto-load notices). The real failure lives in the most recent assistant
    message: either a stopReason="error" with errorMessage, or a short
    no-tool-call "I already did it" hallucination. Build a single-line error
    that names the actual cause instead of the noisy stderr.
    """
    fallback = ((result.stderr or result.stdout or "").strip() or "unknown error")[:800]
    fallback_msg = f"openclaw agent failed on host: {fallback}"

    session_file = _session_jsonl_path(job_id)
    if not session_file or not session_file.exists():
        return fallback_msg

    last_assistant = None
    last_texts = []
    tool_call_count = 0
    had_tool_error = False
    try:
        with session_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    last_texts = [
                        c.get("text", "") for c in msg.get("content", [])
                        if c.get("type") == "text" and c.get("text")
                    ]
                    for c in msg.get("content", []):
                        if c.get("type") == "toolCall":
                            tool_call_count += 1
                if msg.get("role") == "toolResult" and msg.get("isError"):
                    had_tool_error = True
    except OSError:
        return fallback_msg

    if not last_assistant:
        return fallback_msg

    err_msg = last_assistant.get("errorMessage")
    if err_msg:
        return f"openclaw agent failed: {err_msg}"

    stop_reason = last_assistant.get("stopReason")
    if stop_reason == "error" and not err_msg:
        return f"openclaw agent failed: stopReason=error (no errorMessage); rc={result.returncode}"

    last_text = " ".join(last_texts).strip()

    if stop_reason == "stop" and tool_call_count == 0 and last_text:
        preview = last_text[:160].replace("\n", " ")
        return (
            "openclaw agent returned without any tool call but reported done "
            f"(model hallucination, last text: \"{preview}\")"
        )

    if not last_text and not had_tool_error:
        return (
            f"openclaw agent returned no assistant text and no tool calls; "
            f"rc={result.returncode}; stderr={fallback[:200]}"
        )

    return fallback_msg


def finalize_from_script_file(job):
    script_path = RUNS_DIR / job["id"] / "script.txt"
    if not script_path.exists():
        return False
    script = script_path.read_text(encoding="utf-8").strip()
    if not script:
        return False
    if len(script) < MIN_SCRIPT_CHARS:
        #字数不够，不是 fatal error；写入了说明 agent 完成了工作，只是太短
        #重置为 retry_pending，让下一轮 writer 补写，不占用 error 状态
        job["status"] = "retry_pending"
        job["error"] = f"script too short ({len(script)} chars, min {MIN_SCRIPT_CHARS}); will retry"
        save_job(job)
        print(f"[voice-writer] {job['id']} short ({len(script)} chars), set to retry_pending", file=sys.stderr)
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

    try:
        updated = load_job(job_path(job["id"]))
    except (OSError, json.JSONDecodeError) as exc:
        updated = dict(job)
        print(f"[voice-writer] {job['id']} job json unreadable after agent run: {exc}", file=sys.stderr)
        if result.returncode == 0 and finalize_from_script_file(updated):
            print(f"[voice-writer] {job['id']} ready from script file after json repair")
            return True
        updated["status"] = "error"
        updated["error"] = f"job json unreadable after agent run: {exc}"
        save_job(updated)
        return False

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

    # Agent ran but produced no usable script. Scrape the session jsonl to
    # find the *real* error: the openclaw agent CLI prints startup noise
    # (Doctor warnings, plugin allow-list) to stderr on every invocation,
    # so a raw stderr scrape gives the wrong root cause (e.g. state-migrations
    # warnings) when the actual failure is a model-side error or hallucination.
    real_err = scrape_session_error(job["id"], result)
    updated["status"] = "error"
    updated["error"] = real_err
    save_job(updated)
    tail = real_err.replace("\n", " ")[:300]
    print(f"[voice-writer] {job['id']} failed: {tail}", file=sys.stderr)
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
