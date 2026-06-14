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

The Web app (`app.py`) creates `mode="theme"` jobs with `status="pending"` and touches `.writer-trigger`. A host-side systemd watcher (`voice-studio-writer.path` → `voice-studio-writer.service`) runs `scripts/process_pending_voice_jobs.py`, calls `openclaw agent` from the host environment, generates the script, and updates the job to `status="ready"`. Do not call `openclaw agent` from inside the Docker Web container; it cannot reliably reach the host gateway/auth context. The writer must read `reference-style.md`, which is the canonical abstraction of the project's historical writing style. Individual files under `reference-scripts/` are archival material and are not runtime prompt dependencies. The main chat session should only orchestrate the Web job workflow: create or inspect jobs, and stop for review. Do not draft the long narration directly in the main chat session.

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

**Default Voice (2026-06-01 启用):** `azure_yunze_clone` — 由 Microsoft Azure `zh-CN-YunzeNeural` 4 分钟合成样本调 `/v1/voice_clone` 克隆得到。复用云泽音色，但走 MiniMax 路径，省下 Azure 额度。

**Other voices (可选):**
- `Chinese (Mandarin)_Radio_Host` — 电台男主播（短视频/科普/叙事）
- `Chinese_deep_voiced_male_vv1` — Deep Voice（低沉男声，原默认）
- `Chinese (Mandarin)_Gentle_Senior` — 温婉柔和

`Chinese (Mandarin)_Gentleman` 已下线，不要再作为默认或 fallback 参数使用。

- Speed: `0.9` (页面默认；慢速，助眠节奏；CLI 默认同 0.9)

**MiniMax limits:**
- 走 Token Plan 订阅（当前主用): 受 5 小时滚动 + 周窗口配额限制。平台统一按 3.5 元/万字符 (speech-2.8-hd) 折算 credits。够跑 ~18亿 M3 等价 token/月 额度。
- 走普通 API Key: 每日 11000 字上限（不适用，現用 key 是 sk-cp-  Token Plan 专用 key）

**合规注意:** `azure_yunze_clone` 音色来自 Azure 神经声音合成样本，与 Microsoft 神经声音 ToS 边界需使用者自行评估。适用于内部使用、研发测试、已获微软授权的场景；公开商用前请确认合规。

**When to fall back / switch:** Azure 不可用、配额耗尽、Token Plan 额度够但想避免 Azure 流量、或用户明确指定 MiniMax 时

## Canonical assets

- **TTS (primary):** Azure `zh-CN-YunzeNeural` via `scripts/azure_tts.py`
- **TTS (fallback):** MiniMax `speech-2.8-hd` via `scripts/minimax_tts.py`
- **BGM:** `skills/voice-studio/assets/bgm_default.mp3` (默认以 6% 音量混入成品；除非用户明确要 voice-only)
- **OSS upload:** `scripts/upload_to_oss.py` → R2 bucket `openclaw`, 30-day pre-signed URL

## Web job script workflow

1. Receive or inspect a Web job topic/theme.
2. Create an original Chinese narration script, written for listening (not a titled document). Avoid all section headers, numbered labels, and structured article formatting. The output should read like natural speech across a handful of unhurried paragraphs.
3. Determine script length from the target audio duration and calibrated reading speed. Do **not** use a fixed length blindly. Target: **20 minutes** (minimum 15 minutes). Until a full-length calibration is measured, estimate using **~220 Chinese characters/minute** for Azure `zh-CN-YunzeNeural` with `--style calm --rate=-10%`. For a 20-minute target, draft about **4400 Chinese characters** (minimum 3300 characters for the 15-minute floor). Keep it slow and breathable. Avoid scripts that are obviously too short or too long.
4. Narrator identity: **老波**. 品牌锚点不必每处都显——开头"我是老波"可视情况省略（若能直接入题效果更好则省略），结尾锚点偶用即可，不必强求。全程以第二人称"你"与听众对话，像睡前聊天的朋友，不是课堂讲师。
5. Write the script to `runs/<job_id>/script.txt` and update the job JSON with `status="ready"`, `script=<full script>`, `edited_script=null`, `error=null`, and `updated_at`.
6. Stop. TTS/mixing/publishing are separate Web actions.

## Script style

`reference-style.md` is the single canonical writing specification. Follow its
historical style abstraction rather than imitating individual old scripts.

Hard constraints:

