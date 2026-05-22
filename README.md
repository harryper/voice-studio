# voice-studio

Web project for creating original ~15-minute audio narrations (sleep / narration / ambient), using **Azure Speech REST TTS** (primary, 云泽 voice) with **MiniMax** as fallback.

## Workflow

```
theme → gpt-5.5 script → ready for review → explicit TTS action only
```

`voice-studio` no longer has a direct default chat workflow. All creation goes through the Web project and its job state files in `jobs/*.json`.

1. Create a Web job from a theme or pasted script.
2. For theme jobs, spawn a subagent to write an original Chinese narration script sized from target duration and calibrated reading speed (default ~3300-3600 Chinese chars for ~15 min).
3. Update the job to `status="ready"` for review.
4. Generate narration audio only from the Web UI TTS action or an explicit instruction tied to a specific Web job.
5. The Web TTS action handles Azure fallback, optional BGM mixing, publishing, and the final public MP3 URL.

Duration policy: default target is ~15 minutes, but **12-25 minutes is acceptable**. Do not recalibrate/rewrite solely for duration if output falls in that range.

Writing policy: script must create listener immersion and surface the theme within the first 60-90 seconds. Use second-person sensory scenes and low cognitive load; avoid sounding like a knowledge article.

## TTS Providers

| Provider | Voice | Notes |
|----------|-------|-------|
| **Azure (primary)** | `zh-CN-YunzeNeural` | 云泽，中年男声；REST API，免费层 50 万字符/月 |
| **MiniMax (fallback)** | `Chinese (Mandarin)_Gentleman` | 每日上限 11000 字 |

## Scripts

| Script | Purpose |
|--------|---------|
| `azure_tts.py` | TTS generation via Azure Speech REST API (primary) |
| `minimax_tts.py` | TTS generation via MiniMax HTTP API (fallback) |
| `mix_with_bgm.py` | Mix voice + BGM into final MP3 |
| `publish_download.py` | Copy file to public-downloads and report URL |
| `download_douyin_web.py` | Download Douyin video (legacy / on request only) |

## Setup

```bash
# Web UI config (not tracked by git)
cp config.example.json config.json
# then edit config.json and replace password / secret_key

# Azure TTS key (not tracked by git)
echo "your-azure-key" > scripts/azure_speech_key.txt
chmod 600 scripts/azure_speech_key.txt

# MiniMax key (fallback, not tracked by git)
echo "your-minimax-key" > scripts/minimax_api_key.txt
chmod 600 scripts/minimax_api_key.txt
```

The Web UI can also be configured with environment variables:

- `VOICE_STUDIO_PASSWORD`
- `VOICE_STUDIO_SECRET_KEY`
- `VOICE_STUDIO_PORT`
- `VOICE_STUDIO_DOWNLOAD_ROOT`
- `VOICE_STUDIO_COSMIC_FOLDER`

## Web UI

```bash
docker compose up -d --build
```

Open `http://<host>:9999/`. The generated public MP3 links are published by `publish_download.py`, normally through the separate public-downloads HTTP service.

## Implementation Commands

These commands are implementation details behind the Web TTS action. Do not use them as a separate default workflow.

```bash
# TTS generation (primary: Azure)
python3 scripts/azure_tts.py --text script.md --out voice.mp3 --voice zh-CN-YunzeNeural --style calm --rate=-10%

# Fallback: MiniMax
python3 scripts/minimax_tts.py --text script.md --out voice.mp3 --voice "Chinese (Mandarin)_Gentleman" --speed 0.85

# Mix with BGM
python3 scripts/mix_with_bgm.py --voice voice.mp3 --out final.mp3 --bgm-volume 0.03

# Publish
python3 scripts/publish_download.py --file final.mp3 --folder cosmic-sleep --name my-audio.mp3
```

## Assets

- `assets/bgm_default.mp3` — default ambient BGM (looped for full narration, 3% volume by default, felt more than heard)

## TTS Config (defaults)

### Azure (primary)
| Parameter | Value |
|-----------|-------|
| Voice | `zh-CN-YunzeNeural` (云泽) |
| Style | `calm` |
| Rate | `-10%` |
| Region | `eastasia` |

### MiniMax (fallback)
| Parameter | Value |
|-----------|-------|
| Model | `speech-2.8-hd` |
| Voice | `Chinese (Mandarin)_Gentleman` |
| Speed | `0.85` |

## Token-saving rules

- Write the script only once (no multi-draft rewrites)
- Do not transcribe videos unless explicitly requested
- If the script is too long, trim locally rather than rewriting
