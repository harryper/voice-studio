---
name: voice-studio
description: "voice-studio skill: create original ~15-minute audio narrations (sleep/narration/ambient) from a user-provided theme, using MiniMax speech-2.8-hd HTTP TTS. Use when the user gives a topic/theme and asks to generate voice audio, 音频, TTS, 旁白, 科普助眠, or says voice-studio. Single-call MiniMax TTS handles up to 10000 chars — enough for full narration. Only process video links if the user explicitly asks to parse/extract a video."
---

# voice-studio skill

Technical skill id: `voice-studio`. User-facing name: **voice-studio skill**.

Default task: the user gives **one topic/theme**, and you create an original narration audio around **15 minutes** long.

## Script writing

Use **gpt-5.5** (custom-fm-5-5) for script drafting. If gpt-5.5 is unavailable, fall back to **MiniMax-M2.7** for writing. Spawn a child subagent with `sessions_spawn(runtime="subagent", mode="run", cleanup="delete", model="gpt-5.5")` (or `model="MiniMax-M2.7"` for fallback). The main session should only orchestrate the workflow: save the script, run TTS scripts, mix BGM, publish files, and report links. Do not draft the long narration directly in the main session.

**Important constraint for content style:** The script is for a **narration/voice blog**, not a document or article with formal section headers. Do not prefix paragraphs with titles or labels (e.g. "一、", "1.", "【】", or bolded headings). Write in continuous, breathable prose with natural paragraph breaks — the kind that sounds like someone talking softly, not reading a report. Keep the tone intimate, unhurried, and suited for listening rather than scanning.

This skill no longer defaults to parsing other videos. That workflow was token-heavy. Only download/transcribe video links if the user explicitly asks: `解析这个视频` / `提取视频文案` / `按这个视频改`.

## TTS Provider: MiniMax HTTP TTS (as of 2026-05-19)

**API Key:** stored in `scripts/minimax_api_key.txt` (permission 600)

**Endpoint:** `https://api.minimaxi.com/v1/t2a_v2` (HTTP, not WebSocket)

**Model:** `speech-2.8-hd`

**Voice:** `Chinese (Mandarin)_Gentle_Youth` (default; 温和青年，柔和亲切，适合助眠旁白)
- Speed: `0.85` (默认，慢速；适合助眠节奏)

**Why single-call:**
- MiniMax HTTP TTS supports up to **10,000 characters** per call
- Default ~15-minute scripts are well within this limit
- No segmentation, no chunk stitching, no tempo drift

## Canonical assets

- **TTS:** MiniMax HTTP `speech-2.8-hd` via `scripts/minimax_tts.py`
- **BGM:** `skills/voice-studio/assets/bgm_default.mp3` (默认混入成品；除非用户明确要 voice-only)
- **Public downloads root:** `/root/.openclaw/workspace/public-downloads/`
- **Public URL prefix:** `http://43.173.67.197:18082/` when static server is running

## Default workflow: theme → original ~15-minute audio

1. Receive a topic/theme from the user.
2. Create an original Chinese narration script, written for listening (not a titled document). Avoid all section headers, numbered labels, and structured article formatting. The output should read like natural speech across a handful of unhurried paragraphs.
3. Determine script length from the target audio duration and the current voice reading speed. Do **not** use a fixed length blindly. For the default MiniMax `Chinese (Mandarin)_Gentle_Youth` at speed `0.85`, recent calibration is about **230 Chinese characters/minute** (3809 Chinese chars → 16m31s). For a ~15-minute target, draft about **3300-3600 Chinese characters** first, then keep it slow and breathable. Avoid scripts that are obviously too short or too long.
4. Narrator identity: use **Jesse** if self-reference is needed.
5. Generate narration with MiniMax HTTP TTS in a **single call**.
6. Mix the narration with **default BGM** by default.
7. Publish the mixed final MP3 as the main direct download link.

## Script style

Write directly in a sleep/narration cadence. Do not create a long outline first unless the user asks.

Structure:

1. Quiet hook: 20-40 seconds, one unsettling or expansive question.
2. Slow descent: explain the idea simply, with concrete images.
3. Cosmic scale expansion: move from human scale to Earth, stars, galaxies, time, or deep space.
4. Gentle mystery: keep wonder, avoid hard-selling or sensational shouting.
5. Soft landing: end calmly, with a Jesse sign-off only if natural.

Rules:

- Before drafting, estimate required script length from target duration: `target_minutes × calibrated_chars_per_minute`. Use the latest measured speed from prior productions when available. If no calibration exists, start with ~230 Chinese chars/min for `Chinese (Mandarin)_Gentle_Youth` speed `0.85`.
- After TTS generation, check actual audio duration. The default acceptable range is **12-25 minutes**; if the generated audio falls within this range, do **not** recalibrate or rewrite just for duration. Only if it is shorter than 12 minutes or longer than 25 minutes, adjust future script length using the observed ratio: `new_chars = current_chars × target_seconds / actual_seconds`.
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
- Before full TTS, optionally make a 60-90 second sample only when the user asks for a 试听.

## Generate narration: MiniMax HTTP single call

```bash
python3 skills/voice-studio/scripts/minimax_tts.py \
  --text <script.md> \
  --out <voice.mp3> \
  --voice "Chinese (Mandarin)_Gentle_Youth" \
  --speed 0.85 \
  --retries 1
```

The script sends the full narration in one HTTP request and saves the response hex audio directly to MP3. No chunking, no stitching needed.

## BGM mixing: default final step

After voice generation, mix with **default BGM** by default. Keep the MiniMax voice track natural and intact; loop the BGM for the full voice duration, lower it to 3%, and layer it underneath.

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
- Do **not** use loudnorm/overall normalization by default; preserve the raw MiniMax voice sound and compensate only for `amix` level behavior in the script.
- If BGM asset is missing or mixing fails, publish voice-only and note it clearly.

## Publish download link

```bash
python3 skills/voice-studio/scripts/publish_download.py \
  --file <final-mixed.mp3> \
  --folder cosmic-sleep \
  --name <safe-name>.mp3
```

Return the direct URL:
```
http://43.173.67.197:18082/<folder>/<file>.mp3
```

Do not use OpenClaw control port `18789`; it requires auth and returns `Unauthorized`.

## Legacy video mode

Only use this when explicitly requested. For Douyin links:

```bash
python3 skills/voice-studio/scripts/download_douyin_web.py "https://v.douyin.com/.../" --out-dir tmp/cosmic-sleep/<slug>
```

Then transcribe with `faster_whisper`, lightly clean, replace original narrator self-reference with **Jesse**, and proceed to MiniMax HTTP TTS. Skip BGM unless explicitly requested.