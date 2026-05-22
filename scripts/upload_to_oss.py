#!/usr/bin/env python3
"""
Upload a file to Cloudflare R2 (S3-compatible) and print a pre-signed download URL.
Pre-sign expires in 30 days. Object key uses YYYY-MM-DD/ prefix.
"""
import argparse, boto3, datetime, os
from pathlib import Path

# R2 credentials from agent-memory
ENDPOINT = 'https://fd978dbd73c3c86b0939206710f90843.r2.cloudflarestorage.com'
ACCESS_KEY = '7a963141031920d7602e3abce763de41'
SECRET_KEY = '95e32fb2e1a96a471afef93b445de7043df1445b9f677d829186a7cb0f193586'
BUCKET = 'openclaw'


def upload_file(file_path: str, folder: str = 'cosmic-sleep', name: str = None) -> str:
    """Upload file and return pre-signed URL valid for 30 days."""
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {file_path}")

    # Object key: YYYY-MM-DD/prefix/name
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    slug = name or src.name
    # ensure clean path: strip leading/trailing spaces, replace spaces with hyphens
    slug = slug.replace(' ', '-')
    object_key = f"{date_str}/{folder}/{slug}"

    client = boto3.client(
        's3',
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name='auto',
    )

    client.upload_file(str(src), BUCKET, object_key)

    # Generate pre-signed URL, 30 days expiry
    url = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': BUCKET, 'Key': object_key},
        ExpiresIn=30 * 24 * 3600,
    )
    return url


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--folder', default='cosmic-sleep')
    ap.add_argument('--name', default=None)
    args = ap.parse_args()

    url = upload_file(args.file, folder=args.folder, name=args.name)
    print(url)