import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITER_PATH = ROOT / 'scripts' / 'process_pending_voice_jobs.py'


def load_build_prompt():
    tree = ast.parse(WRITER_PATH.read_text(encoding='utf-8'))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'build_prompt'
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(WRITER_PATH), 'exec'), namespace)
    return namespace['build_prompt']


build_prompt = load_build_prompt()


class WriterPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = build_prompt({
            'id': 'abc12345',
            'theme': '测试主题',
        })

    def test_uses_abstract_style_profile_not_individual_scripts(self):
        self.assertIn('reference-style.md', self.prompt)
        self.assertNotIn('reference-scripts/', self.prompt)
        self.assertNotIn('quantum-death-bubble', self.prompt)
        self.assertNotIn('why-space-is-cold', self.prompt)
        self.assertNotIn('solar-system-vertical-flight', self.prompt)

    def test_rejects_phrase_quotas_and_requires_fact_checking(self):
        self.assertNotIn('至少使用 4 次', self.prompt)
        self.assertIn('不要固定套用', self.prompt)
        self.assertIn('使用可用的搜索工具核验', self.prompt)
        self.assertIn('不要只凭模型记忆', self.prompt)
        self.assertIn('无法核验就删除', self.prompt)

    def test_keeps_job_output_contract(self):
        self.assertIn('主题：测试主题', self.prompt)
        self.assertIn('runs/abc12345/script.txt', self.prompt)
        self.assertIn('jobs/voice/abc12345.json', self.prompt)
        self.assertIn('status="ready"', self.prompt)


if __name__ == '__main__':
    unittest.main()
