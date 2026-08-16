#!/usr/bin/env python3
"""
刷新"热门主题推荐"：从 NIH、Nature 等生命科学媒体真采原始线索，
调用 OpenClaw 默认模型 MiniMax-M3（走 Anthropic Messages 协议）改写为
"老波"风格的助眠科普中文选题，落到本地 topic_recommendations.json。

行为：
- 拉取 NIH Research Matters、Nature Biology、PLOS Biology、bioRxiv、eLife 等 RSS/Atom 源
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
import string
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

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
# 每条 (name, url, kind, count)；统一按 RSS/Atom 解析。
BIOLOGY_CATEGORIES = ('细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为')

# 知识库补位必须使用与分类匹配的稳定权威解说页。每类一个 URL，既防止模型
# 编造来源，也避免所有补位主题撞在同一个通用主页上。
CURATED_FALLBACK_URLS = {
    '细胞': 'https://www.genome.gov/genetics-glossary/Cell',
    '遗传': 'https://www.genome.gov/genetics-glossary/Gene',
    '衰老': 'https://www.ncbi.nlm.nih.gov/books/NBK10041/',
    '神经': 'https://www.ncbi.nlm.nih.gov/books/NBK10799/',
    '微生物': 'https://www.ncbi.nlm.nih.gov/books/NBK560448/',
    '免疫': 'https://www.ncbi.nlm.nih.gov/books/NBK279364/',
    '演化': 'https://www.ncbi.nlm.nih.gov/books/NBK230201/',
    '生态': 'https://www.ncbi.nlm.nih.gov/books/NBK217802/',
    '植物': 'https://www.ncbi.nlm.nih.gov/books/NBK217808/',
    '动物行为': 'https://www.ncbi.nlm.nih.gov/books/NBK224378/',
}
CURATED_FALLBACK_GUIDE = '\n'.join(
    f'  {category} => {url}'
    for category, url in CURATED_FALLBACK_URLS.items()
)

SOURCES = [
    # NIH's main Research Matters feed returns 403 to this runtime. NIEHS is an
    # official NIH institute; this biology/environmental-health research feed is
    # current and reachable from the refresh client.
    {'name': 'NIEHS Published Research', 'kind': 'rss', 'count': 4,
     'url': ('https://www.niehs.nih.gov/news/newsroom/rssfeed/'
             'rss_recently_published_research.xml')},
    {'name': 'Nature Biology', 'kind': 'rss', 'count': 4,
     'url': 'https://www.nature.com/subjects/biological-sciences.rss'},
    # Cell's planned current.rss endpoint rejected the refresh client with 403;
    # PLOS Biology is an official, currently fetchable biology-only Atom feed.
    {'name': 'PLOS Biology', 'kind': 'rss', 'count': 3,
     'url': 'https://journals.plos.org/plosbiology/feed/atom'},
    {'name': 'bioRxiv', 'kind': 'rss', 'count': 3,
     'url': 'https://connect.biorxiv.org/biorxiv_xml.php?subject=all'},
    {'name': 'eLife', 'kind': 'rss', 'count': 3,
     'url': 'https://elifesciences.org/rss/recent.xml'},
]

RSS1_NAMESPACE = 'http://purl.org/rss/1.0/'
ATOM_NAMESPACE = 'http://www.w3.org/2005/Atom'

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
    # RSS 1.0: <rdf:RDF><item>（item 和字段都在默认 RSS 1.0 namespace）
    # Atom:    <feed><entry>
    candidates = (
        root.findall('.//item')
        or root.findall(f'.//{{{RSS1_NAMESPACE}}}item')
        or root.findall(f'.//{{{ATOM_NAMESPACE}}}entry')
    )
    for it in candidates:
        title = (it.findtext('title') or
                 it.findtext(f'{{{RSS1_NAMESPACE}}}title') or
                 it.findtext(f'{{{ATOM_NAMESPACE}}}title') or '').strip()
        link = (it.findtext('link') or
                it.findtext(f'{{{RSS1_NAMESPACE}}}link') or
                it.findtext(f'{{{ATOM_NAMESPACE}}}link') or '').strip()
        if not link:
            atom_link = it.find(f'{{{ATOM_NAMESPACE}}}link')
            if atom_link is not None:
                link = atom_link.attrib.get('href', '').strip()
        desc = (it.findtext('description') or
                it.findtext(f'{{{RSS1_NAMESPACE}}}description') or
                it.findtext(f'{{{ATOM_NAMESPACE}}}summary') or '').strip()
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
STYLE_GUIDE = f'''你是"老波"——一个生命科学科普频道的助眠主播，每天晚上用第二人称"你"和听众聊天。
我们要给听众一个反常识的、让人想继续听的睡前话题，作为 15-20 分钟的中文音频脚本选题。

【标题要求】（15-30 字，**绝对不能直译英文新闻标题**；标题会作为博客标题直接发布，**严禁任何标点符号**）
- 反常识判断：一句话打破听众的固有印象
- 悬念标签：让人想点开听完
- 常用句式（全部不用标点）：
  · 你以为 X 其实 Y
  · X 到底 Y 了没有
  · 为什么 X 总是 Y
  · X 可能正在你的 Y 里悄悄发生
  · X 比 Y 古老 Z 倍
  · X 偷偷改变了你的 Y
  · X 离我们到底有多远
- 例（**风格参考，不是让你照抄**）：
  · 你以为细胞只会变老其实它也会拆旧零件
  · 肠道里的微生物为什么会彼此传递消息
  · 植物不移动为什么也能记住季节
  · 动物的睡眠到底在整理什么

【angle 要求】（20-40 字）
- 讲明白"这个标题要从哪个画面 / 哪个反常识点切进去"
- 格式参考："从 X 讲到 Y"、"用第一视角讲 X"、"把 X 误解拆掉，讲成 Y"
- 要具体到画面感，不要抽象定义
- 例（**风格参考**）：
  · 从溶酶体讲到细胞如何拆解并循环利用旧零件
  · 从果蝇的神经回路讲到记忆如何在突触间留下痕迹
  · 把基因和命运的误解拆掉，讲成环境如何参与调节表达

【category 限定】（从下面选一个最贴的）
{' / '.join(BIOLOGY_CATEGORIES)}

【source 字段】
写源名 + 今天的日期，例如 "NIH Research Matters {TODAY}" / "Nature Biology {TODAY}"

【source_url 字段】
从原报道里选**最像科普大众报道的那一条**链接（不要 PDF 论文原始链接，要 RSS 里给的 html landing）

【硬规则】
- 输出必须是合法 JSON 数组，**第一个字符必须是 `[`，最后一个字符必须是 `]`**
- **exactly 5 个对象**，不多不少
- **5 条必须分散到 5 条不同的原始线索**；不要 5 条都讲同一篇新闻的不同角度
  （提示：[[1]][[2]][[3]] 三个不同线索对应三个不同主题；不要为了看似"都在讲一个主题"而全部引用同一条）
- 如果某条线索不够"反常识 / 不够听完"、适合静眠，可以跳过该线索，选别的
- angle / source 等正文用全角中文标点（，。？——）
- **标题严禁任何标点符号**（包括中文全角、半角逗号/问号/引号、「，。？；：、！——……""''《》」等所有字符，以及 Markdown 符号 `* # _ `）
- **不要任何开头/结尾寒暄、不要 markdown 围栏、不要代码块标记、不要 "以下是..." 引导句**
- 不要复述英文原标题；中文标题要让人想点开听
- 细胞、动物或体外研究必须明确其证据层级，不能写成已经在人类身上证实；人类研究也要说明它是观察、关联还是干预证据
- 绝不把相关性写成因果关系；没有直接实验或干预证据时，要使用“相关”“可能”“提示”等谨慎表述
- 不要承诺疗效、不要医学建议、不要诊断或治疗建议，也不要"治愈""根治"
- `source_url` 只能逐字使用 user 消息中某条线索的 html 链接，或使用下方与 category 对应的知识库补位链接；严禁编造或改写 URL
- 字符串里要打引号的地方用全角 `“”`，**绝不能用转义 `\"`**
- 如果 user 消息末尾有【已覆盖链接】块，**禁止**基于这些 URL 的报道改写新题（换个标题重写同一篇报道也是重复）；改用其他未覆盖的线索，或用生命科学知识库造一条老波选题

【平衡要求】（关键）
- 如果 user 消息末尾有【当前分类分布】块：5 条里**至少 2 条要覆盖【低于 3 条的分类】中列出的低分类**
- 优先级：低分类 > 现有足够分类
- 如果外部线索里某条不够反常识 / 不适合你需要的低分类，**你有权完全不用外部线索，调用你的生命科学知识库**造一条符合老波风格的题目：仅需满足"标题反常识、angle 有画面感、面向睡前听众"、以一个**真实生命科学概念**为基础（不编造不存在的生命现象）
- 造题时：`source` 写 "生命科学选题 {TODAY}"，`source_url` 必须从下面选与 `category` 同一行的 URL：
{CURATED_FALLBACK_GUIDE}
- 5 条里**最多 3 条可以用老波选题**，至少 2 条**必须**覆盖低分类（要么外部线索支持、要么你造）
'''

def build_prompt(raw_items, category_status='', covered_urls=()):
    """raw_items 缩成 prompt 输入。

    category_status: 形如：
        【当前分类分布】
        细胞=6  遗传=5  神经=5  免疫=4  植物=3
        ⚠️ 低于 3 条的分类（需优先补足）: 衰老, 微生物, 演化, 生态, 动物行为
    空字符串则不发该块。

    covered_urls: 已在 topic_recommendations.json 里的 source_url 集合。
    M3 看到【已覆盖链接】块后禁止基于这些 URL 改写新题，避免换标题重写同一报道。"""
    lines = [f'【{TODAY} 抓到的科学线索】共 {len(raw_items)} 条：']
    for i, it in enumerate(raw_items, 1):
        lines.append(f'\n[{i}] 来源: {it["source"]}')
        lines.append(f'    英文原标题: {it["title"]}')
        if it.get('desc'):
            lines.append(f'    摘要: {it["desc"]}')
        if it.get('link'):
            lines.append(f'    链接: {it["link"]}')
    if covered_urls:
        # 截断到 50 条避免 prompt 膨胀
        shown = list(covered_urls)[:50]
        lines.append(f'\n【已覆盖链接】（库内已有，禁止基于它们改写新题）共 {len(shown)} 条：')
        for u in shown:
            lines.append(f'  - {u}')
        if len(covered_urls) > 50:
            lines.append(f'  ...还有 {len(covered_urls) - 50} 条省略')
    if category_status:
        lines.append('\n' + category_status)
    lines.append('\n请严格按 schema 输出 5 条 JSON 对象，JSON 数组前后用 []，每条 7 个字段：'
                 ' title, category, angle, source, source_url, evergreen, updated_at')
    return '\n'.join(lines)


def count_categories(existing, target=3):
    """统计当前分类分布，返回 status 块字符串 + 低分类列表。

    Returns: (status_block, low_categories)
        status_block: 可拼到 prompt 末尾的多行文本
        low_categories: ['细胞', '遗传', ...]  低于 target 条的分类
    """
    from collections import Counter
    c = Counter({category: 0 for category in BIOLOGY_CATEGORIES})
    c.update(
        t.get('category')
        for t in existing
        if t.get('category') in BIOLOGY_CATEGORIES
    )
    lines = ['【当前分类分布】']
    for cat, n in sorted(c.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f'  {cat} = {n}')
    low = [cat for cat in BIOLOGY_CATEGORIES if c[cat] < target]
    if low:
        lines.append(f'  ⚠️ 低于 {target} 条的分类（需优先补足）: {", ".join(low)}')
    return '\n'.join(lines), low

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

def call_minimax(raw_items, category_status='', covered_urls=()):
    """调 OpenClaw 默认模型 MiniMax-M3（Anthropic Messages 协议）。
    带 warm-up ping + 1-2 次 retry。
    M3 会在前面产生一个 type=thinking 的块（包含思考过程），取文本时跳过。

    category_status: 分类平衡提示文本（由 main() 算好后传入）。
    covered_urls: 已在库里的 source_url 集合，会拼进 user prompt。
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
        'messages': [{'role': 'user', 'content': build_prompt(raw_items, category_status, covered_urls=covered_urls)}],
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
            return parse_topics_json(content, raw_items=raw_items)
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

