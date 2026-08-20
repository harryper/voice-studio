import copy
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app as voice_app


def _increment_job_counter(job_id, iterations):
    for _ in range(iterations):
        def increment(job):
            job['counter'] = job.get('counter', 0) + 1

        voice_app.mutate_job(job_id, increment)


class JobStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_mode_config = copy.deepcopy(voice_app.MODE_CONFIG)
        self.addCleanup(self._restore_mode_config)

        root = Path(self.temp_dir.name)
        for mode in ('voice', 'music', 'cover'):
            job_dir = root / 'jobs' / mode
            archive_dir = root / 'archive' / mode
            job_dir.mkdir(parents=True)
            archive_dir.mkdir(parents=True)
            voice_app.MODE_CONFIG[mode]['job_dir'] = str(job_dir)
            voice_app.MODE_CONFIG[mode]['archive_dir'] = str(archive_dir)

    def _restore_mode_config(self):
        voice_app.MODE_CONFIG.clear()
        voice_app.MODE_CONFIG.update(self.original_mode_config)

    def test_failed_serialization_keeps_previous_job_readable(self):
        original = {
            'id': 'atomic-job',
            'mode': 'voice',
            'status': 'ready',
        }
        voice_app.save_job(original)

        invalid = dict(original, status='done', unserializable=object())
        with self.assertRaises(TypeError):
            voice_app.save_job(invalid)

        saved = json.loads(Path(voice_app.job_path('atomic-job')).read_text())
        self.assertEqual(saved['status'], 'ready')
        self.assertNotIn('unserializable', saved)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process locks')
    def test_mutate_job_serializes_cross_process_updates(self):
        job_id = 'shared-job'
        voice_app.save_job({
            'id': job_id,
            'mode': 'voice',
            'status': 'ready',
            'counter': 0,
        })

        context = multiprocessing.get_context('fork')
        workers = [
            context.Process(target=_increment_job_counter, args=(job_id, 25))
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        self.assertEqual(voice_app.load_job(job_id)['counter'], 100)

    def test_concurrent_tts_requests_get_distinct_run_ids(self):
        job_id = 'tts-job'
        voice_app.save_job({
            'id': job_id,
            'mode': 'voice',
            'status': 'ready',
            'script': '一段可以合成的旁白。',
            'voice_runs': [],
        })
        rendezvous = threading.Barrier(2)

        def fake_synthesize(job, run_id, voice, do_mix, bgm_asset, **kwargs):
            rendezvous.wait(timeout=5)
            return {
                'run_id': run_id,
                'voice_url': f'https://example.test/{run_id}-voice.mp3',
                'final_url': f'https://example.test/{run_id}-final.mp3',
                'status': 'done',
            }

        def invoke_tts():
            with voice_app.app.test_request_context(
                f'/api/jobs/{job_id}/process-tts', method='POST'
            ):
                voice_app.process_tts(job_id)

        with mock.patch.object(voice_app, '_synthesize_run', side_effect=fake_synthesize):
            workers = [threading.Thread(target=invoke_tts) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())

        run_ids = [run['run_id'] for run in voice_app.load_job(job_id)['voice_runs']]
        self.assertEqual(len(run_ids), 2)
        self.assertEqual(len(set(run_ids)), 2)

    def test_failed_archive_serialization_keeps_active_job_and_no_partial_archive(self):
        original = {
            'id': 'archive-job',
            'mode': 'voice',
            'status': 'done',
        }
        voice_app.save_job(original)
        invalid = dict(original, unserializable=object())

        with self.assertRaises(TypeError):
            voice_app.archive_job(invalid)

        self.assertTrue(Path(voice_app.job_path('archive-job')).exists())
        archive_path = Path(voice_app.job_archive_dir('voice')) / 'archive-job.json'
        self.assertFalse(archive_path.exists())


if __name__ == '__main__':
    unittest.main()
