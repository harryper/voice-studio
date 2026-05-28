#!/usr/bin/env python3
"""Rename historical voice-studio R2 objects to readable names.

R2/S3 has no real rename operation, so this script copies each known object to a
new readable key, updates local job JSON URLs, then optionally deletes old keys.
"""
import argparse
import copy
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3

from upload_to_oss import ACCESS_KEY, BUCKET, ENDPOINT, SECRET_KEY

SKILL_DIR = Path(__file__).resolve().parents[1]
JOB_ROOTS = [SKILL_DIR / "jobs", SKILL_DIR / "archive"]


def safe_name(value, fallback="voice", max_len=80):
    value = (value or "").strip()
    if not value:
        value = fallback
    chars = []
    for char in value:
        if char.isalnum() or char in (".", "_", "-"):
            chars.append(char)
        elif char.isspace() or char in "，。、《》【】（）()[]{}：:；;、/\\|!?！？“”\"'`~@#$%^&*+=,":
            chars.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(chars)).strip("-._")
    return (slug[:max_len].strip("-._") or fallback)


def job_name_fragment(job, fallback="untitled"):
    for key in ("title", "theme", "edited_script", "script", "edited_lyrics", "lyrics"):
        text = (job.get(key) or "").strip()
        if text:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
            return safe_name(first_line, fallback=fallback, max_len=48)
    return fallback


def parse_key(url):
    path = unquote(urlparse(url).path).lstrip("/")
    if path.startswith(f"{BUCKET}/"):
        return path[len(BUCKET) + 1 :]
    return path


def infer_date_and_timestamp(old_key, job):
    match = re.search(r"(\d{4}-\d{2}-\d{2})", old_key)
    if match:
        date_folder = match.group(1)
    else:
        compact = re.search(r"(20\d{6})", old_key)
        if compact:
            raw = compact.group(1)
            date_folder = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        else:
            created = (job.get("created_at") or "")[:10]
            date_folder = created if re.match(r"20\d{2}-\d{2}-\d{2}", created) else dt.date.today().isoformat()

    stamp = None
    match = re.search(r"(20\d{6}-\d{6})", old_key)
    if match:
        stamp = match.group(1)
    else:
        created = job.get("created_at")
        if created:
            try:
                stamp = dt.datetime.fromisoformat(created).strftime("%Y%m%d-%H%M%S")
            except ValueError:
                pass
    return date_folder, stamp or date_folder.replace("-", "") + "-000000"


def infer_artifact(field, old_key, job):
    basename = Path(old_key).name.lower()
    ext = Path(urlparse(old_key).path).suffix.lower().lstrip(".") or "mp3"
    if "script_url" in field or basename.endswith(".txt") or "script" in basename:
        return "script", "txt"
    if "lyrics_url" in field or "lyrics" in basename:
        return "lyrics", "txt"
    if job.get("mode") == "music" or "music" in basename:
        return "song", ext
    if "voice_url" in field or "-voice" in basename:
        return "voice-only", ext
    if "final_url" in field or "custom-" in basename or "-final" in basename:
        return "mixed-final" if job.get("bgm", True) else "voice-final", ext
    if "audio_url" in field:
        return "song" if job.get("mode") == "music" else "mixed-final", ext
    return "file", ext


def infer_run_suffix(field, old_key):
    match = re.search(r"voice_runs\[(\d+)\]", field)
    if match:
        return f"r{int(match.group(1)) + 1}"
    match = re.search(r"run-\d{8}-\d{6}-(\d+)", old_key)
    if match:
        return f"r{match.group(1)}"
    return None


def new_key_for(job, field, old_key):
    title = job_name_fragment(job, fallback="music" if job.get("mode") == "music" else "voice")
    date_folder, timestamp = infer_date_and_timestamp(old_key, job)
    artifact, ext = infer_artifact(field, old_key, job)
    short_id = safe_name((job.get("id") or "job")[:8], fallback="job", max_len=16)
    run_suffix = infer_run_suffix(field, old_key)
    id_part = f"{short_id}-{run_suffix}" if run_suffix else short_id
    mode = "music" if job.get("mode") == "music" else "voice"
    filename = f"{mode}-{title}-{timestamp}-{id_part}-{artifact}.{ext}"
    return f"{date_folder}/voice-studio/{title}/{filename}"


def iter_url_refs(job):
    for key in ("audio_url", "final_url", "voice_url", "script_url", "lyrics_url"):
        if job.get(key):
            yield (key,), key, job[key]
    for index, run in enumerate(job.get("voice_runs") or []):
        for key in ("script_url", "voice_url", "final_url"):
            if run.get(key):
                yield ("voice_runs", index, key), f"voice_runs[{index}].{key}", run[key]


def set_ref(job, path, value):
    if len(path) == 1:
        job[path[0]] = value
    else:
        job[path[0]][path[1]][path[2]] = value


def presign(client, key):
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=7 * 24 * 3600,
    )


def load_jobs():
    files = []
    for root in JOB_ROOTS:
        if root.exists():
            files.extend(sorted(root.rglob("*.json")))
    for path in files:
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"SKIP invalid json {path}: {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Copy objects and update JSON files.")
    parser.add_argument("--delete-old", action="store_true", help="Delete old object keys after successful copy.")
    args = parser.parse_args()

    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
    )

    backup_dir = SKILL_DIR / "backups" / ("oss-rename-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    planned = copied = updated_files = deleted = missing = skipped = 0
    delete_candidates = set()

    for path, job in load_jobs():
        original_job = copy.deepcopy(job)
        changed = False
        for ref_path, field, url in list(iter_url_refs(job)):
            old_key = parse_key(url)
            new_key = new_key_for(job, field, old_key)
            if old_key == new_key:
                skipped += 1
                continue
            planned += 1
            print(f"{'APPLY' if args.apply else 'DRY'} {old_key} -> {new_key}")
            if not args.apply:
                continue
            try:
                client.head_object(Bucket=BUCKET, Key=old_key)
            except Exception as exc:
                missing += 1
                print(f"  MISSING old object, keep existing URL: {old_key} ({exc.__class__.__name__})")
                continue
            client.copy_object(
                Bucket=BUCKET,
                Key=new_key,
                CopySource={"Bucket": BUCKET, "Key": old_key},
            )
            copied += 1
            set_ref(job, ref_path, presign(client, new_key))
            changed = True
            if args.delete_old:
                delete_candidates.add(old_key)
        if args.apply and changed:
            backup_path = backup_dir / path.relative_to(SKILL_DIR)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            updated_files += 1
        elif args.apply and job != original_job:
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply and args.delete_old:
        for old_key in sorted(delete_candidates):
            client.delete_object(Bucket=BUCKET, Key=old_key)
            deleted += 1

    print(
        json.dumps(
            {
                "planned": planned,
                "copied": copied,
                "updated_files": updated_files,
                "deleted_old": deleted,
                "missing_old": missing,
                "skipped_same_name": skipped,
                "backup_dir": str(backup_dir) if args.apply and updated_files else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
