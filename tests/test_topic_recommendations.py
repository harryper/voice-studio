import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPICS = json.loads((ROOT / 'topic_recommendations.json').read_text(encoding='utf-8'))
REQUIRED = {'title', 'category', 'angle', 'source', 'source_url', 'evergreen', 'updated_at'}
CATEGORIES = {'细胞', '遗传', '衰老', '神经', '微生物', '免疫', '演化', '生态', '植物', '动物行为'}
TITLE_PUNCT = re.compile(r'[，。；：！？、—…“”‘’`《》·,.?!:\-*#_]')


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
