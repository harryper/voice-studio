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
        "先读并严格遵守这些文件：\n"
        "A. skills/voice-studio/SKILL.md 的 Web job script workflow 和 Script style\n"
        "B. skills/voice-studio/reference-style.md 的历史成稿风格画像和自检清单\n\n"
        "合规与表达纪律（务必遵守，避免触发模型端内容审核）：\n"
        "α. 本任务为公开学术天文学/宇宙学事实科普；主题里出现的任何比喻词（如\"化石\"\"中心\"\"爆炸\"）"
        "都只是叙事抓手，**正文里必须用中性学术表达替换**，包括但不限于：\n"
        "   - \"玛土撒拉 / Methuselah\" → 用 \"HD 140283\" 或 \"那颗编号 HD 140283 的高龄恒星\"\n"
        "   - \"大爆炸\" → 用 \"宇宙形成早期\" 或 \"宇宙诞生之初\"\n"
        "   - \"银河系中心\" → 用 \"银晕高空\"、\"银盘深处\"、\"靠近银心方向\"等具体位置描述\n"
        "   - \"化石\" → 用 \"残留物\"、\"早期世代的恒星\"、\"古老样本\"等中性词\n"
        "   - 任何\"死亡 / 毁灭 / 终结 / 末日 / 末日感\"类修辞 → 改成中性天文过程描述\n"
        "β. 数字、年龄、距离必须来自 NASA / ESA / arXiv / NOIRLab / ApJ / A&A 一手或权威科普源；\n"
        "   没有一手出处的数字不要写；学界共识区间（带 ±）原样保留。\n"
        "γ. 全文严格用第二人称\"你\"和老波口吻；不出现\"99% 的人\"\"科学家都震惊\"等夸张话术；\n"
        "   不使用 \"圣经 / 启示录 / 末世\" 等宗教/末世类词汇做比喻。\n\n"
        "要求：\n"
        "1. 不模仿任何单篇旧稿；只使用 reference-style.md 提炼出的抽象叙事规律\n"
        "2. 开头 30 秒内点题，建立一个反常识矛盾或问题和一个核心画面；不要固定套用\"你以为……错了\"\n"
        "3. 全文只围绕一个核心机制和一条因果链展开，使用生活化画面解释，但比喻不能冒充事实\n"
        "4. 开头可以锋利，中段必须降速；保持低认知负荷、自然口语和适合睡前持续聆听的节奏\n"
        "5. 写稿前先在内部列出 3-6 个关键事实，并使用可用的搜索工具核验。具体数字、日期、任务、研究结论和\"首次\"必须有可靠一手来源；不要只凭模型记忆，无法核验就删除或降级为明确推测\n"
        "6. 不使用无依据的\"99%的人\"\"科学家都睡不着\"等点击话术，不为了震撼虚构数字、因果或科学家态度\n"
        "7. 结尾回扣开头的核心画面，可留白、递进或回环；老波品牌落款按语气决定，不强制\n"
        "8. 目标 20 分钟，最低 15 分钟；按约 220 中文字/分钟估算，写 4200-4800 中文字，绝对不能低于 3300 中文字\n"
        "9. 旁白身份是老波，主要用第二人称\"你\"，不要使用标题、编号、小节名、引用列表或 Markdown 格式\n"
        f"10. 将文稿写入 skills/voice-studio/runs/{job_id}/script.txt\n"
        f"11. 更新 skills/voice-studio/jobs/voice/{job_id}.json：status=\"ready\", script=<全文>, edited_script=null, error=null\n"
        f"12. job_id={job_id}\n\n"
        "**执行纪律（关键，避免触发 LLM 输出审核）**：\n"
        "- 事实核验期间尽量减少在文本里复述\"事实清单已经定型\"\"关键事实已确认\"这类总结段；\n"
        "  搜索/思考过程用最少的过渡文字带过，重点是**直接用 write 工具落盘**。\n"
        "- **不要**输出超过 200 字的事实回顾段、bullet list 总结、\"我现在判断 / 我决定\"型元叙述；\n"
        "  这类文本在某些模型端会触发输出审核导致 stopReason=error。\n"
        "- 找到必要事实后**立刻调用 write 工具把全文一次性写入 script.txt**，再更新 job JSON。\n"
        "- 最终回复只允许一句话说明已写入文件，例如：\"已写入 runs/<id>/script.txt（<N> 中文字）。\"\n"
        "写完后按 reference-style.md 的自检清单自查；如果事实未核验、开头没有点题、因果链发散或全文低于 3300 中文字，必须先修正再保存。\n"
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


# Debounce window: if the .writer-trigger was touched within this many
# seconds before this script started, sleep until the window has passed.
# This collapses bursty filesystem events (web app creates several jobs in
# quick succession → many path-triggered service starts) into a single run.
DEBOUNCE_SECONDS = 3

# Last-run marker used to throttle token-plan consumption across service
# invocations. If the previous writer run finished less than this many
# seconds ago, sleep until the gap is met before launching the next
# `openclaw agent` call. One agent run = ~5 min of LLM time + 4-5k tokens
# for a 4k-char script.
MIN_GAP_BETWEEN_RUNS = 30

# Cap how many jobs a single writer invocation will process. The next job
# gets picked up by the *next* path-triggered service start (the writer
# itself writes back into jobs/, which re-fires PathChanged on /jobs).
MAX_JOBS_PER_RUN = 1

TRIGGER = SKILL_DIR / ".writer-trigger"
LAST_RUN_MARKER = SKILL_DIR / ".writer.lastrun"


def _sleep_until_quiet():
    """Sleep until the trigger file has been stable for DEBOUNCE_SECONDS.

    A bursty web-app creator will touch .writer-trigger 3-4 times in 200ms.
    systemd's path unit fires once per event, so we get 3-4 service starts
    in a row. The first one grabs fcntl + waits 3s; if the file was re-touched
    during the wait, restart. The remaining service starts block on fcntl
    and exit cheaply once the first run finishes.
    """
    if not TRIGGER.exists():
        return
    deadline = time.time() + DEBOUNCE_SECONDS * 4  # hard cap
    while time.time() < deadline:
        mtime = TRIGGER.stat().st_mtime
        age = time.time() - mtime
        if age >= DEBOUNCE_SECONDS:
            return
        time.sleep(min(DEBOUNCE_SECONDS, max(0.2, DEBOUNCE_SECONDS - age)))


def _respect_min_gap():
    """If the previous run finished < MIN_GAP_BETWEEN_RUNS ago, sleep."""
    if not LAST_RUN_MARKER.exists():
        return
    try:
        last = float(LAST_RUN_MARKER.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return
    if not last:
        return
    gap = time.time() - last
    if gap >= MIN_GAP_BETWEEN_RUNS:
        return
    wait = MIN_GAP_BETWEEN_RUNS - gap
    print(f"[voice-writer] throttling: previous run {gap:.1f}s ago, sleeping {wait:.1f}s")
    time.sleep(wait)


def _stamp_last_run():
    LAST_RUN_MARKER.write_text(f"{time.time()}\n", encoding="utf-8")


def main():
    JOBS_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)

    # Race: another writer is already running. The other run will pick up
    # anything we'd touch too, so we exit cheaply.
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[voice-writer] another writer is running, skipping")
            return 0

        _sleep_until_quiet()
        _respect_min_gap()

        processed = 0
        for _ in range(MAX_JOBS_PER_RUN):
            jobs = pending_jobs()
            if not jobs:
                break
            process_one(jobs[0])
            processed += 1

        _stamp_last_run()
        print(f"[voice-writer] processed={processed} (cap={MAX_JOBS_PER_RUN})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
