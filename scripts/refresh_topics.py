#!/usr/bin/env python3
"""
刷新"热门主题推荐"：从 NASA、arXiv、ESA 等科学媒体真采原始线索，
调用 OpenClaw 默认模型 MiniMax-M3（走 Anthropic Messages 协议）改写为
"老波"风格的助眠科普中文选题，落到本地 topic_recommendations.json。

行为：
- 拉取 NASA APOD + NASA 新闻 RSS + arXiv astro-ph + ESA Space Science + Quanta + Phys.org 等源
- 把这些原始英文/科学新闻塞进 prompt，让 M3 按老波风格改写为 5 条
- 按 title 去重，prepend 进现有 30 条，cap 在 30
- 任何一步失败（外网抖 / M3 限流 / JSON 解析不出来）都降级到洗牌，绝不静默失败
- 文件不存在时降级到洗牌后写一份空骨架
"""
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# ── 路径 / 配置 ───────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOPICS_PATH = os.path.join(SCRIPT_DIR, '..', 'topic_recommendations.json')
KEY_PATH = os.path.join(SCRIPT_DIR, 'minimax_api_key.txt')

# OpenClaw 默认模型 = minimax/MiniMax-M3（Anthropic Messages 协议）
MINIMAX_BASE = 'https://api.minimaxi.com/anthropic/v1/messages'
MINIMAX_MODEL = 'MiniMax-M3'
ANTHROPIC_VERSION = '2023-06-01'

HTTP_TIMEOUT = 8          # 单个外网请求的硬超时
LLM_TIMEOUT = 40          # M3 一次调用上限（disabled thinking 后实测 16s 完成）
MAX_TOPICS = 30
NEW_TOPICS_PER_RUN = 5
TODAY = datetime.now().strftime('%Y-%m-%d')

# ── 数据源 ────────────────────────────────────────────────────
# 每条 (name, url, kind, count)
# kind: 'apod' 走 NASA APOD JSON；其它统一按 RSS/Atom 解析
SOURCES = [
    {'name': 'NASA APOD',       'kind': 'apod', 'count': 3},
    {'name': 'NASA news',       'kind': 'rss', 'count': 3,
     'url': 'https://www.nasa.gov/news-release/feed/'},
    {'name': 'arXiv astro-ph',  'kind': 'rss', 'count': 3,
     'url': 'https://export.arxiv.org/rss/astro-ph'},
    {'name': 'ESA Space Sci.',  'kind': 'rss', 'count': 2,
     'url': 'https://www.esa.int/rssfeed/Our_Activities/Space_Science'},
    {'name': 'Quanta Magazine', 'kind': 'rss', 'count': 2,
     'url': 'https://www.quantamagazine.org/feed/'},
    {'name': 'Phys.org space',  'kind': 'rss', 'count': 2,
     'url': 'https://phys.org/rss-feed/space-news/'},
]

# ── 抓取层 ────────────────────────────────────────────────────
def _http_get(url, timeout=HTTP_TIMEOUT):
    """带 UA 的简单 GET，失败抛 RuntimeError。"""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'voice-studio-refresh/1.0 (channel:laobo)',
        'Accept': 'application/rss+xml, application/atom+xml, application/json, */*',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get('content-type', '')

