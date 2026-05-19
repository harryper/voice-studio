#!/usr/bin/env python3
"""Generate short narration with Xiaomi MiMo TTS.

Default voice is the built-in 白桦 voice. Pass --ref to use the legacy
voiceclone reference audio path.
"""
import argparse, base64, json, os, re, sys
from pathlib import Path

import requests
try:
    import json5
except Exception:
    json5 = None

DEFAULT_REF = "/root/.openclaw/workspace/tmp/audio-tone/voice_ref_cleanish_60_88.wav"
DEFAULT_VOICE = "白桦"
DEFAULT_MODEL = "mimo-v2.5-tts"
DEFAULT_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")


def load_config(path):
    text = Path(path).read_text(encoding="utf-8")
    return json5.loads(text) if json5 else json.loads(text)


def normalize_text(text: str) -> str:
    text = re.sub(r"^#\s*", "", text, flags=re.M)
    # Add visible breathing points for the TTS model without over-fragmenting.
    text = text.replace("。", "。\n")
    text = text.replace("？", "？\n")
    text = text.replace("！", "！\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="Input text/markdown file")
    ap.add_argument("--out", required=True, help="Output audio path (.mp3 or .wav)")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="Built-in MiMo voice name; default: 白桦")
    ap.add_argument("--model", default=None, help="MiMo TTS model; default: config model or mimo-v2.5-tts")
    ap.add_argument("--ref", default=None, help="Optional legacy reference voice wav; when set, uses voiceclone model")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="OpenClaw config path")
    ap.add_argument("--format", default=None, choices=["mp3", "wav"], help="Output format; inferred from --out when omitted")
    ap.add_argument("--style", default=(
        "请用白桦音色朗读 assistant 消息中的正文。要求：语速比普通朗读慢约15%到20%；"
        "句间停顿清楚，段落之间留白更松；整体是低沉、贴耳、克制的科普助眠播客感；"
        "不要快读，不要播音腔，不要情绪夸张。只朗读 assistant 消息中的正文，不要解释，不要重复。"
    ))
    args = ap.parse_args()

    text = normalize_text(Path(args.text).read_text(encoding="utf-8"))

    cfg = load_config(args.config)
    prov = cfg["messages"]["tts"]["providers"]["xiaomi"]
    api_key = prov["apiKey"]
    base_url = prov["baseUrl"].rstrip("/")
    fmt = args.format or Path(args.out).suffix.lower().lstrip(".") or "mp3"
    if args.ref:
        ref_b64 = base64.b64encode(Path(args.ref).read_bytes()).decode("ascii")
        voice = "data:audio/wav;base64," + ref_b64
        model = args.model or "mimo-v2.5-tts-voiceclone"
    else:
        voice = args.voice
        model = args.model or prov.get("model") or DEFAULT_MODEL

    payload = {
        "model": model,
        "audio": {"voice": voice, "format": fmt},
        "messages": [
            {"role": "user", "content": args.style},
            {"role": "assistant", "content": text},
        ],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(base_url + "/chat/completions", headers=headers, json=payload, timeout=300)
    if r.status_code != 200:
        print(r.text[:4000], file=sys.stderr)
        r.raise_for_status()
    data = r.json()
    audio_b64 = data["choices"][0]["message"]["audio"]["data"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(audio_b64))

    # Save redacted API metadata next to output for debugging/reuse.
    data["choices"][0]["message"]["audio"]["data"] = f"<base64 audio omitted; len={len(audio_b64)}>"
    out.with_suffix(out.suffix + ".response.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
