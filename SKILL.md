---
name: voice-studio
description: "voice-studio skill: manage the voice-studio Web project for original sleep/narration/ambient audio. All creation must go through the Web job workflow: create/inspect jobs, write scripts into ready state, and only run TTS from the Web UI action or an explicit user instruction tied to a Web job. Only process video links if the user explicitly asks to parse/extract a video."
---

# voice-studio skill

Technical skill id: `voice-studio`. User-facing name: **voice-studio skill**.

The canonical product is the **voice-studio Web project**. Do not run the old direct/default "topic → script → TTS → mix → publish" flow from chat.

## Canonical Web workflow

All voice-studio work goes through `skills/voice-studio/app.py` and `skills/voice-studio/jobs/*.json`.

For web `mode="theme"` jobs:
- `pending` → write script → update job to `status="ready"`
- stop there for user review
- do **not** call TTS, mix BGM, publish audio, or message the user
- only run TTS from the Web UI `/process-tts` action, or when the user explicitly says to generate audio for a specific Web job

For web `mode="script"` jobs:
- load the pasted script into `status="ready"`
- let the user edit/review in the Web UI
- only run TTS from the Web UI `/process-tts` action, or when explicitly instructed for that job

Direct chat requests like "用 voice-studio 做一个主题" should create or guide the user to a Web job, not bypass the Web project.

## Script writing

The Web app (`app.py`) creates `mode="theme"` jobs with `status="pending"` and touches `.writer-trigger`. A host-side systemd watcher (`voice-studio-writer.path` → `voice-studio-writer.service`) runs `scripts/process_pending_voice_jobs.py`, calls `openclaw agent` from the host environment, generates the script, and updates the job to `status="ready"`. Do not call `openclaw agent` from inside the Docker Web container; it cannot reliably reach the host gateway/auth context. The writer must read `reference-style.md` and the three files in `reference-scripts/` before drafting, then imitate their structure, hook logic, sentence rhythm, and ending style without copying original sentences. The main chat session should only orchestrate the Web job workflow: create or inspect jobs, and stop for review. Do not draft the long narration directly in the main chat session.

**Important constraint for content style:** The script is for a **narration/voice blog**, not a document or article with formal section headers. Do not prefix paragraphs with titles or labels (e.g. "一、", "1.", "【】", or bolded headings). Write in continuous, breathable prose with natural paragraph breaks — the kind that sounds like someone talking softly, not reading a report. Keep the tone intimate, unhurried, and suited for listening rather than scanning.

This skill no longer defaults to parsing other videos. Only download/transcribe video links if the user explicitly asks: `解析这个视频` / `提取视频文案` / `按这个视频改`.

## TTS Provider: Azure Speech (primary) + MiniMax (fallback) (as of 2026-05-21)

### Primary: Azure Speech REST TTS

**Key:** stored in `scripts/azure_speech_key.txt` (permission 600)

**Script:** `scripts/azure_tts.py` (workspace root)

**Region:** `eastasia`

**Voice:** `zh-CN-YunzeNeural` (云泽; 中年男声，平静温和)

**Default params:** `--style calm --rate=-10%` (慢速平静，适合助眠)

**Supported params:** `--style`, `--styledegree`, `--rate`, `--pitch`, `--volume`, `--pause-ms`

**Why Azure primary:**
- 音色选择多，情感控制强
- 免费层每月 50 万字符
- REST API 稳定，无每日硬上限

### Fallback: MiniMax HTTP TTS

**API Key:** stored in `scripts/minimax_api_key.txt` (permission 600)

**Endpoint:** `https://api.minimaxi.com/v1/t2a_v2`

**Model:** `speech-2.8-hd`

**Voice:** `Chinese (Mandarin)_Gentleman` (备用; 男声，沉稳克制)

- Speed: `0.85` (慢速；适合助眠节奏)

**MiniMax limits:** 每日额度上限 11000 字；超出后会失败

**When to fall back:** Azure 不可用、配额耗尽、或用户明确指定 MiniMax 时

## Canonical assets

- **TTS (primary):** Azure `zh-CN-YunzeNeural` via `scripts/azure_tts.py`
- **TTS (fallback):** MiniMax `speech-2.8-hd` via `scripts/minimax_tts.py`
- **BGM:** `skills/voice-studio/assets/bgm_default.mp3` (默认混入成品；除非用户明确要 voice-only)
- **BGM:** `skills/voice-studio/assets/bgm_default.mp3` (默认混入成品；除非用户明确要 voice-only)
- **OSS upload:** `scripts/upload_to_oss.py` → R2 bucket `openclaw`, 30-day pre-signed URL

## Web job script workflow

1. Receive or inspect a Web job topic/theme.
2. Create an original Chinese narration script, written for listening (not a titled document). Avoid all section headers, numbered labels, and structured article formatting. The output should read like natural speech across a handful of unhurried paragraphs.
3. Determine script length from the target audio duration and calibrated reading speed. Do **not** use a fixed length blindly. Target: **20 minutes** (minimum 15 minutes). Until a full-length calibration is measured, estimate using **~220 Chinese characters/minute** for Azure `zh-CN-YunzeNeural` with `--style calm --rate=-10%`. For a 20-minute target, draft about **4400 Chinese characters** (minimum 3300 characters for the 15-minute floor). Keep it slow and breathable. Avoid scripts that are obviously too short or too long.
4. Narrator identity: **老波**. 品牌锚点不必每处都显——开头"我是老波"可视情况省略（若能直接入题效果更好则省略），结尾锚点偶用即可，不必强求。全程以第二人称"你"与听众对话，像睡前聊天的朋友，不是课堂讲师。
5. Write the script to `runs/<job_id>/script.txt` and update the job JSON with `status="ready"`, `script=<full script>`, `edited_script=null`, `error=null`, and `updated_at`.
6. Stop. TTS/mixing/publishing are separate Web actions.

