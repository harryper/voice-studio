import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import refresh_topics


EXPECTED = ('细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为')
APPROVED_BIOLOGY_FEEDS = {
    'https://www.niehs.nih.gov/news/newsroom/rssfeed/rss_recently_published_research.xml',
    'https://www.nature.com/subjects/biological-sciences.rss',
    'https://journals.plos.org/plosbiology/feed/atom',
    'https://connect.biorxiv.org/biorxiv_xml.php?subject=all',
    'https://elifesciences.org/rss/recent.xml',
}
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


def valid_fallback_batch():
    return [{
        'title': f'{category}里的生命规律可能比想象中复杂',
        'category': category,
        'angle': f'从一个具体画面讲到{category}背后的真实生命科学概念',
        'source': '生命科学选题',
        'source_url': url,
    } for category, url in list(CURATED_FALLBACK_URLS.items())[:5]]


def full_existing_pool():
    rows = [{
        'title': f'常青主题{i}',
        'category': '细胞',
        'angle': '常青角度',
        'source': '权威解说',
        'source_url': f'https://example.org/evergreen-{i}',
        'evergreen': True,
        'updated_at': '2026-01-01',
    } for i in range(20)]
    rows.extend({
        'title': f'热点主题{i}',
        'category': '遗传',
        'angle': '热点角度',
        'source': '新闻来源',
        'source_url': f'https://example.org/hot-{i}',
        'evergreen': False,
        'updated_at': f'2026-02-{i + 1:02d}',
    } for i in range(10))
    return rows


def fresh_topics(count):
    return [{
        'title': f'全新主题{i}',
        'category': '生态',
        'angle': '全新角度',
        'source': '新来源',
        'source_url': f'https://example.org/fresh-{i}',
        'evergreen': False,
        'updated_at': '2026-08-16',
    } for i in range(count)]


