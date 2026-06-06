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
                   cover_url=None, cover_feature_id=None,
                   api_key=None):
    """Call /v1/music_generation. Downloads MP3 to output_path.
    Supports three modes:
      1) Standard: model in {music-2.6, music-2.6-free} → lyrics + prompt → original song.
      2) Cover One-Step: cover_url set, model in {music-cover, music-cover-free} → style/voice transform, original lyrics preserved.
      3) Cover Two-Step: cover_feature_id set + custom lyrics → cover with rewritten lyrics.
    """
    url = f"{API_BASE}/v1/music_generation"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or load_api_key()}",
    }
    body = {
        "model": model,
        "output_format": "url",
        "audio_setting": {
            "sample_rate": sample_rate,
            "bitrate": bitrate,
            "format": fmt,
        },
    }

    is_cover = model in ("music-cover", "music-cover-free")

    if is_cover and cover_url and cover_feature_id:
        raise ValueError("cover_url and cover_feature_id are mutually exclusive")
    if is_cover and cover_feature_id and not lyrics:
        raise ValueError("cover_feature_id mode requires --lyrics (rewrite or pass through)")
    if is_cover and not (cover_url or cover_feature_id):
        raise ValueError("music-cover model requires --cover-url or --cover-feature-id")

    if is_cover:
        # Cover mode: prompt describes target style/voice (e.g. "Jazz, female vocal, sax, slow")
        body["prompt"] = prompt
        if cover_url:
            body["audio_url"] = cover_url
        else:
            body["cover_feature_id"] = cover_feature_id
            body["lyrics"] = lyrics
    else:
        # Standard mode
        body["prompt"] = prompt
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


def cover_preprocess(audio_url, model="music-cover", api_key=None):
    """Two-step cover: extract features + structured lyrics from reference audio.
    Returns cover_feature_id (24h valid) and formatted_lyrics (editable).
    """
    url = f"{API_BASE}/v1/music_cover_preprocess"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or load_api_key()}",
    }
    body = {"model": model, "audio_url": audio_url}
    resp = requests.post(url, json=body, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("base_resp", {})
    if status.get("status_code", 0) != 0:
        raise RuntimeError(f"Cover preprocess error {status.get('status_code')}: {status.get('status_msg')}")

    return {
        "cover_feature_id": data.get("cover_feature_id"),
        "formatted_lyrics": data.get("formatted_lyrics"),
        "lyrics_chords": data.get("lyrics_chords"),
        "feature_expiry_hours": 24,
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
    mp = sub.add_parser("music", help="Generate music from lyrics (or cover)")
    mp.add_argument("--prompt", required=True, help="Style/mood description (cover: target style/voice)")
    mp.add_argument("--lyrics", default=None, help="Lyrics text (or --lyrics-file). Cover two-step: required when using --cover-feature-id")
    mp.add_argument("--lyrics-file", default=None, help="File containing lyrics")
    mp.add_argument("--output", "-o", required=True, help="Output MP3 file")
    mp.add_argument("--model", default="music-2.6", choices=["music-2.6", "music-2.6-free", "music-cover", "music-cover-free"])
    mp.add_argument("--instrumental", action="store_true", help="Generate instrumental only (standard mode)")
    mp.add_argument("--lyrics-optimizer", action="store_true", help="Auto-generate lyrics from prompt (standard mode)")
    mp.add_argument("--sample-rate", type=int, default=44100)
    mp.add_argument("--bitrate", type=int, default=256000)
    mp.add_argument("--format", default="mp3", choices=["mp3", "wav", "pcm"])
    # Cover-specific
    mp.add_argument("--cover-url", default=None, help="[Cover] Reference audio URL (one-step mode, 6s-6min, <=50MB)")
    mp.add_argument("--cover-feature-id", default=None, help="[Cover] Preprocessed feature id (two-step mode, 24h valid)")

    # preprocess subcommand (cover two-step)
    pp = sub.add_parser("preprocess", help="[Cover] Extract features+lyrics from reference audio (two-step setup)")
    pp.add_argument("--cover-url", required=True, help="Reference audio URL")
    pp.add_argument("--model", default="music-cover", choices=["music-cover", "music-cover-free"])
    pp.add_argument("--output", "-o", default=None, help="Output JSON file (default: stdout)")

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
            cover_url=args.cover_url,
            cover_feature_id=args.cover_feature_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "preprocess":
        result = cover_preprocess(args.cover_url, model=args.model)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"Saved to {args.output}", file=sys.stderr)
            print(f"cover_feature_id: {result['cover_feature_id']}")
            print(f"formatted_lyrics preview: {(result['formatted_lyrics'] or '')[:200]}...")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