- Open within 30 seconds with the topic, a clear contradiction/question, and one core image; vary the wording instead of always using `你以为……错了`.
- Build the whole narration around one mechanism and one causal chain.
- Start with tension, then lower the pace into an audio-first, low-cognitive-load explanation.
- Verify concrete dates, numbers, missions, studies, and “first-ever” claims against reliable primary sources before drafting. Remove unverifiable details.
- Mark theory and speculation with language such as `可能`, `模型认为`, or `一种解释是`.
- Use second person naturally, but do not force bed, breathing, darkness, or cosmic-scale expansion into every topic.
- End by returning to the opening image through a loop, progression, or unresolved question. The 老波 brand sign-off is optional.
- Keep paragraphs short and spoken. Do not use headings, numbered sections, Markdown, citations, or a reference list in the narration.
- Target **4200-4800 Chinese characters** and never go below **3300**. After TTS, accept **15-25 minutes**; recalibrate future length only when output falls outside that range.
- Keep it original. Do not copy old metaphors, jokes, signature phrases, or endings.

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
  --voice "Chinese_deep_voiced_male_vv1" \
  --speed 0.9 \
  --retries 1
```

## Web BGM mixing implementation

After Web voice generation, the Web TTS action mixes with **default BGM** by default. Keep the voice track natural and intact; loop the BGM for the full voice duration, lower it to 6%, and layer it underneath.

```bash
python3 skills/voice-studio/scripts/mix_with_bgm.py \
  --voice <voice.mp3> \
  --bgm skills/voice-studio/assets/bgm_default.mp3 \
  --out <final-mixed.mp3> \
  --bgm-volume 0.06
```

Rules:
- Main deliverable is the **mixed** MP3 unless the user explicitly asks for voice-only.
- Default BGM volume is **6%** (`0.06`). Keep it as a subtle atmosphere bed, not dominant background music.
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

## Music Cover (翻唱) workflow (2026-06-05 新增)

The voice-studio Web has a third tab **🎤 翻唱** that wraps MiniMax's `music-cover` model. Two modes are supported:

- **One-Step** (default): upload reference audio + target style prompt → preserves the original lyrics, only transforms style/voice. Fastest path.
- **Two-Step**: upload reference audio → call `music_cover_preprocess` to extract a `cover_feature_id` (24h valid) + structured `formatted_lyrics` → user can edit lyrics → call `music_generation` with `--cover-feature-id`. More flexible (full rewrite allowed).

### Web job schema (`mode='music_cover'`)

| Field | Notes |
|---|---|
| `reference_audio_url` | R2 URL of uploaded audio (6s-6min, ≤25MB to web) |
| `reference_audio_name` | Original filename |
| `cover_prompt` | Target style/voice description (e.g. "古风, 竹笛, 女声空灵, 慢板") |
| `cover_mode` | `'one_step'` or `'two_step'` |
| `cover_feature_id` | Set after `cover-preprocess` (two-step only) |
| `formatted_lyrics` | Lyrics extracted by MiniMax (two-step only, editable) |
| `lyrics` / `edited_lyrics` | Two-step lyrics (editable via coverLyricsEditor) |
| `status` | `awaiting_reference` → `pending` → `preprocessing` → `preprocess_ready` → `generating` → `done` (or `error`) |

### Endpoints (in addition to the music endpoints)

- `POST /api/jobs/{id}/cover-preprocess` — two-step step 1, returns 202-ish (status flips async to `preprocess_ready`)
- `POST /api/jobs/{id}/cover-generate` — final generation, async, polls via `/api/jobs/{id}` until `done`/`error`
- `GET /api/jobs/cover` — list active cover jobs (works and favorites UI shared with music tab)

### Storage

Cover jobs live in `jobs/cover/` and archive to `archive/cover/`. They are independent from `voice` and `music` job dirs. Generated covers are uploaded to R2 with `--theme cover`.

### Underlying CLI

`scripts/minimax_music.py music` now has two new flags:

```bash
# one-step
python3 scripts/minimax_music.py music \
  --cover-url https://r2/.../ref.mp3 \
  --prompt "古风, 竹笛, 女声空灵" \
  -o cover.mp3

# two-step
python3 scripts/minimax_music.py preprocess --cover-url https://r2/.../ref.mp3 -o prep.json
python3 scripts/minimax_music.py music \
  --cover-feature-id $(jq -r .cover_feature_id prep.json) \
  --lyrics-file new_lyrics.txt \
  --prompt "爵士, 低沉男声" \
  -o cover.mp3
```

## Legacy video mode

Only use this when explicitly requested. For Douyin links:

```bash
python3 skills/voice-studio/scripts/download_douyin_web.py "https://v.douyin.com/.../" --out-dir tmp/cosmic-sleep/<slug>
```

Then transcribe with `faster_whisper`, lightly clean, replace original narrator self-reference with **老波**, create/update a Web job for review, and stop at `status="ready"` unless the user explicitly triggers Web TTS.
