# voice-studio

Generate original ~15-minute audio narrations (sleep / narration / ambient) from a user-provided theme, using MiniMax `speech-2.8-hd` HTTP TTS.

## Workflow

```
theme → gpt-5.5 script (fallback: MiniMax-M2.7) → MiniMax TTS → mix with BGM → publish
```

1. Receive a topic from the user
2. Spawn a subagent to write an original Chinese narration script sized from target duration and calibrated reading speed (default ~3300-3600 Chinese chars for ~15 min)
3. Generate narration audio in a single MiniMax HTTP call
4. Mix with default looped BGM (3% low-volume bed)
5. Publish as MP3 and return direct download link

## Scripts

| Script | Purpose |
|--------|---------|
| `minimax_tts.py` | TTS generation via MiniMax HTTP API |
| `mix_with_bgm.py` | Mix voice + BGM into final MP3 |
| `publish_download.py` | Copy file to public-downloads and report URL |
| `download_douyin_web.py` | Download Douyin video (legacy / on request only) |

## Setup

```bash
# Place your MiniMax API key here (not tracked by git)
echo "your-api-key" > scripts/minimax_api_key.txt
chmod 600 scripts/minimax_api_key.txt
```

## Usage

```bash
# TTS generation
python3 scripts/minimax_tts.py --text script.md --out voice.mp3

# Mix with BGM
python3 scripts/mix_with_bgm.py --voice voice.mp3 --out final.mp3 --bgm-volume 0.03

# Publish
python3 scripts/publish_download.py --file final.mp3 --folder voice-studio --name my-audio.mp3
```

## Assets

- `assets/bgm_default.mp3` — default ambient BGM (looped for full narration, 3% volume by default, felt more than heard)

## TTS Config (defaults)

| Parameter | Value |
|-----------|-------|
| Model | `speech-2.8-hd` |
| Voice | `Chinese (Mandarin)_Gentle_Youth` |
| Speed | `0.85` |
| Max chars/call | 10,000 |

## Token-saving rules

- Write the script only once (no multi-draft rewrites)
- Do not transcribe videos unless explicitly requested
- If the script is too long, trim locally rather than rewriting