_PROMISE_WORDING_RE = re.compile(
    r'治愈|根治|包治|治好|疗效显著|药到病除|永不复发|百分之百有效|百分百有效|'
    r'彻底康复|彻底逆转|保证(?:疗效|有效|治愈|康复)|一定能|肯定能'
)

def _clean_title(t):
    """移除 Unicode 与 ASCII 标点，供严格校验和最终标准化复用。"""
    if not isinstance(t, str) or not t:
        return ''
    s = ''.join(
        '' if ch in string.punctuation or unicodedata.category(ch).startswith('P') else ch
        for ch in t
    )
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _normalize_url(url):
    """返回用于来源校验/去重的 HTTPS URL；非法 URL 返回空字符串。"""
    if not isinstance(url, str):
        return ''
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return ''
    if parsed.scheme.lower() != 'https' or not parsed.hostname:
        return ''
    if parsed.username or parsed.password:
        return ''
    path = parsed.path.rstrip('/') or '/'
    netloc = parsed.netloc.lower()
    return urlunsplit(('https', netloc, path, parsed.query, ''))


def _dedupe_url(url):
    """沿用推荐池的文章去重语义：忽略 query string 与尾部斜杠。"""
    normalized = _normalize_url(url)
    return normalized.split('?', 1)[0] if normalized else ''


