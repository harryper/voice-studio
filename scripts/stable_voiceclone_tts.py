#!/usr/bin/env python3
"""Reliable segmented Xiaomi MiMo narration generation for longer sleep audio.

Splits text into small semantic chunks, retries short/bad generations, then stitches
chunks with tiny silences. This avoids MiMo long-audio degradation while keeping
voice/style reasonably consistent across chunks.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import shutil
from pathlib import Path

import requests
try:
    import json5
except Exception:
    json5 = None
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

DEFAULT_REF = "/root/.openclaw/workspace/tmp/audio-tone/voice_ref_cleanish_60_88.wav"
DEFAULT_VOICE = "白桦"
DEFAULT_MODEL = "mimo-v2.5-tts"
DEFAULT_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
DEFAULT_STYLE = (
    "请用白桦音色朗读 assistant 消息中的正文。要求：语速比普通朗读慢约15%到20%；"
    "句间停顿清楚，段落之间留白更松；整体是低沉、贴耳、克制的科普助眠播客感；"
    "不要快读，不要播音腔，不要情绪夸张。只朗读 assistant 消息中的正文，不要解释，不要重复。"
)


def ffmpeg_exe():
    if imageio_ffmpeg:
        return imageio_ffmpeg.get_ffmpeg_exe()
    return "ffmpeg"


def load_config(path):
    text = Path(path).read_text(encoding="utf-8")
    return json5.loads(text) if json5 else json.loads(text)


def normalize_text(text: str) -> str:
    text = re.sub(r"^#\s*", "", text, flags=re.M)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def char_len(s: str) -> int:
    return len(re.sub(r"\s+", "", s))


def split_text(text: str, max_chars: int):
    """Group natural paragraphs into TTS chunks.

    Paragraph boundaries are preferred, but not every paragraph becomes a chunk.
    Adjacent short paragraphs are grouped until the next paragraph would exceed
    max_chars. Only split inside a paragraph when that paragraph alone exceeds
    max_chars. This keeps a ~4000-char script around 6 chunks when max_chars is
    about 650-750, reducing independent TTS performances and audible seams.
    """
    parts = []
    buf = ""
    for para in re.split(r"\n\s*\n", normalize_text(text)):
        para = para.strip()
        if not para:
            continue
        if char_len(para) <= max_chars:
            candidate = (buf.rstrip() + "\n\n" + para).strip() if buf else para
            if buf and char_len(candidate) > max_chars:
                parts.append(buf.strip())
                buf = para
            else:
                buf = candidate
            continue
        if buf:
            parts.append(buf.strip())
            buf = ""
        sentences = [x.strip() for x in re.split(r"(?<=[。！？!?；;])", para) if x.strip()]
        sent_buf = ""
        for sent in sentences or [para]:
            if char_len(sent) > max_chars:
                # Hard fallback for very long punctuation-free text.
                for i in range(0, len(sent), max_chars):
                    piece = sent[i:i + max_chars].strip()
                    if piece:
                        if sent_buf:
                            parts.append(sent_buf.strip())
                            sent_buf = ""
                        parts.append(piece)
                continue
            candidate = (sent_buf + sent).strip()
            if sent_buf and char_len(candidate) > max_chars:
                parts.append(sent_buf.strip())
                sent_buf = sent
            else:
                sent_buf = candidate
        if sent_buf:
            parts.append(sent_buf.strip())
    if buf:
        parts.append(buf.strip())
    return parts


def probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        cmd = [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
            return float(out)
        except Exception:
            pass
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", str(path)]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
    except Exception:
        out = getattr(sys.exc_info()[1], "output", "") or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def write_pcm16_as_audio(pcm_bytes: bytes, out: Path, bitrate: str):
    """Convert MiMo streaming PCM16LE mono/24kHz bytes into the requested audio file."""
    out.parent.mkdir(parents=True, exist_ok=True)
    codec_args = ["-c:a", "pcm_s16le"] if out.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-b:a", bitrate]
    cmd = [
        ffmpeg_exe(), "-y",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
        "-ar", "44100", "-ac", "2",
        *codec_args,
        str(out),
    ]
    subprocess.run(cmd, input=pcm_bytes, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def iter_sse_json(response):
    """Yield JSON objects from an OpenAI-compatible text/event-stream response."""
    for raw in response.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def extract_stream_audio_b64(event):
    choices = event.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    audio = delta.get("audio") or {}
    if isinstance(audio, dict):
        return audio.get("data")
    return None


def generate_chunk(text, out, voice, model, cfg, fmt, style, timeout, min_completion_tokens, min_seconds, retries, stream_pcm16=False, bitrate="192k"):
    prov = cfg["messages"]["tts"]["providers"]["xiaomi"]
    api_key = prov["apiKey"]
    base_url = prov["baseUrl"].rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "model": model,
        "audio": {"voice": voice, "format": "pcm16" if stream_pcm16 else fmt},
        "messages": [
            {"role": "user", "content": style},
            {"role": "assistant", "content": text},
        ],
        "stream": bool(stream_pcm16),
    }
    last_error = None
    for attempt in range(1, retries + 2):
        try:
            r = requests.post(base_url + "/chat/completions", headers=headers, json=payload, timeout=timeout, stream=stream_pcm16)
            if r.status_code != 200:
                raise RuntimeError(r.text[:1200])
            if stream_pcm16:
                pcm = bytearray()
                redacted_events = []
                for event in iter_sse_json(r):
                    audio_b64 = extract_stream_audio_b64(event)
                    if audio_b64:
                        pcm.extend(base64.b64decode(audio_b64))
                        event = json.loads(json.dumps(event))
                        event["choices"][0]["delta"]["audio"]["data"] = f"<base64 pcm16 omitted; len={len(audio_b64)}>"
                    redacted_events.append(event)
                if not pcm:
                    raise RuntimeError("stream returned no pcm16 audio")
                write_pcm16_as_audio(bytes(pcm), out, bitrate)
                out.with_suffix(out.suffix + ".response.json").write_text(json.dumps({
                    "mode": "stream-pcm16",
                    "pcm_bytes": len(pcm),
                    "events": redacted_events,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                completion_tokens = 0
            else:
                data = r.json()
                audio_b64 = data["choices"][0]["message"]["audio"]["data"]
                out.write_bytes(base64.b64decode(audio_b64))
                meta = data.copy()
                meta["choices"][0]["message"]["audio"]["data"] = f"<base64 audio omitted; len={len(audio_b64)}>"
                out.with_suffix(out.suffix + ".response.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                usage = data.get("usage") or {}
                completion_tokens = int(usage.get("completion_tokens") or 0)
            duration = probe_duration(out)
            if completion_tokens and completion_tokens < min_completion_tokens:
                raise RuntimeError(f"too few completion_tokens={completion_tokens}")
            if duration < min_seconds:
                raise RuntimeError(f"audio too short: {duration:.2f}s < {min_seconds:.2f}s")
            return {"path": str(out), "duration": duration, "completion_tokens": completion_tokens, "attempt": attempt}
        except Exception as e:
            last_error = e
            if attempt <= retries:
                time.sleep(1.5 * attempt)
            else:
                break
    raise RuntimeError(f"failed to generate chunk {out.name}: {last_error}")


def make_silence(path: Path, seconds: float, bitrate: str):
    codec_args = ["-c:a", "pcm_s16le"] if path.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-b:a", bitrate]
    cmd = [
        ffmpeg_exe(), "-y", "-f", "lavfi", "-t", str(seconds),
        "-i", "anullsrc=r=44100:cl=stereo", *codec_args, str(path)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stitch(chunks, out: Path, pause_seconds: float, bitrate: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cosmic-stitch-") as td:
        td = Path(td)
        chunk_suffix = Path(chunks[0]).suffix.lower() if chunks else ".wav"
        silence = td / ("silence" + chunk_suffix)
        make_silence(silence, pause_seconds, bitrate)
        concat = td / "concat.txt"
        lines = []
        for i, chunk in enumerate(chunks):
            lines.append(f"file '{Path(chunk).resolve()}'")
            if i != len(chunks) - 1 and pause_seconds > 0:
                lines.append(f"file '{silence.resolve()}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", bitrate,
            "-id3v2_version", "3", "-write_xing", "0", str(out)
        ]
        subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description="Stable segmented Xiaomi MiMo TTS")
    ap.add_argument("--text", required=True, help="Input text/markdown file")
    ap.add_argument("--out", required=True, help="Output stitched narration audio")
    ap.add_argument("--workdir", default=None, help="Directory for chunk files; default: <out>.chunks")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="Built-in MiMo voice name; default: 白桦")
    ap.add_argument("--model", default=None, help="MiMo TTS model; default: config model or mimo-v2.5-tts")
    ap.add_argument("--ref", default=None, help="Optional legacy reference voice wav; when set, uses voiceclone model instead of built-in voice")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="OpenClaw config path")
    ap.add_argument("--format", default=None, choices=["mp3", "wav"], help="Non-streaming chunk output format; inferred from --out when omitted. Streaming PCM16 chunks are stored as WAV for final one-pass encoding.")
    ap.add_argument("--max-chars", type=int, default=700, help="Maximum non-space chars per TTS chunk; adjacent paragraphs are grouped under this ceiling")
    ap.add_argument("--pause-seconds", type=float, default=0.45, help="Silence inserted between chunks")
    ap.add_argument("--min-completion-tokens", type=int, default=120, help="Retry when API metadata suggests a truncated response")
    ap.add_argument("--min-seconds-per-100-chars", type=float, default=8.0, help="Retry when generated audio is suspiciously short")
    ap.add_argument("--retries", type=int, default=1, help="Retries per chunk after first failure")
    ap.add_argument("--timeout", type=int, default=180, help="HTTP timeout seconds per chunk")
    ap.add_argument("--bitrate", default="192k")
    ap.add_argument(
        "--stream-pcm16",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use MiMo official streaming pcm16 mode per chunk, then convert locally (default: on)",
    )
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--plan-only", action="store_true", help="Only print chunk plan; do not call API")
    args = ap.parse_args()

    text = normalize_text(Path(args.text).read_text(encoding="utf-8"))
    chunks = split_text(text, args.max_chars)
    if not chunks:
        raise SystemExit("empty input text")
    plan = [{"index": i + 1, "chars": char_len(c), "preview": c[:80]} for i, c in enumerate(chunks)]
    print(json.dumps({"chunks": plan, "total_chars": char_len(text)}, ensure_ascii=False, indent=2))
    if args.plan_only:
        return

    fmt = args.format or Path(args.out).suffix.lower().lstrip(".") or "mp3"
    chunk_fmt = "wav" if args.stream_pcm16 else fmt
    out = Path(args.out)
    workdir = Path(args.workdir) if args.workdir else out.with_suffix(out.suffix + ".chunks")
    workdir.mkdir(parents=True, exist_ok=True)

    cfg = load_config(args.config)
    prov = cfg["messages"]["tts"]["providers"]["xiaomi"]
    if args.ref:
        ref_b64 = base64.b64encode(Path(args.ref).read_bytes()).decode("ascii")
        voice = "data:audio/wav;base64," + ref_b64
        model = args.model or "mimo-v2.5-tts-voiceclone"
    else:
        voice = args.voice
        model = args.model or prov.get("model") or DEFAULT_MODEL

    generated = []
    manifest = {"source": str(Path(args.text).resolve()), "out": str(out.resolve()), "chunks": []}
    for i, chunk in enumerate(chunks, 1):
        chunk_out = workdir / f"chunk_{i:02d}.{chunk_fmt}"
        min_seconds = max(2.5, char_len(chunk) / 100.0 * args.min_seconds_per_100_chars)
        info = generate_chunk(
            chunk, chunk_out, voice, model, cfg, chunk_fmt, args.style, args.timeout,
            args.min_completion_tokens, min_seconds, args.retries,
            stream_pcm16=args.stream_pcm16, bitrate=args.bitrate
        )
        info.update({"index": i, "chars": char_len(chunk), "text": chunk})
        manifest["chunks"].append(info)
        generated.append(chunk_out)
        print(f"chunk {i}/{len(chunks)} ok: {info['duration']:.1f}s", file=sys.stderr)

    stitch(generated, out, args.pause_seconds, args.bitrate)
    manifest["stitched_duration"] = probe_duration(out)
    out.with_suffix(out.suffix + ".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
