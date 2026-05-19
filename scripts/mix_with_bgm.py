#!/usr/bin/env python3
"""Mix narration with a looping/cropped BGM bed."""
import argparse, subprocess, sys
from pathlib import Path

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

DEFAULT_BGM = os.path.expanduser("~/.openclaw/workspace/skills/voice-studio/assets/bgm_default.mp3")


def ffmpeg_exe():
    if imageio_ffmpeg:
        return imageio_ffmpeg.get_ffmpeg_exe()
    return "ffmpeg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", required=True, help="Narration audio")
    ap.add_argument("--bgm", default=DEFAULT_BGM, help="BGM audio")
    ap.add_argument("--out", required=True, help="Output mp3")
    ap.add_argument("--voice-volume", type=float, default=0.90)
    ap.add_argument("--bgm-volume", type=float, default=0.05)
    ap.add_argument("--bitrate", default="192k")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    filt = (
        f"[0:a]volume={args.voice_volume}[voice];"
        f"[1:a]volume={args.bgm_volume}[bgm];"
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[a]"
    )
    cmd = [
        ffmpeg_exe(), "-y",
        "-i", args.voice,
        "-stream_loop", "-1", "-i", args.bgm,
        "-filter_complex", filt,
        "-map", "[a]",
        "-ar", "44100", "-ac", "2",
        "-c:a", "libmp3lame", "-b:a", args.bitrate,
        "-id3v2_version", "3", "-write_xing", "0",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    print(out)


if __name__ == "__main__":
    main()