class BiologyRefreshTests(unittest.TestCase):
    def merge_in_temp(self, new_topics, existing):
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = os.path.join(temp_dir, 'topics.json')
            original_path = refresh_topics.TOPICS_PATH
            refresh_topics.TOPICS_PATH = topics_path
            try:
                added, total = refresh_topics.merge_and_write(
                    new_topics, existing)
                with open(topics_path, encoding='utf-8') as topic_file:
                    merged = json.load(topic_file)
            finally:
                refresh_topics.TOPICS_PATH = original_path
        return added, total, merged

    def test_source_catalog_routes_only_to_biology_feeds(self):
        urls = [source['url'] for source in refresh_topics.SOURCES]
        self.assertEqual(refresh_topics.BIOLOGY_CATEGORIES, EXPECTED)
        self.assertSetEqual(set(urls), APPROVED_BIOLOGY_FEEDS)

    def test_parser_reads_rss_2_fixture(self):
        xml = b'''<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Cell recycling discovery</title>
          <link>https://example.org/rss2-cell</link>
          <description>&lt;b&gt;Cells&lt;/b&gt; reuse old parts.</description>
        </item></channel></rss>'''
        self.assertEqual(refresh_topics._parse_rss(xml, 'RSS 2', 1), [{
            'title': 'Cell recycling discovery',
            'link': 'https://example.org/rss2-cell',
            'desc': 'Cells reuse old parts.',
            'source': 'RSS 2',
        }])

    def test_parser_reads_atom_fixture(self):
        xml = b'''<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Plant memory discovery</title>
          <link href="https://example.org/atom-plant" />
          <summary>&lt;i&gt;Plants&lt;/i&gt; retain signals.</summary>
        </entry></feed>'''
        self.assertEqual(refresh_topics._parse_rss(xml, 'Atom', 1), [{
            'title': 'Plant memory discovery',
            'link': 'https://example.org/atom-plant',
            'desc': 'Plants retain signals.',
            'source': 'Atom',
        }])

    def test_parser_reads_rss_1_fixture(self):
        xml = b'''<?xml version="1.0"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                 xmlns="http://purl.org/rss/1.0/">
          <item rdf:about="https://example.org/rss1-microbe">
            <title>Microbe cooperation discovery</title>
            <link>https://example.org/rss1-microbe</link>
            <description>&lt;strong&gt;Microbes&lt;/strong&gt; share proteins.</description>
          </item>
        </rdf:RDF>'''
        self.assertEqual(refresh_topics._parse_rss(xml, 'RSS 1', 1), [{
            'title': 'Microbe cooperation discovery',
            'link': 'https://example.org/rss1-microbe',
            'desc': 'Microbes share proteins.',
            'source': 'RSS 1',
        }])

    def test_category_balance_includes_categories_absent_from_pool(self):
        status, low = refresh_topics.count_categories([{'category': '细胞'}], target=3)
        self.assertIn('细胞 = 1', status)
        self.assertIn('遗传 = 0', status)
        self.assertIn('动物行为', low)

    def test_parser_rejects_batches_without_exactly_five_objects(self):
        batch = valid_fallback_batch()
        for candidate in (batch[:4], batch + [dict(batch[0], title='第六个主题')]):
            with self.subTest(size=len(candidate)):
                with self.assertRaises(RuntimeError):
                    refresh_topics.parse_topics_json(
                        json.dumps(candidate, ensure_ascii=False))

    def test_parser_rejects_non_biology_category(self):
        batch = valid_fallback_batch()
        batch[0]['category'] = '黑洞'
        with self.assertRaises(RuntimeError):
            refresh_topics.parse_topics_json(json.dumps(batch, ensure_ascii=False))

    def test_parser_reports_malformed_category_as_runtime_error(self):
        batch = valid_fallback_batch()
        batch[0]['category'] = 42
        try:
            refresh_topics.parse_topics_json(
                json.dumps(batch, ensure_ascii=False))
        except RuntimeError:
            pass
        except Exception as exc:
            self.fail(f'parser leaked {type(exc).__name__} instead of RuntimeError')
        else:
            self.fail('parser accepted a non-string category')

    def test_parser_rejects_punctuation_and_promises_in_titles(self):
        for title in ('细胞真的会返老还童?', '这个发现保证治愈癌症'):
            batch = valid_fallback_batch()
            batch[0]['title'] = title
            with self.subTest(title=title):
                with self.assertRaises(RuntimeError):
                    refresh_topics.parse_topics_json(
                        json.dumps(batch, ensure_ascii=False))

    def test_parser_rejects_duplicate_titles_and_urls(self):
        for duplicate_field in ('title', 'source_url'):
            batch = valid_fallback_batch()
            batch[1][duplicate_field] = batch[0][duplicate_field]
            with self.subTest(field=duplicate_field):
                with self.assertRaises(RuntimeError):
                    refresh_topics.parse_topics_json(
                        json.dumps(batch, ensure_ascii=False))

    def test_parser_treats_query_variants_as_duplicate_article_urls(self):
        raw_items = [{
            'title': f'Fetched title {index}',
            'link': f'https://example.org/same-article?tracking={index}',
            'desc': 'Fetched description',
            'source': 'Fixture Biology',
        } for index in (1, 2)]
        batch = valid_fallback_batch()
        for row, raw_item in zip(batch, raw_items):
            row.update({
                'source': f'Fixture Biology {refresh_topics.TODAY}',
                'source_url': raw_item['link'],
            })
        with self.assertRaises(RuntimeError):
            refresh_topics.parse_topics_json(
                json.dumps(batch, ensure_ascii=False), raw_items=raw_items)

    def test_parser_rejects_http_unknown_urls_and_hallucinated_sources(self):
        cases = (
            ('source_url', 'http://www.genome.gov/genetics-glossary/Cell'),
            ('source_url', 'https://invented.example/biology-breakthrough'),
            ('source', 'Hallucinated Journal'),
        )
        for field, value in cases:
            batch = valid_fallback_batch()
            batch[0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(RuntimeError):
                    refresh_topics.parse_topics_json(
                        json.dumps(batch, ensure_ascii=False))

    def test_parser_accepts_only_fetched_landing_urls_beyond_fallbacks(self):
        raw_items = [{
            'title': 'A real source title',
            'link': 'https://example.org/fetched-biology-story',
            'desc': 'A real source description',
            'source': 'Fixture Biology',
        }]
        batch = valid_fallback_batch()
        batch[0].update({
            'source': f'Fixture Biology {refresh_topics.TODAY}',
            'source_url': raw_items[0]['link'],
        })
        try:
            parsed = refresh_topics.parse_topics_json(
                json.dumps(batch, ensure_ascii=False), raw_items=raw_items)
        except TypeError as exc:
            self.fail(f'parser has no provenance input: {exc}')
        self.assertEqual(parsed[0]['source_url'], raw_items[0]['link'])

    def test_parser_defaults_to_category_specific_curated_urls(self):
        categories = list(CURATED_FALLBACK_URLS)[:5]
        batch = valid_fallback_batch()
        for row in batch:
            row.pop('source')
            row.pop('source_url')
        parsed = refresh_topics.parse_topics_json(
            json.dumps(batch, ensure_ascii=False))
        self.assertEqual(
            [item['source_url'] for item in parsed],
            [CURATED_FALLBACK_URLS[category] for category in categories],
        )

    def test_two_low_category_fallback_topics_survive_merge(self):
        batch = valid_fallback_batch()
        for row in batch:
            row.pop('source')
            row.pop('source_url')
        parsed = refresh_topics.parse_topics_json(
            json.dumps(batch, ensure_ascii=False))
        existing = [{
            'title': f'已有主题{i}',
            'category': '细胞',
            'angle': '已有角度',
            'source': '已有来源',
            'source_url': f'https://example.org/existing-{i}',
            'evergreen': i < 20,
            'updated_at': '2026-01-01',
        } for i in range(30)]
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = os.path.join(temp_dir, 'topics.json')
            original_path = refresh_topics.TOPICS_PATH
            refresh_topics.TOPICS_PATH = topics_path
            try:
                refresh_topics.merge_and_write(parsed, existing)
                with open(topics_path, encoding='utf-8') as topic_file:
                    merged = json.load(topic_file)
            finally:
                refresh_topics.TOPICS_PATH = original_path
        added_titles = {item['title'] for item in merged[:5]}
        self.assertIn(batch[0]['title'], added_titles)
        self.assertIn(batch[1]['title'], added_titles)

    def test_merge_keeps_full_pool_for_zero_one_two_and_five_fresh_topics(self):
        for fresh_count in (0, 1, 2, 5):
            with self.subTest(fresh_count=fresh_count):
                added, total, merged = self.merge_in_temp(
                    fresh_topics(fresh_count), full_existing_pool())
                self.assertEqual(added, fresh_count)
                self.assertEqual(total, 30)
                self.assertEqual(len(merged), 30)
                self.assertEqual(
                    sum(item['evergreen'] for item in merged), 20)
                self.assertEqual(
                    sum(not item['evergreen'] for item in merged), 10)

    def test_merge_evicts_only_the_number_of_hot_topics_actually_accepted(self):
        existing = full_existing_pool()
        candidates = [
            dict(existing[0]),
            dict(existing[-1]),
            *fresh_topics(1),
        ]
        added, total, merged = self.merge_in_temp(candidates, existing)
        self.assertEqual((added, total), (1, 30))
        self.assertEqual(len(merged), 30)
        self.assertEqual(sum(item['evergreen'] for item in merged), 20)
        self.assertEqual(sum(not item['evergreen'] for item in merged), 10)

    def test_replace_failure_preserves_original_in_merge_and_fallback_paths(self):
        operations = (
            lambda: refresh_topics.merge_and_write(
                fresh_topics(1), full_existing_pool()),
            refresh_topics.fallback_shuffle,
        )
        for operation in operations:
            with self.subTest(operation=operation.__name__):
                with tempfile.TemporaryDirectory() as temp_dir:
                    topics_path = os.path.join(temp_dir, 'topics.json')
                    original_text = json.dumps(
                        full_existing_pool(), ensure_ascii=False, indent=2)
                    with open(topics_path, 'w', encoding='utf-8') as topic_file:
                        topic_file.write(original_text)
                    original_path = refresh_topics.TOPICS_PATH
                    refresh_topics.TOPICS_PATH = topics_path
                    try:
                        with mock.patch.object(
                                refresh_topics.os, 'replace',
                                side_effect=OSError('simulated replace failure')):
                            with self.assertRaises(OSError):
                                operation()
                        with open(topics_path, encoding='utf-8') as topic_file:
                            self.assertEqual(topic_file.read(), original_text)
                        self.assertEqual(os.listdir(temp_dir), ['topics.json'])
                    finally:
                        refresh_topics.TOPICS_PATH = original_path

    def test_fallback_shuffle_randomizes_within_evergreen_and_hot_groups(self):
        rows = []
        for index in ('a', 'b', 'c'):
            rows.extend((
                {
                    'title': f'evergreen-{index}',
                    'evergreen': True,
                    'updated_at': '2026-01-01',
                },
                {
                    'title': f'hot-{index}',
                    'evergreen': False,
                    'updated_at': '2026-01-01',
                },
            ))
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = os.path.join(temp_dir, 'topics.json')
            with open(topics_path, 'w', encoding='utf-8') as topic_file:
                json.dump(rows, topic_file)
            original_path = refresh_topics.TOPICS_PATH
            refresh_topics.TOPICS_PATH = topics_path
            try:
                with mock.patch.object(
                        refresh_topics.random, 'shuffle',
                        side_effect=lambda values: values.reverse()):
                    refresh_topics.fallback_shuffle()
                with open(topics_path, encoding='utf-8') as topic_file:
                    shuffled = json.load(topic_file)
            finally:
                refresh_topics.TOPICS_PATH = original_path
        self.assertEqual([item['title'] for item in shuffled], [
            'evergreen-c', 'evergreen-b', 'evergreen-a',
            'hot-c', 'hot-b', 'hot-a',
        ])
