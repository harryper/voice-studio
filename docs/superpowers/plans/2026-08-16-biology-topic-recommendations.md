# Biology Topic Recommendations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 30-item astronomy-heavy recommendation pool with a 10-hot/20-evergreen biology pool and keep future refreshes biology-only.

**Architecture:** Preserve the existing JSON contract, Flask endpoints, and dynamic front-end category filters. Add contract tests around the static pool, then change the refresh script's feeds, taxonomy, safety prompt, and parsing defaults without altering its fetch/LLM/merge/fallback pipeline.

**Tech Stack:** Python 3 standard library, JSON, `unittest`, Flask's existing JSON loader.

## Global Constraints

- Keep exactly 30 recommendation records: 10 with `evergreen: false` and 20 with `evergreen: true`.
- Every record keeps `title`, `category`, `angle`, `source`, `source_url`, `evergreen`, and `updated_at`.
- Allowed categories are `细胞`, `遗传`, `衰老`, `神经`, `微生物`, `免疫`, `演化`, `生态`, `植物`, and `动物行为`.
- Titles contain no punctuation and make no treatment or cure promise.
- Current-news entries use unique, traceable source URLs; evergreen entries use stable authoritative explainers.
- Preserve deduplication, the 30-item cap, fallback shuffle, and existing API/front-end behavior.
- Do not modify `scripts/azure_tts.py`.

---

### Task 1: Lock the recommendation-pool contract

**Files:**
- Create: `tests/test_topic_recommendations.py`
- Read: `topic_recommendations.json`

**Interfaces:**
- Consumes: the repository-root `topic_recommendations.json` array.
- Produces: executable assertions for count, schema, mix, taxonomy, title style, and hot-source uniqueness.

- [ ] **Step 1: Write the failing contract tests**

```python
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPICS = json.loads((ROOT / 'topic_recommendations.json').read_text(encoding='utf-8'))
REQUIRED = {'title', 'category', 'angle', 'source', 'source_url', 'evergreen', 'updated_at'}
CATEGORIES = {'细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为'}
TITLE_PUNCT = re.compile(r'[，。；：！？、—…“”‘’`《》·,.?!:;\\-*#_]')

class TopicRecommendationTests(unittest.TestCase):
    def test_pool_has_expected_schema_and_mix(self):
        self.assertEqual(len(TOPICS), 30)
        self.assertEqual(sum(not item['evergreen'] for item in TOPICS), 10)
        self.assertEqual(sum(item['evergreen'] for item in TOPICS), 20)
        self.assertTrue(all(set(item) == REQUIRED for item in TOPICS))

    def test_pool_is_biology_only_and_titles_are_clean(self):
        self.assertTrue(all(item['category'] in CATEGORIES for item in TOPICS))
        self.assertTrue(all(not TITLE_PUNCT.search(item['title']) for item in TOPICS))
        self.assertTrue(all(item['title'].strip() and item['angle'].strip() for item in TOPICS))

    def test_hot_sources_are_traceable_and_unique(self):
        hot = [item for item in TOPICS if not item['evergreen']]
        urls = [item['source_url'] for item in hot]
        self.assertTrue(all(url.startswith('https://') for url in urls))
        self.assertEqual(len(urls), len(set(urls)))
```

- [ ] **Step 2: Run the contract tests and confirm the old pool fails**

Run: `python -m unittest tests.test_topic_recommendations -v`

Expected: FAIL because the current pool contains astronomy categories and does not have a 10/20 biology mix.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test_topic_recommendations.py
git commit -m "test: define biology topic pool contract"
```

### Task 2: Replace the static recommendation pool

**Files:**
- Modify: `topic_recommendations.json`
- Test: `tests/test_topic_recommendations.py`

**Interfaces:**
- Consumes: the exact seven-field schema asserted in Task 1.
- Produces: 30 records consumed unchanged by `app.load_topic_recommendations()` and the front-end category renderer.

- [ ] **Step 1: Replace all current records**