def _allowed_source_provenance(raw_items):
    """生成 URL -> 合法 source 集合，并加入显式策展的分类补位来源。"""
    allowed = {}
    for item in raw_items or ():
        if not isinstance(item, dict):
            continue
        url = _normalize_url(item.get('link'))
        source = item.get('source')
        if not url or not isinstance(source, str) or not source.strip():
            continue
        source = source.strip()
        allowed.setdefault(url, set()).update((source, f'{source} {TODAY}'))
    for url in CURATED_FALLBACK_URLS.values():
        allowed[_normalize_url(url)] = {
            '生命科学选题',
            f'生命科学选题 {TODAY}',
        }
    return allowed


def parse_topics_json(content, raw_items=()):
    """从 M3 回复抽取并严格验证完整的五条 topic 批次。"""
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
    if len(arr) != NEW_TOPICS_PER_RUN:
        raise RuntimeError(
            f'M3 必须返回 {NEW_TOPICS_PER_RUN} 条 topic，实际 {len(arr)} 条')

    allowed_sources = _allowed_source_provenance(raw_items)
    fallback_categories = {
        _normalize_url(url): category
        for category, url in CURATED_FALLBACK_URLS.items()
    }
    cleaned = []
    seen_titles = set()
    seen_urls = set()
    for index, raw in enumerate(arr, 1):
        if not isinstance(raw, dict):
            raise RuntimeError(f'M3 第 {index} 条 topic 不是对象')
        raw_title = raw.get('title')
        title = _clean_title(raw_title)
        if not title or title != raw_title.strip():
            raise RuntimeError(f'M3 第 {index} 条标题为空或含标点')
        if _PROMISE_WORDING_RE.search(title):
            raise RuntimeError(f'M3 第 {index} 条标题含疗效承诺或夸大表述')
        angle = raw.get('angle')
        if not isinstance(angle, str) or not angle.strip():
            raise RuntimeError(f'M3 第 {index} 条 angle 为空或不是字符串')
        angle = angle.strip()
        category = raw.get('category') or '细胞'
        if not isinstance(category, str):
            raise RuntimeError(f'M3 第 {index} 条 category 不是字符串')
        category = category.strip()[:20]
        if category not in BIOLOGY_CATEGORIES:
            raise RuntimeError(f'M3 第 {index} 条 category 不在允许范围: {category!r}')
        source = raw.get('source') or '生命科学选题'
        if not isinstance(source, str) or not source.strip():
            raise RuntimeError(f'M3 第 {index} 条 source 为空或不是字符串')
        source = source.strip()[:80]
        source_url = raw.get('source_url') or CURATED_FALLBACK_URLS[category]
        normalized_url = _normalize_url(source_url)
        if not normalized_url:
            raise RuntimeError(f'M3 第 {index} 条 source_url 不是合法 HTTPS URL')
        if normalized_url not in allowed_sources:
            raise RuntimeError(f'M3 第 {index} 条 source_url 不来自抓取线索或策展列表')
        fallback_category = fallback_categories.get(normalized_url)
        if fallback_category and fallback_category != category:
            raise RuntimeError(
                f'M3 第 {index} 条知识库 URL 与 category 不匹配')
        if source not in allowed_sources[normalized_url]:
            raise RuntimeError(f'M3 第 {index} 条 source 与来源 URL 不匹配')
        if title in seen_titles:
            raise RuntimeError(f'M3 第 {index} 条标题与批次内其他 topic 重复')
        dedupe_url = _dedupe_url(source_url)
        if dedupe_url in seen_urls:
            raise RuntimeError(f'M3 第 {index} 条 URL 与批次内其他 topic 重复')
        seen_titles.add(title)
        seen_urls.add(dedupe_url)
        cleaned.append({
            'title': title[:60],
            'category': category,
            'angle': angle[:120],
            'source': source,
            'source_url': source_url.strip()[:300],
            'evergreen': False,
            'updated_at': TODAY,
        })
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