def _parse_rss(xml_bytes, source_name, count):
    """RSS/Atom 解析。兼容 title/link/pubDate 缺失/重复命名空间。"""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f'  [warn] {source_name} RSS 解析失败: {e}', file=sys.stderr)
        return items

    # RSS 2.0: <rss><channel><item>
    # Atom:   <feed><entry>
    candidates = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
    for it in candidates:
        title = (it.findtext('title') or
                 it.findtext('{http://www.w3.org/2005/Atom}title') or '').strip()
        link = (it.findtext('link') or
                it.findtext('{http://www.w3.org/2005/Atom}link') or '').strip()
        if not link:
            atom_link = it.find('{http://www.w3.org/2005/Atom}link')
            if atom_link is not None:
                link = atom_link.attrib.get('href', '').strip()
        desc = (it.findtext('description') or
                it.findtext('{http://www.w3.org/2005/Atom}summary') or '').strip()
        if not title:
            continue
        # 简单清理 HTML 标签和多余空白
        desc = re.sub(r'<[^>]+>', ' ', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()
        items.append({
            'title': title[:200],
            'link': link[:300],
            'desc': desc[:280],
            'source': source_name,
        })
        if len(items) >= count:
            break
    return items

def fetch_source(spec):
    """拉一个源；失败返回空列表，错误信息打到 stderr。"""
    if spec['kind'] == 'apod':
        try:
            body, _ = _http_get('https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY&count='
                                + str(spec['count']))
            arr = json.loads(body)
            return [{
                'title': (it.get('title') or '').strip()[:200],
                'link': (it.get('url') or it.get('hdurl') or '').strip()[:300],
                'desc': (it.get('explanation') or '').strip()[:280],
                'source': 'NASA APOD ' + TODAY,
            } for it in arr if it.get('title')]
        except Exception as e:
            print(f'  [warn] NASA APOD 抓取失败: {type(e).__name__}: {e}', file=sys.stderr)
            return []
    if spec['kind'] == 'rss':
        try:
            body, _ = _http_get(spec['url'])
            return _parse_rss(body, spec['name'], spec['count'])
        except Exception as e:
            print(f'  [warn] {spec["name"]} 抓取失败: {type(e).__name__}: {e}', file=sys.stderr)
            return []
    return []

def gather_raw_items():
    """从所有源拉一遍，单源失败不影响其它源。"""
    pool = []
    for spec in SOURCES:
        try:
            got = fetch_source(spec)
            if got:
                print(f'  [fetch] {spec["name"]:20s}  got {len(got)} items', file=sys.stderr)
                pool.extend(got)
        except Exception as e:
            print(f'  [warn] {spec["name"]} unexpected: {e}', file=sys.stderr)
    return pool

# ── 老波风格 prompt ───────────────────────────────────────────
STYLE_GUIDE = '''你是"老波"——一个宇宙科普频道的助眠主播，每天晚上用第二人称"你"和听众聊天。
我们要给听众一个反常识的、让人想继续听的睡前话题，作为 15-20 分钟的中文音频脚本选题。

【标题要求】（15-30 字，**绝对不能直译英文新闻标题**）
- 反常识判断：一句话打破听众的固有印象
- 悬念标签：让人想点开听完
- 常用句式：
  · "如果 X，Y 会怎样？"
  · "你以为 X——其实 Y"
  · "为什么 X？"
  · "X 这么 Y，但 Z"
  · "X 离 Y 还差 Z"
- 例（**风格参考，不是让你照抄**）：
  · 如果太阳突然消失，地球会怎样？
  · 黑洞会死亡吗？
  · 时间为什么只能往前走？
  · 宇宙一直在膨胀，那它到底在往哪里撑？

【angle 要求】（20-40 字）
- 讲明白"这个标题要从哪个画面 / 哪个反常识点切进去"
- 格式参考："从 X 讲到 Y"、"用第一视角讲 X"、"把 X 误解拆掉，讲成 Y"
- 要具体到画面感，不要抽象定义
- 例（**风格参考**）：
  · 从引力波速度讲到 8 分钟后的黑暗和轨道混乱
  · 从熵增和时间箭头讲到一杯水为什么不会自己回到杯子里
  · 把宇宙膨胀从气球误解，讲成空间本身变大的安静恐怖

【category 限定】（从下面选一个最贴的）
太阳系 / 宇宙 / 恒星 / 黑洞 / 时间 / 量子 / 暗物质 / 生命 / 地球 / 观测

【source 字段】
写源名 + 今天的日期，例如 "NASA APOD 2026-06-08" / "arXiv 2026-06-08"

【source_url 字段】
从原报道里选**最像科普大众报道的那一条**链接（不要 PDF 论文原始链接，要 RSS 里给的 html landing）

【硬规则】
- 输出必须是合法 JSON 数组，**第一个字符必须是 `[`，最后一个字符必须是 `]`**
- **exactly 5 个对象**，不多不少
- **5 条必须分散到 5 条不同的原始线索**；不要 5 条都讲同一篇新闻的不同角度
  （提示：[[1]][[2]][[3]] 三个不同线索对应三个不同主题；不要为了看似"都在讲一个主题"而全部引用同一条）
- 如果某条线索不够"反常识 / 不够听完"、适合静眠，可以跳过该线索，选别的
- 中文标点用全角（，。？——）
- **不要任何开头/结尾寒暄、不要 markdown 围栏、不要代码块标记、不要 "以下是..." 引导句**
- 不要复述英文原标题；中文标题要让人想点开听
- 不要承诺疗效、不要医学建议、不要"治愈""根治"
- `source_url` 选线索里给的 html 链接，不要 PDF；源不是网页的给主站 https://science.nasa.gov/
- 字符串里要打引号的地方用全角 `“”`，**绝不能用转义 `\"`**
'''

def build_prompt(raw_items):
    """raw_items 缩成 prompt 输入。"""
    lines = [f'【{TODAY} 抓到的科学线索】共 {len(raw_items)} 条：']
    for i, it in enumerate(raw_items, 1):
        lines.append(f'\n[{i}] 来源: {it["source"]}')
        lines.append(f'    英文原标题: {it["title"]}')
        if it.get('desc'):
            lines.append(f'    摘要: {it["desc"]}')
        if it.get('link'):
            lines.append(f'    链接: {it["link"]}')
    lines.append('\n请严格按 schema 输出 5 条 JSON 对象，JSON 数组前后用 []，每条 7 个字段：'
                 ' title, category, angle, source, source_url, evergreen, updated_at')
    return '\n'.join(lines)

def _load_api_key():
    if not os.path.exists(KEY_PATH):
        raise RuntimeError(f'minimax key file missing: {KEY_PATH}')
    with open(KEY_PATH, 'r', encoding='utf-8') as f:
        return f.read().strip()

def _extract_text(resp):
    """从 Anthropic Messages 响应里抽取最终文本。跳过 type=thinking 的块。"""
    blocks = resp.get('content') or []
    parts = []
    for b in blocks:
        if b.get('type') == 'text':
            parts.append(b.get('text', ''))
    return ''.join(parts).strip()

def call_minimax(raw_items):
    """调 OpenClaw 默认模型 MiniMax-M3（Anthropic Messages 协议）。
    带 warm-up ping + 1-2 次 retry。
    M3 会在前面产生一个 type=thinking 的块（包含思考过程），取文本时跳过。
    """
    api_key = _load_api_key()

    # ── warm-up ping：1-2 次最小调用确认服务有响应 ──
    ping_body = {
        'model': MINIMAX_MODEL,
        'max_tokens': 8,
        'messages': [{'role': 'user', 'content': '回 OK'}],
    }
    for ping_try in range(2):
        try:
            req = urllib.request.Request(
                MINIMAX_BASE,
                data=json.dumps(ping_body).encode('utf-8'),
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': ANTHROPIC_VERSION,
                    'content-type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                ping_resp = json.loads(r.read())
            # M3 可能只回 type=thinking 块（不含 text），只要 content 数组有块就算服务健康
            blocks = ping_resp.get('content') or []
            if not blocks:
                raise RuntimeError(f'warm-up ping 响应体空: {str(ping_resp)[:200]}')
            break  # ping 成功
        except Exception as e:
            if ping_try == 1:
                raise RuntimeError(f'warm-up ping 失败: {type(e).__name__}: {e}') from e
            time.sleep(1.0 + random.random())
    print('  [warmup] MiniMax-M3 ping OK', file=sys.stderr)

    # ── 真请求：带 1-2 次 retry 处理偶发 5xx/timeout/空响应 ──
    # thinking 设为 disabled：M3 默认会先生成思考块（占很多 token 且对本任务的
    # 中文 hook 改写无价值），关掉后 16s 就能出 text；否则 130s+
    body = {
        'model': MINIMAX_MODEL,
        'max_tokens': 2400,
        'system': STYLE_GUIDE,
        'messages': [{'role': 'user', 'content': build_prompt(raw_items)}],
        'temperature': 0.85,
        'thinking': {'type': 'disabled'},
    }
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                MINIMAX_BASE,
                data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': ANTHROPIC_VERSION,
                    'content-type': 'application/json',
                    'accept': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
                resp = json.loads(r.read())
            # M3 可能输出多个 text 块；拼接后可能为空（仅 thinking），
            # 仅当 type=text 的总拼接为空且没有 type=text 块时才重试
            blocks = resp.get('content') or []
            text_blocks = [b for b in blocks if b.get('type') == 'text']
            content = ''.join(b.get('text', '') for b in text_blocks).strip()
            if not content and not text_blocks:
                raise RuntimeError(f'真请求响应体里没有 type=text 块: {str(resp)[:200]}')
            return parse_topics_json(content)
        except urllib.error.HTTPError as e:
            last_err = e
            # 4xx 客户端错不重试；5xx / 529（Anthropic 限流） 重试
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f'HTTP {e.code}: {e.read().decode()[:200]}') from e
            backoff = (1.2 + random.random() * 0.8) * (attempt + 1)
            print(f'  [retry] M3 HTTP {e.code}，第 {attempt+1} 次失败，{backoff:.1f}s 后重试', file=sys.stderr)
            time.sleep(backoff)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err = e
            backoff = (1.2 + random.random() * 0.8) * (attempt + 1)
            print(f'  [retry] M3 {type(e).__name__}，{backoff:.1f}s 后重试', file=sys.stderr)
            time.sleep(backoff)
        except RuntimeError as e:
            # 空响应这种"疑似服务抖"，也重试
            last_err = e
            if attempt == 2:
                raise
            backoff = (1.2 + random.random() * 0.8) * (attempt + 1)
            print(f'  [retry] M3 {str(e)[:60]}，{backoff:.1f}s 后重试', file=sys.stderr)
            time.sleep(backoff)
    raise RuntimeError(f'M3 重试 3 次仍失败: {type(last_err).__name__}: {last_err}')

_JSON_FENCE = re.compile(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', re.IGNORECASE)

def parse_topics_json(content):
    """从 M3 回复里抽 JSON 数组；容错处理 markdown 围栏和前后杂质。"""
    s = content.strip()
    # 1) 先抓 ```json [...] ``` 围栏
    m = _JSON_FENCE.search(s)
    if m:
        s = m.group(1)
    else:
        # 2) 找第一个 [ 和最后一个 ] 之间的内容
        start = s.find('[')
        end = s.rfind(']')
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]
    try:
        arr = json.loads(s)
    except json.JSONDecodeError as e:
        raise RuntimeError(f'M3 返回不是合法 JSON: {e}; head={content[:200]!r}') from e
    if not isinstance(arr, list):
        raise RuntimeError(f'M3 返回根不是数组: {type(arr).__name__}')

    cleaned = []
    for raw in arr[:NEW_TOPICS_PER_RUN]:
        if not isinstance(raw, dict):
            continue
        title = (raw.get('title') or '').strip()
        angle = (raw.get('angle') or '').strip()
        if not title or not angle:
            continue
        cleaned.append({
            'title': title[:60],
            'category': (raw.get('category') or '宇宙').strip()[:20],
            'angle': angle[:120],
            'source': (raw.get('source') or 'NASA').strip()[:80],
            'source_url': (raw.get('source_url') or 'https://science.nasa.gov/').strip()[:300],
            'evergreen': False,
            'updated_at': TODAY,
        })
    if len(cleaned) < 1:
        raise RuntimeError('M3 返回 0 条有效 topic')
    return cleaned

# ── 落盘 / 降级 ──────────────────────────────────────────────
def load_existing():
    if not os.path.exists(TOPICS_PATH):
        return []
    try:
        with open(TOPICS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as e:
        print(f'  [warn] 现有 topic_recommendations.json 读不出来: {e}', file=sys.stderr)
        return []

def merge_and_write(new_topics, existing):
    """按 title 去重，新条目 prepend并保持稳定在最前，cap 在 MAX_TOPICS。

    排序语义：
    1) 本次刷新的全新条目（永远在最前，用户一点就能看到）
    2) 原有的常青条目（evergreen=True 优先）
    3) 原有的非常青条目（按 updated_at 倒序，最近用的靠前）
    """
    seen = {t.get('title', '').strip() for t in existing}
    fresh = []
    for t in new_topics:
        title = t['title'].strip()
        if title and title not in seen:
            seen.add(title)
            fresh.append(t)

    rest = list(existing)  # 不包括新条目

    # rest 内部：evergreen 优先，updated_at 倒序
    rest.sort(key=lambda x: (
        not bool(x.get('evergreen')),                    # False(0)=evergreen 排前
        -(int(x.get('updated_at', '0000-00-00').replace('-', '')) or 0),  # 日期倒序
        x.get('title', ''),
    ))

    combined = fresh + rest
    trimmed = combined[:MAX_TOPICS]
    with open(TOPICS_PATH, 'w', encoding='utf-8') as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)
    return len(fresh), len(trimmed)

def fallback_shuffle():
    """外网或 LLM 失败时降级：洗牌 + 刷 updated_at。"""
    existing = load_existing()
    if not existing:
        with open(TOPICS_PATH, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return {'mode': 'shuffle', 'added': 0, 'total': 0, 'note': 'no existing'}
    pool = [dict(t) for t in existing]
    random.shuffle(pool)
    for t in pool:
        t['updated_at'] = TODAY
    pool.sort(key=lambda x: (not bool(x.get('evergreen')), x.get('title', '')))
    pool = pool[:MAX_TOPICS]
    with open(TOPICS_PATH, 'w', encoding='utf-8') as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)
    return {'mode': 'shuffle', 'added': 0, 'total': len(pool), 'note': 'fallback'}

# ── 主流程 ────────────────────────────────────────────────────
def main():
    print(f'[refresh_topics] {TODAY}', file=sys.stderr)
    raw = gather_raw_items()
    print(f'[refresh_topics] raw items 总量: {len(raw)}', file=sys.stderr)
    if not raw:
        result = fallback_shuffle()
        result['note'] = '所有数据源都拉空；已降级到洗牌'
        print(json.dumps(result, ensure_ascii=False))
        return

    try:
        new_topics = call_minimax(raw)
        print(f'[refresh_topics] M3 返回 {len(new_topics)} 条候选', file=sys.stderr)
    except Exception as e:
        print(f'  [warn] M3 调用失败: {type(e).__name__}: {e}', file=sys.stderr)
        result = fallback_shuffle()
        result['note'] = f'LLM 失败({type(e).__name__})；已降级到洗牌'
        print(json.dumps(result, ensure_ascii=False))
        return

    existing = load_existing()
    added, total = merge_and_write(new_topics, existing)
    result = {'mode': 'fetch+llm', 'added': added, 'total': total,
              'raw_count': len(raw), 'candidates': len(new_topics),
              'note': 'ok' if added else '新题全部命中已有，未新增（属正常）'}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()