Create 10 sourced 2026 hot entries covering these distinct findings: midlife brain immune remodeling; mutation-driven inflammation and lifestyle; transcription-factor rejuvenation in mice; SuperAger hippocampal cell signatures; heterogeneous DNA-methylation aging; age- and sex-dependent longevity loci; cellular senescence mapping; microbiome ecological assembly; plant immune memory; and animal navigation biology.

Create 20 evergreen entries spanning all ten allowed categories, including DNA packaging, mitochondrial inheritance, epigenetics, gene duplication, telomeres, adult neuroplasticity, sleep memory, phage–bacteria arms races, biofilms, immune memory, autoimmune recognition, convergent evolution, endosymbiosis, keystone species, fungal networks, plant signaling, carnivorous plants, collective animal behavior, bird migration, and octopus distributed neural control.

Each JSON object must have this exact shape:

```json
{
  "title": "大脑里的免疫细胞可能会在中年悄悄换班",
  "category": "神经",
  "angle": "从海马体里的小胶质细胞讲到中年之后脑免疫版图的一次重排",
  "source": "NIH 2026-07-23",
  "source_url": "https://www.nih.gov/news-events/news-releases/brain-immunity-may-undergo-major-midlife-overhaul",
  "evergreen": false,
  "updated_at": "2026-07-23"
}
```

- [ ] **Step 2: Run the contract tests**

Run: `python -m unittest tests.test_topic_recommendations -v`

Expected: all three tests PASS.

- [ ] **Step 3: Verify Flask normalization preserves all 30 records**

Run:

```bash
python -c "import app; rows=app.load_topic_recommendations(); assert len(rows)==30; print(len(rows))"
```

Expected: `30`.

- [ ] **Step 4: Commit the new pool**

```bash
git add topic_recommendations.json
git commit -m "content: replace recommendations with biology topics"
```

### Task 3: Lock and implement biology-only automatic refresh

**Files:**
- Create: `tests/test_refresh_topics_biology.py`
- Modify: `scripts/refresh_topics.py`

**Interfaces:**
- Consumes: RSS/Atom/APOD-like source dictionaries through existing `fetch_source()` and `gather_raw_items()` functions.
- Produces: `BIOLOGY_CATEGORIES`, biology feeds in `SOURCES`, a biology-specific `STYLE_GUIDE`, category balancing that includes absent categories, and biology defaults in `parse_topics_json()`.

- [ ] **Step 1: Write failing refresh-policy tests**

```python
import json
import unittest
from scripts import refresh_topics

EXPECTED = ('细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为')

class BiologyRefreshTests(unittest.TestCase):
    def test_source_catalog_routes_only_to_biology_feeds(self):
        urls = [source['url'] for source in refresh_topics.SOURCES]
        self.assertEqual(refresh_topics.BIOLOGY_CATEGORIES, EXPECTED)
        self.assertTrue(any('nih.gov' in url for url in urls))
        self.assertTrue(any('nature.com' in url for url in urls))
        self.assertTrue(all('nasa.gov' not in url and 'astro-ph' not in url for url in urls))

    def test_category_balance_includes_categories_absent_from_pool(self):
        status, low = refresh_topics.count_categories([{'category': '细胞'}], target=3)
        self.assertIn('细胞 = 1', status)
        self.assertIn('遗传 = 0', status)
        self.assertIn('动物行为', low)

    def test_parser_uses_biology_defaults(self):
        parsed = refresh_topics.parse_topics_json(json.dumps([{
            'title': '细胞里面也有一套垃圾分类系统',
            'angle': '从溶酶体讲到细胞如何拆解并循环利用旧零件'
        }], ensure_ascii=False))
        self.assertEqual(parsed[0]['category'], '细胞')
        self.assertIn('nih.gov', parsed[0]['source_url'])

    def test_parser_rejects_non_biology_category(self):
        parsed = refresh_topics.parse_topics_json(json.dumps([{
            'title': '细胞里面也有一套垃圾分类系统',
            'category': '黑洞',
            'angle': '从溶酶体讲到细胞如何拆解并循环利用旧零件'
        }], ensure_ascii=False))
        self.assertEqual(parsed[0]['category'], '细胞')
```

