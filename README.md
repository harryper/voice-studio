# voice-studio

Generate original ~15-minute audio narrations (sleep / narration / ambient) from a user-provided theme, using **Azure Speech REST TTS** (primary, 云泽 voice) with **MiniMax** as fallback.

## Workflow

```
theme → gpt-5.5 script → Azure TTS (fallback: MiniMax) → mix with BGM → publish
```

1. Receive a topic from the user
2. Spawn a subagent to write an original Chinese narration script sized from target duration and calibrated reading speed (default ~3300-3600 Chinese chars for ~15 min)
3. Generate narration audio via Azure Speech TTS; fall back to MiniMax if Azure unavailable
4. Mix with default looped BGM (3% low-volume bed)
5. Publish as MP3 and return direct download link

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
# Azure TTS key (not tracked by git)
echo "your-azure-key" > scripts/azure_speech_key.txt
chmod 600 scripts/azure_speech_key.txt

# MiniMax key (fallback, not tracked by git)
echo "your-minimax-key" > scripts/minimax_api_key.txt
chmod 600 scripts/minimax_api_key.txt
```

## Usage

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