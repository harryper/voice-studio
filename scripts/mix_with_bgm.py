#!/usr/bin/env python3
"""Mix narration with a low-volume BGM bed. Simple mixing, no normalization."""
import argparse, os, re, subprocess, sys
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


def detect_duration(ff, path):
    r = subprocess.run([ff, "-i", path, "-f", "null", "-"],
        capture_output=True, text=True)
    for line in r.stderr.split("\n"):
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
        if m:
            h, mi, sec = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + sec
    return None


def main():
    ap = argparse.ArgumentParser(description="Mix narration with ambient BGM")
    ap.add_argument("--voice", required=True)
    ap.add_argument("--bgm", default=DEFAULT_BGM)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bgm-volume", type=float, default=0.06,
                    help="BGM volume as linear multiplier (default 0.03, i.e. 3%%)")
    args = ap.parse_args()

    ff = ffmpeg_exe()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    dur = detect_duration(ff, args.voice)
    if dur is None:
        print("ERROR: could not detect voice duration", file=sys.stderr)
        sys.exit(1)

    bgm_vol = args.bgm_volume
    dur_sec = dur

    # Simple approach:
    # 1. Loop BGM at input level with -stream_loop -1
    # 2. Trim looped BGM to exactly the voice duration
    # 3. Mix with amix (duration=first keeps voice length)
    # amix sums levels then divides by 2 — we compensate by doubling the voice
    voice_gain = 2.0  # counter the /2 from amix
    filt = (
        f"[0:a]volume={voice_gain}[v];"
        f"[1:a]volume={bgm_vol},atrim=0:{dur_sec},asetpts=PTS-STARTPTS[a_bgm];"
        f"[v][a_bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )

    cmd = [
        ff, "-y",
        "-i", args.voice,
        "-stream_loop", "-1", "-i", args.bgm,
        "-filter_complex", filt,
        "-map", "[a]",
        "-ar", "44100", "-ac", "2",
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-id3v2_version", "3",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    print(f"OK: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB, {dur_sec:.0f}s), BGM vol={bgm_vol}")


if __name__ == "__main__":
    main()