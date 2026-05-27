#!/usr/bin/env python3
"""MiniMax Music API client — lyrics generation + music generation."""
import argparse
import json
import os
import sys
import time
import requests

API_BASE = "https://api.minimaxi.com"


def load_api_key():
    key_path = os.path.join(os.path.dirname(__file__), "minimax_api_key.txt")
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"API key not found: {key_path}")
    return open(key_path).read().strip()


def generate_lyrics(prompt, title=None, mode="write_full_song", api_key=None):
    """Call /v1/lyrics_generation. Returns dict with song_title, style_tags, lyrics."""
    url = f"{API_BASE}/v1/lyrics_generation"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or load_api_key()}",
    }
    body = {"mode": mode, "prompt": prompt}
    if title:
        body["title"] = title

    resp = requests.post(url, json=body, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("base_resp", {})
    if status.get("status_code", 0) != 0:
        raise RuntimeError(f"Lyrics API error {status.get('status_code')}: {status.get('status_msg')}")

    return {
        "song_title": data.get("song_title", ""),
        "style_tags": data.get("style_tags", ""),
        "lyrics": data.get("lyrics", ""),
    }


def generate_music(prompt, lyrics, output_path, model="music-2.6",
                   is_instrumental=False, lyrics_optimizer=False,
                   sample_rate=44100, bitrate=256000, fmt="mp3",
                   api_key=None):
    """Call /v1/music_generation. Downloads MP3 to output_path."""
    url = f"{API_BASE}/v1/music_generation"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or load_api_key()}",
    }
    body = {
        "model": model,
        "prompt": prompt,
        "output_format": "url",
        "audio_setting": {
            "sample_rate": sample_rate,
            "bitrate": bitrate,
            "format": fmt,
        },
    }
    if is_instrumental:
        body["is_instrumental"] = True
    elif lyrics_optimizer and not lyrics:
        body["lyrics_optimizer"] = True
    else:
        body["lyrics"] = lyrics

    resp = requests.post(url, json=body, headers=headers, timeout=300)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("base_resp", {})
    if status.get("status_code", 0) != 0:
        raise RuntimeError(f"Music API error {status.get('status_code')}: {status.get('status_msg')}")

    # Get audio from response — could be URL or hex depending on output_format
    music_data = data.get("data", {})
    audio_field = music_data.get("audio", "")
    extra = data.get("extra_info", {})

    # Determine if audio_field is a URL or hex-encoded data
    if audio_field.startswith("http"):
        # It's a URL — download it
        dl = requests.get(audio_field, timeout=120)
        dl.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(dl.content)
    elif audio_field:
        # Assume hex-encoded audio
        with open(output_path, "wb") as f:
            f.write(bytes.fromhex(audio_field))
    else:
        raise RuntimeError("No audio data in response")

    return {
        "duration_ms": extra.get("music_duration"),
        "sample_rate": extra.get("music_sample_rate"),
        "channels": extra.get("music_channel"),
        "bitrate": extra.get("bitrate"),
        "size_bytes": os.path.getsize(output_path),
    }


def main():
    parser = argparse.ArgumentParser(description="MiniMax Music API")
    sub = parser.add_subparsers(dest="command")

    # lyrics subcommand
    lp = sub.add_parser("lyrics", help="Generate lyrics from theme")
    lp.add_argument("--prompt", required=True, help="Theme/style description")
    lp.add_argument("--title", default=None, help="Optional song title")
    lp.add_argument("--mode", default="write_full_song", choices=["write_full_song", "edit"])
    lp.add_argument("--output", "-o", default=None, help="Output JSON file (default: stdout)")

    # music subcommand
    mp = sub.add_parser("music", help="Generate music from lyrics")
    mp.add_argument("--prompt", required=True, help="Style/mood description")
    mp.add_argument("--lyrics", default=None, help="Lyrics text (or --lyrics-file)")
    mp.add_argument("--lyrics-file", default=None, help="File containing lyrics")
    mp.add_argument("--output", "-o", required=True, help="Output MP3 file")
    mp.add_argument("--model", default="music-2.6", choices=["music-2.6", "music-2.6-free", "music-cover", "music-cover-free"])
    mp.add_argument("--instrumental", action="store_true", help="Generate instrumental only")
    mp.add_argument("--lyrics-optimizer", action="store_true", help="Auto-generate lyrics from prompt")
    mp.add_argument("--sample-rate", type=int, default=44100)
    mp.add_argument("--bitrate", type=int, default=256000)
    mp.add_argument("--format", default="mp3", choices=["mp3", "wav", "pcm"])

    args = parser.parse_args()

    if args.command == "lyrics":
        result = generate_lyrics(args.prompt, title=args.title, mode=args.mode)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"Saved to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "music":
        lyrics_text = args.lyrics or ""
        if args.lyrics_file:
            with open(args.lyrics_file, encoding="utf-8") as f:
                lyrics_text = f.read()
        result = generate_music(
            args.prompt, lyrics_text, args.output,
            model=args.model,
            is_instrumental=args.instrumental,
            lyrics_optimizer=args.lyrics_optimizer,
            sample_rate=args.sample_rate,
            bitrate=args.bitrate,
            fmt=args.format,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