def _atomic_write_topics(topics):
    """在目标同目录完成 fsync 后原子替换，失败时保留原文件。"""
    target_path = os.path.abspath(TOPICS_PATH)
    target_dir = os.path.dirname(target_path)
    prefix = f'.{os.path.basename(target_path)}.'
    fd, temp_path = tempfile.mkstemp(
        dir=target_dir, prefix=prefix, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as topic_file:
            json.dump(topics, topic_file, ensure_ascii=False, indent=2)
            topic_file.flush()
            os.fsync(topic_file.fileno())
        os.replace(temp_path, target_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

def merge_and_write(new_topics, existing):
    """按 title + source_url 双键去重，新条目 prepend并保持稳定在最前，cap 在 MAX_TOPICS。

    排序语义：
    1) 本次刷新的全新条目（永远在最前，用户一点就能看到）
    2) 原有的常青条目（evergreen=True 优先）
    3) 原有的非常青条目（按 updated_at 倒序，最近用的靠前）

    只由 MAX_TOPICS 截断淘汰旧热点；30 条满池里每接受一条新题才淘汰一条，
    去重全命中时不缩池。
    """
    seen_titles = set()
    seen_urls = set()
    for t in existing:
        seen_titles.add(t.get('title', '').strip())
        url = (t.get('source_url') or '').split('?')[0].rstrip('/')
        if url:
            seen_urls.add(url)

    fresh = []
    for t in new_topics:
        title = t['title'].strip()
        url = (t.get('source_url') or '').split('?')[0].rstrip('/')
        if not title:
            continue
        # 双键去重：title 命中 或 url 命中都跳过
        if title in seen_titles:
            continue
        if url and url in seen_urls:
            continue
        seen_titles.add(title)
        if url:
            seen_urls.add(url)
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
    _atomic_write_topics(trimmed)
    return len(fresh), len(trimmed)

def fallback_shuffle():
    """外网或 LLM 失败时降级：组内洗牌 + 刷 updated_at。"""
    existing = load_existing()
    if not existing:
        _atomic_write_topics([])
        return {'mode': 'shuffle', 'added': 0, 'total': 0, 'note': 'no existing'}
    evergreen = [dict(t) for t in existing if t.get('evergreen')]
    hot = [dict(t) for t in existing if not t.get('evergreen')]
    random.shuffle(evergreen)
    random.shuffle(hot)
    pool = evergreen + hot
    for t in pool:
        t['updated_at'] = TODAY
    pool = pool[:MAX_TOPICS]
    _atomic_write_topics(pool)
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

    # 算分类平衡提示：保证刷新后各分类 >= 3 条
    existing = load_existing()
    cat_status, low_cats = count_categories(existing, target=3)
    if low_cats:
        print(f'[refresh_topics] 低于3条的分类: {low_cats}', file=sys.stderr)

    # 收集已在库里的 source_url（去掉 query string 和尾 slash）传给 LLM，
    # 让它知道哪些报道已经覆盖，避免换个标题重写同一篇
    covered_urls = sorted({
        (t.get('source_url') or '').split('?')[0].rstrip('/')
        for t in existing
        if t.get('source_url')
    })

    try:
        new_topics = call_minimax(raw, category_status=cat_status, covered_urls=covered_urls)
        print(f'[refresh_topics] M3 返回 {len(new_topics)} 条候选 (covered_urls={len(covered_urls)})', file=sys.stderr)
    except Exception as e:
        print(f'  [warn] M3 调用失败: {type(e).__name__}: {e}', file=sys.stderr)
        result = fallback_shuffle()
        result['note'] = f'LLM 失败({type(e).__name__})；已降级到洗牌'
        print(json.dumps(result, ensure_ascii=False))
        return

    added, total = merge_and_write(new_topics, existing)
    result = {'mode': 'fetch+llm', 'added': added, 'total': total,
              'raw_count': len(raw), 'candidates': len(new_topics),
              'note': 'ok' if added else '新题全部命中已有，未新增（属正常）'}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()