## Script style

Write directly in a sleep/narration cadence. Do not create a long outline first unless the user asks.

The script must first work as **immersive sleep audio**, not as a compressed knowledge article. The listener should feel personally placed inside the scene, become calmer within the first minute, and clearly understand the theme early.

Structure:

1. Quiet hook: 20-40 seconds, one unsettling or expansive question that names or strongly reveals the theme immediately.
2. Immersive descent: place the listener in a concrete, low-stimulation scene — lying in bed, looking at the ceiling, hearing night sounds, watching darkness, breathing slower.
3. Theme anchoring: within the first 60-90 seconds, clearly return to the exact theme/question. Do not delay the topic for several minutes.
4. Gentle scale expansion: move from the listener's body and room → city/night sky → Earth → solar system → stars → galaxies → deep time/space.
5. Soft repetition: repeat the core theme in different, quiet forms throughout the script so the listener never loses the subject.
6. Soft landing: end calmly. 留白式很强：不解决问题，把听者扔在不安感里。品牌锚点（"我是老波，咱们在梦中的平行宇宙继续聊。" / "我是老波，祝你晚安。"）偶用即可，不必每篇都强求。

Rules:

- Before drafting, calculate target character count: `target_minutes × calibrated_chars_per_minute`. Target: **20 minutes** (~4400 chars at 220 chars/min). Hard floor: **15 minutes** (~3300 chars). Use the latest measured speed from prior productions when available. If no calibration exists, use ~220 Chinese chars/min for Azure.
- After Web TTS generation, check actual audio duration. The acceptable range is **15-25 minutes**; 20 minutes is the target. Do **not** recalibrate or rewrite solely for duration if output falls within 15-25 minutes. Only if it is shorter than 15 minutes, adjust future script length: `new_chars = current_chars × target_seconds / actual_seconds`.
- The listener must have **代入感**: use second-person perspective (`你`) often, concrete sensations, slow breathing cues, darkness, distance, silence, temperature, and bodily relaxation.
- The opening must quickly answer: "我现在在听什么主题？" If the listener cannot identify the theme within the first minute, rewrite the opening.
- Keep cognitive load low. Do not stack too many facts, numbers, or definitions. Use facts as quiet stepping stones, not lecture notes.
- Use short paragraphs and natural pauses.
- Prefer calm certainty over clickbait.
- Avoid dense citation-style exposition.
- Do not fabricate specific named studies, dates, or numbers unless verified or broadly established.
- For speculative ideas, say `也许`, `可能`, `有一种想法认为`.
- Keep it original; do not imitate or preserve another creator's signature phrases.

## Token-saving rules

- Do not transcribe videos unless explicitly requested.
- Do not write multiple drafts by default.
- Do not ask a model to rewrite the entire script after drafting; draft once in final spoken form.
- If the script is too long, trim locally rather than asking for another full rewrite.
- Before Web TTS, optionally make a 60-90 second sample only when the user asks for a 试听.

## Web TTS implementation: Azure TTS (primary)

These commands are implementation details for the Web `/process-tts` action. Do not run them as a separate default flow.

```bash
python3 skills/voice-studio/scripts/azure_tts.py \
  --text <script.md> \
  --voice zh-CN-YunzeNeural \
  --style calm \
  --rate=-10% \
  --out <voice.mp3>
```

If Azure fails, fall back to MiniMax single-call:

```bash
python3 skills/voice-studio/scripts/minimax_tts.py \
  --text <script.md> \
  --out <voice.mp3> \
  --voice "Chinese (Mandarin)_Gentleman" \
  --speed 0.85 \
  --retries 1
```

## Web BGM mixing implementation

After Web voice generation, the Web TTS action mixes with **default BGM** by default. Keep the voice track natural and intact; loop the BGM for the full voice duration, lower it to 3%, and layer it underneath.

```bash
python3 skills/voice-studio/scripts/mix_with_bgm.py \
  --voice <voice.mp3> \
  --bgm skills/voice-studio/assets/bgm_default.mp3 \
  --out <final-mixed.mp3> \
  --bgm-volume 0.03
```

Rules:
- Main deliverable is the **mixed** MP3 unless the user explicitly asks for voice-only.
- Default BGM volume is **3%** (`0.03`). Keep it as a subtle atmosphere bed, not audible background music.
- BGM must loop for the full narration duration; do not pad the tail with silence.
- Do **not** use loudnorm/overall normalization by default; preserve the raw voice sound and compensate only for `amix` level behavior in the script.
- If BGM asset is missing or mixing fails, publish voice-only and note it clearly.

## Web publish implementation

Upload to Cloudflare R2 with 30-day pre-signed URL. Do not store to local public-downloads.

```bash
python3 skills/voice-studio/scripts/upload_to_oss.py \
  --file <final-mixed.mp3> \
  --folder cosmic-sleep \
  --name <safe-name>.mp3
```

Returns a pre-signed URL (e.g. `https://fd978dbd...r2.cloudflarestorage.com/openclaw/2026-05-22/cosmic-sleep/xxx.mp3?X-Amz-...&X-Amz-Expires=2592000`). Link is valid for 30 days.

## Legacy video mode

Only use this when explicitly requested. For Douyin links:

```bash
python3 skills/voice-studio/scripts/download_douyin_web.py "https://v.douyin.com/.../" --out-dir tmp/cosmic-sleep/<slug>
```

Then transcribe with `faster_whisper`, lightly clean, replace original narrator self-reference with **老波**, create/update a Web job for review, and stop at `status="ready"` unless the user explicitly triggers Web TTS.
