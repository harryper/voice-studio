import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as voice_app


class LocalArtifactCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runs = self.root / 'runs'
        self.jobs = self.root / 'jobs'
        self.archive = self.root / 'archive'
        self.runs.mkdir()
        self.jobs.mkdir()
        self.archive.mkdir()
        self.now = time.time()

        self.patchers = [
            patch.object(voice_app, 'RUNS_DIR', self.runs),
            patch.object(voice_app, 'LOCAL_ARTIFACT_RETENTION_SECONDS', 72 * 3600),
            patch.object(
                voice_app,
                'MODE_CONFIG',
                {
                    'voice': {
                        'job_dir': str(self.jobs),
                        'archive_dir': str(self.archive),
                    }
                },
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def write_job(self, job):
        (self.jobs / f"{job['id']}.json").write_text(
            json.dumps(job), encoding='utf-8'
        )

    def write_artifact(self, path, age_hours):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
        timestamp = self.now - age_hours * 3600
        os.utime(path, (timestamp, timestamp))

    def test_deletes_only_uploaded_voice_artifacts_after_retention(self):
        run_dir = self.runs / 'voice-job' / 'run-1'
        voice = run_dir / 'voice.mp3'
        mixed = run_dir / 'mixed.mp3'
        self.write_artifact(voice, age_hours=73)
        self.write_artifact(mixed, age_hours=73)
        self.write_job({
            'id': 'voice-job',
            'mode': 'theme',
            'voice_runs': [{
                'run_id': 'run-1',
                'voice_url': 'https://r2.example/voice.mp3',
                'final_url': 'https://r2.example/mixed.mp3',
                'bgm': True,
            }],
        })

        result = voice_app.cleanup_expired_local_artifacts(now=self.now)

        self.assertEqual(result['files'], 2)
        self.assertFalse(voice.exists())
        self.assertFalse(mixed.exists())

    def test_keeps_recent_or_not_uploaded_artifacts(self):
        recent = self.runs / 'recent' / 'run-1' / 'voice.mp3'
        failed = self.runs / 'failed' / 'run-1' / 'voice.mp3'
        chunks = self.runs / 'recent' / 'run-1' / 'azure_chunks' / 'chunk_01.mp3'
        self.write_artifact(recent, age_hours=24)
        self.write_artifact(failed, age_hours=100)
        self.write_artifact(chunks, age_hours=1)
        self.write_job({
            'id': 'recent',
            'mode': 'script',
            'voice_runs': [{
                'run_id': 'run-1',
                'voice_url': 'https://r2.example/voice.mp3',
                'final_url': 'https://r2.example/voice.mp3',
                'bgm': False,
            }],
        })
        self.write_job({
            'id': 'failed',
            'mode': 'script',
            'voice_runs': [{'run_id': 'run-1', 'bgm': False}],
        })

        result = voice_app.cleanup_expired_local_artifacts(now=self.now)

        self.assertEqual(result['files'], 1)
        self.assertTrue(recent.exists())
        self.assertTrue(failed.exists())
        self.assertFalse(chunks.exists())


if __name__ == '__main__':
    unittest.main()
