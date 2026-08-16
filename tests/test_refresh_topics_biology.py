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
