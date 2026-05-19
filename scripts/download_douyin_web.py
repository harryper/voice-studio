#!/usr/bin/env python3
"""Best-effort Douyin web downloader without TikHub.

It follows a v.douyin.com short link, extracts a video_id from the share page, then downloads
through aweme.snssdk.com with imageio_ffmpeg/ffmpeg.
"""
import argparse, re, subprocess, sys
from pathlib import Path
from urllib.parse import unquote

import requests
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def ffmpeg_exe():
    return imageio_ffmpeg.get_ffmpeg_exe() if imageio_ffmpeg else "ffmpeg"


def extract_aweme_id(url: str) -> str:
    m = re.search(r"(?:video|share/video)/(\d+)", url)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--name", default="douyin_video")
    args = ap.parse_args()
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)

    s = requests.Session(); s.headers.update({"User-Agent": UA})
    r = s.get(args.url, allow_redirects=True, timeout=30)
    final_url = r.url
    aweme_id = extract_aweme_id(final_url)
    if not aweme_id:
        print(f"Could not extract aweme id from {final_url}", file=sys.stderr)
        sys.exit(2)

    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    page = s.get(share_url, timeout=30).text
    (out_dir / "share.html").write_text(page, encoding="utf-8")

    m = re.search(r'video_id=([A-Za-z0-9_\-]+)', page)
    if not m:
        m = re.search(r'"uri":"(v[0-9A-Za-z_\-]+)"', page)
    if not m:
        print("Could not find video_id/uri in share page", file=sys.stderr)
        sys.exit(3)
    video_id = m.group(1)

    play_url = f"https://aweme.snssdk.com/aweme/v1/playwm/?line=0&logo_name=aweme_diversion_search&ratio=720p&video_id={video_id}"
    (out_dir / "video_url.txt").write_text(play_url, encoding="utf-8")
    out = out_dir / f"{args.name}.mp4"
    cmd = [ffmpeg_exe(), "-y", "-headers", "User-Agent: Mozilla/5.0\r\nReferer: https://www.iesdouyin.com/\r\n", "-i", play_url, "-c", "copy", str(out)]
    subprocess.run(cmd, check=True)
    print(out)


if __name__ == "__main__":
    main()