- [ ] **Step 2: Run the policy tests and confirm failure**

Run: `python -m unittest tests.test_refresh_topics_biology -v`

Expected: FAIL because `BIOLOGY_CATEGORIES` is absent, astronomy feeds are configured, absent categories are not balanced, and parser defaults are astronomy-specific.

- [ ] **Step 3: Replace the source catalog and taxonomy**

Define:

```python
BIOLOGY_CATEGORIES = ('细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为')

SOURCES = [
    {'name': 'NIH Research Matters', 'kind': 'rss', 'count': 4,
     'url': 'https://www.nih.gov/news-events/nih-research-matters/rss.xml'},
    {'name': 'Nature Biology', 'kind': 'rss', 'count': 4,
     'url': 'https://www.nature.com/subjects/biological-sciences.rss'},
    {'name': 'Science', 'kind': 'rss', 'count': 3,
     'url': 'https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science'},
    {'name': 'Cell', 'kind': 'rss', 'count': 3,
     'url': 'https://www.cell.com/cell/current.rss'},
    {'name': 'bioRxiv', 'kind': 'rss', 'count': 3,
     'url': 'https://connect.biorxiv.org/biorxiv_xml.php?subject=all'},
    {'name': 'eLife', 'kind': 'rss', 'count': 3,
     'url': 'https://elifesciences.org/rss/recent.xml'},
]
```

Remove the APOD branch and astronomy descriptions from the module docstring. Retain the generic RSS/Atom parsing path.

- [ ] **Step 4: Replace the generation policy and defaults**

Set the category list in `STYLE_GUIDE` from `BIOLOGY_CATEGORIES`, require cautious distinction between human evidence and cell/animal evidence, forbid turning correlation into causation, and forbid diagnosis or treatment advice. Change knowledge-base fallback metadata to `生命科学选题 {TODAY}` and `https://www.nih.gov/news-events/nih-research-matters`.

In `parse_topics_json()`, use these defaults:

```python
'category': (raw.get('category') or '细胞').strip()[:20],
'source': (raw.get('source') or '生命科学选题').strip()[:80],
'source_url': (raw.get('source_url') or 'https://www.nih.gov/news-events/nih-research-matters').strip()[:300],
```

Normalize any model-supplied category outside `BIOLOGY_CATEGORIES` to `细胞`, so astronomy or invented categories cannot enter the saved pool.

Update `count_categories()` to initialize counts from every value in `BIOLOGY_CATEGORIES`, so missing categories appear with zero and are treated as low coverage.

- [ ] **Step 5: Run refresh-policy and parser tests**

Run: `python -m unittest tests.test_refresh_topics_biology -v`

Expected: all four tests PASS.

- [ ] **Step 6: Run a syntax check**

Run: `python -m py_compile scripts/refresh_topics.py`

Expected: exit code 0.

- [ ] **Step 7: Commit the refresh migration**

```bash
git add scripts/refresh_topics.py tests/test_refresh_topics_biology.py
git commit -m "feat: refresh biology topic recommendations"
```

### Task 4: Regression verification

**Files:**
- Verify: `topic_recommendations.json`
- Verify: `scripts/refresh_topics.py`
- Verify: `tests/test_topic_recommendations.py`
- Verify: `tests/test_refresh_topics_biology.py`

**Interfaces:**
- Consumes: completed deliverables from Tasks 1–3.
- Produces: evidence that the new pool and refresh behavior satisfy the design without regressing existing tests.

- [ ] **Step 1: Run the focused test suite**

Run: `python -m unittest tests.test_topic_recommendations tests.test_refresh_topics_biology -v`

Expected: 7 tests PASS.

- [ ] **Step 2: Run the complete existing test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 3: Check formatting and the untouched user change**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; `scripts/azure_tts.py` remains modified but unstaged, and no unrelated files are staged.
