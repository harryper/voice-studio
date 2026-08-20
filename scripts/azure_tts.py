#!/usr/bin/env python3
"""
Azure Speech TTS 合成脚本（REST API）

用法:
  python3 azure_tts.py --text "你好世界" -o hello.mp3
  python3 azure_tts.py --text "你好" --voice zh-CN-XiaoxiaoNeural -o hello.mp3
  python3 azure_tts.py --text "平静地说" --style calm --styledegree 1.5 -o calm.mp3
  python3 azure_tts.py --file input.txt --voice zh-CN-YunzeNeural -o output.mp3
  python3 azure_tts.py --text "慢速" --rate -20% --pitch -5% -o slow.mp3

常用中文音色:
  zh-CN-YunzeNeural      云泽  中年平静温和
  zh-CN-YunxiNeural      云希  年轻沉稳播报
  zh-CN-YunjianNeural    云健  新闻播报
  zh-CN-YunyangNeural    云扬  专业播报
  zh-CN-YunfengNeural    云枫  深沉成熟
  zh-CN-XiaoxiaoNeural   晓晓  女声温暖自然
  zh-CN-XiaoyiNeural     晓依  女声活泼年轻
  zh-CN-XiaochenNeural   晓辰  女声温柔亲切

Style (部分音色支持):
  calm / gentle / sad / serious / cheerful / angry / fearful / depressed
"""
import argparse
import sys
import os
import requests
from xml.sax.saxutils import escape as xml_escape

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(SCRIPT_DIR, "azure_speech_key.txt")
DEFAULT_REGION = "eastasia"
DEFAULT_VOICE = "zh-CN-YunzeNeural"
DEFAULT_FORMAT = "audio-48khz-96kbitrate-mono-mp3"

MSTTS_NS = 'xmlns:mstts="http://www.w3.org/2001/mstts"'


def load_key():
    with open(KEY_FILE, "r") as f:
        return f.read().strip()


def get_token(key, region):
    url = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    resp = requests.post(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Token 获取失败: {resp.status_code} {resp.text[:200]}")
    return resp.text


def build_ssml(text, voice, lang, style=None, styledegree=None,
               rate=None, pitch=None, volume=None, pause_ms=None):
    """构建 SSML，按需注入 prosody / express-as / break 标签。"""

    # 内层：文本内容（XML 转义，避免文本里出现 & < > 触发 Azure SSML 解析失败）
    inner = xml_escape(text)

    # 如果有停顿，按段落（双换行）拆开插入 break
    if pause_ms and pause_ms > 0:
        parts = [p.strip() for p in inner.split("\n\n") if p.strip()]
        if len(parts) > 1:
            inner = f'\n    <break time="{pause_ms}ms"/>\n    '.join(parts)

    # express-as（情感/风格）
    if style:
        degree_attr = f' styledegree="{styledegree}"' if styledegree else ""
        inner = f'<mstts:express-as style="{style}"{degree_attr}>\n    {inner}\n  </mstts:express-as>'

    # prosody（语速/音调/音量）
    prosody_parts = []
    if rate:
        prosody_parts.append(f'rate="{rate}"')
    if pitch:
        prosody_parts.append(f'pitch="{pitch}"')
    if volume:
        prosody_parts.append(f'volume="{volume}"')
    if prosody_parts:
        attrs = " ".join(prosody_parts)
        inner = f'<prosody {attrs}>\n    {inner}\n  </prosody>'

    # 需要 mstts 命名空间时加到 <speak> 标签
    ns = f" {MSTTS_NS}" if style else ""

    ssml = f"""<speak version="1.0" xml:lang="{lang}"{ns}>
  <voice name="{voice}">
    {inner}
  </voice>
</speak>"""
    return ssml


def synthesize(text, output, voice=DEFAULT_VOICE, lang="zh-CN",
               region=DEFAULT_REGION, output_format=DEFAULT_FORMAT,
               style=None, styledegree=None, rate=None, pitch=None,
               volume=None, pause_ms=None, key=None):
    if key is None:
        key = load_key()
    token = get_token(key, region)

    ssml = build_ssml(text, voice, lang, style, styledegree,
                      rate, pitch, volume, pause_ms)

    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": output_format,
        "User-Agent": "openclaw-azure-tts"
    }

    resp = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"合成失败: {resp.status_code} {resp.text[:300]}")

    with open(output, "wb") as f:
        f.write(resp.content)
    return len(resp.content)


def main():
    p = argparse.ArgumentParser(description="Azure Speech TTS")
    p.add_argument("--text", help="合成文本")
    p.add_argument("--file", help="从文件读取文本")
    p.add_argument("--output", "-o", required=True, help="输出文件路径")

    # 音色/语言
    p.add_argument("--voice", default=DEFAULT_VOICE, help=f"音色 (默认: {DEFAULT_VOICE})")
    p.add_argument("--lang", default="zh-CN", help="语言 (默认: zh-CN)")
    p.add_argument("--region", default=DEFAULT_REGION, help=f"区域 (默认: {DEFAULT_REGION})")
    p.add_argument("--format", default=DEFAULT_FORMAT, help="输出格式")
    p.add_argument("--key", help="直接提供 key")

    # 情感/风格
    p.add_argument("--style", help="情感风格: calm/gentle/sad/serious/cheerful/angry/fearful/depressed")
    p.add_argument("--styledegree", type=float, help="风格强度 0.01~2 (默认 1)")

    # 语速/音调/音量
    p.add_argument("--rate", help='语速, 如 "-10%" 或 "slow/medium/fast"')
    p.add_argument("--pitch", help='音调, 如 "-5%" 或 "low/medium/high"')
    p.add_argument("--volume", help='音量, 如 "80" 或 "soft/medium/loud"')

    # 段落停顿
    p.add_argument("--pause-ms", type=int, help="段落间停顿(毫秒), 按双换行分段")

    args = p.parse_args()

    if args.text:
        text = args.text
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read().strip()
    else:
        print("错误: 必须提供 --text 或 --file", file=sys.stderr)
        sys.exit(1)

    size = synthesize(
        text, args.output,
        voice=args.voice, lang=args.lang, region=args.region,
        output_format=args.format, key=args.key,
        style=args.style, styledegree=args.styledegree,
        rate=args.rate, pitch=args.pitch, volume=args.volume,
        pause_ms=args.pause_ms
    )
    print(f"✅ 合成完成: {args.output} ({size} bytes)")


if __name__ == "__main__":
    main()
