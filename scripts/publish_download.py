#!/usr/bin/env python3
"""Copy a file to the public-downloads tree and print the public URL."""
import argparse, shutil, subprocess, time
from pathlib import Path

ROOT = Path('/root/.openclaw/workspace/public-downloads')
URL_PREFIX = 'http://43.173.67.197:18082'


def server_running():
    try:
        import urllib.request
        urllib.request.urlopen(URL_PREFIX + '/', timeout=2).close()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--folder', required=True)
    ap.add_argument('--name', default=None)
    args = ap.parse_args()
    src = Path(args.file)
    dest_dir = ROOT / args.folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (args.name or src.name)
    shutil.copy2(src, dest)

    if not server_running():
        subprocess.Popen(['python3', '-m', 'http.server', '18082', '--bind', '0.0.0.0'], cwd=str(ROOT), stdout=open(ROOT/'http-18082.log','a'), stderr=subprocess.STDOUT)
        time.sleep(1)
    print(f"{URL_PREFIX}/{args.folder}/{dest.name}")


if __name__ == '__main__':
    main()
