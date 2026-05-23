#!/usr/bin/env python3
"""
Upload a file to Cloudflare R2 (S3-compatible) and print a pre-signed download URL.
Pre-sign expires in 30 days. Object key format: YYYY-MM-DD/voice-studio/<theme>/<name>
"""
import argparse, boto3, datetime, os
from pathlib import Path

ENDPOINT = 'https://fd978dbd73c3c86b0939206710f90843.r2.cloudflarestorage.com'
ACCESS_KEY = '7a963141031920d7602e3abce763de41'
SECRET_KEY = '95e32fb2e1a96a471afef93b445de7043df1445b9f677d829186a7cb0f193586'
BUCKET = 'openclaw'


def safe_slug(name: str) -> str:
    """Clean string for use in object key: strip, spaces→hyphens, remove unsafe chars."""
    return ''.join(c if c.isalnum() or c in ('.', '-', '_') else '-' for c in name.strip()).strip('-')


def upload_file(file_path: str, theme: str, name: str = None) -> str:
    """Upload file and return pre-signed URL valid for 30 days."""
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {file_path}")

    date_str = datetime.date.today().strftime('%Y-%m-%d')
    theme_slug = safe_slug(theme)
    slug = safe_slug(name or src.name)
    object_key = f"{date_str}/voice-studio/{theme_slug}/{slug}"

    client = boto3.client(
        's3',
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name='auto',
    )

    client.upload_file(str(src), BUCKET, object_key)

    url = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': BUCKET, 'Key': object_key},
        ExpiresIn=7 * 24 * 3600,  # R2 max 7 days
    )
    return url


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--theme', required=True, help='Theme/slug for path grouping')
    ap.add_argument('--name', default=None, help='Final object name (default: source filename)')
    args = ap.parse_args()

    url = upload_file(args.file, theme=args.theme, name=args.name)
    print(url)