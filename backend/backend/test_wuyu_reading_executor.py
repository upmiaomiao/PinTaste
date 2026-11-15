"""Reader dispatch/cache contracts, using self-created material and no network."""
import importlib
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import test_wuyu_reading_replan as fixture
from services import wuyu_reading_workflow as workflow
from services.wuyu_errors import PipelineError
from services.wuyu_reading import sha

name = 'services.wuyu_reading_executor'
executor = importlib.import_module(name) if importlib.util.find_spec(name) else workflow


class ReadingExecutorTests(unittest.TestCase):
    setUp = fixture.ReadingReplanTests.setUp
    tearDown = fixture.ReadingReplanTests.tearDown
    cache_root = fixture.ReadingReplanTests.cache_root
    raw_reader = staticmethod(fixture.ReadingReplanTests.raw_reader)

    def task_and_pipeline(self, raw=None, finish='stop'):
        task = self.original_plan['tasks'][0]
        raw = fixture.ReadingReplanTests.raw_reader(task) if raw is None else raw
        gateway = types.SimpleNamespace(text=Mock(return_value=raw),
            last_response_metadata={'finish_reason': finish, 'usage': {'total_tokens': 19}})
        pipeline = types.SimpleNamespace(config=self.config, gateway=gateway)
        root = self.root / 'executor-fixture' / 'tasks' / task['id'].split(':', 1)[1][:32]
        return task, pipeline, root, raw

    def test_existing_imports_remain_identical(self):
        for name in ('_write', '_cached_response', '_cached', 'run_reader'):
            self.assertIs(getattr(workflow, name), getattr(executor, name))
        self.assertEqual(executor.run_reader.__module__, executor.__name__)

    def test_valid_response_persists_and_replays_without_another_dispatch(self):
        task, pipeline, root, raw = self.task_and_pipeline()
        first = executor.run_reader(task, pipeline, root, 'first', False, lambda: False)
        saved = (root / 'attempt-first' / 'response.json').read_bytes()
        second = executor.run_reader(task, pipeline, root, 'first', False, lambda: False)
        self.assertEqual(pipeline.gateway.text.call_count, 1)
        self.assertTrue(first['model_report']['provider_called'])
        self.assertTrue(second['model_report']['response_reused'])
        self.assertEqual(first['result'], second['result'])
        self.assertEqual(json.loads(saved)['raw'], raw)
        self.assertEqual((root / 'attempt-first' / 'response.json').read_bytes(), saved)

    def test_invalid_response_is_retained_and_same_job_cannot_redispatch(self):
        task, pipeline, root, raw = self.task_and_pipeline('{"bad":true}')
        with self.assertRaises(PipelineError):
            executor.run_reader(task, pipeline, root, 'bad', False, lambda: False)
        path = root / 'attempt-bad' / 'response.json'
        saved = path.read_bytes()
        self.assertEqual(json.loads(saved)['raw'], raw)
        for retry in (False, True):
            with self.assertRaises(PipelineError) as caught:
                executor.run_reader(task, pipeline, root, 'bad', retry, lambda: False)
            self.assertEqual(caught.exception.code, 'reading_retry_required')
        self.assertEqual(pipeline.gateway.text.call_count, 1)
        self.assertEqual(path.read_bytes(), saved)

    def test_previous_failed_dispatch_needs_explicit_new_job_retry(self):
        task, pipeline, root, _ = self.task_and_pipeline('{"bad":true}')
        with self.assertRaises(PipelineError):
            executor.run_reader(task, pipeline, root, 'old', False, lambda: False)
        with self.assertRaises(PipelineError) as caught:
            executor.run_reader(task, pipeline, root, 'new', False, lambda: False)
        self.assertEqual(caught.exception.code, 'reading_retry_required')
        self.assertEqual(pipeline.gateway.text.call_count, 1)
        pipeline.gateway.text.return_value = fixture.ReadingReplanTests.raw_reader(task)
        result = executor.run_reader(task, pipeline, root, 'new', True, lambda: False)
        self.assertTrue(result['model_report']['provider_called'])
        self.assertEqual(pipeline.gateway.text.call_count, 2)
        self.assertTrue((root / 'attempt-old' / 'response.json').is_file())

    def test_truncated_response_is_saved_before_rejection(self):
        task, pipeline, root, raw = self.task_and_pipeline(finish='length')
        with self.assertRaises(PipelineError) as caught:
            executor.run_reader(task, pipeline, root, 'short', False, lambda: False)
        self.assertEqual(caught.exception.code, 'output_truncated')
        self.assertEqual(json.loads((root / 'attempt-short' / 'response.json').read_text())['raw'], raw)
        self.assertIsNone(executor._cached_response(task, root))
        self.assertEqual(pipeline.gateway.text.call_count, 1)

    def test_cancelled_before_provider_retains_dispatch_guard(self):
        task, pipeline, root, _ = self.task_and_pipeline()
        with self.assertRaises(PipelineError) as caught:
            executor.run_reader(task, pipeline, root, 'cancel', False, lambda: True)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertTrue((root / 'attempt-cancel' / 'dispatch.json').is_file())
        self.assertFalse((root / 'attempt-cancel' / 'response.json').exists())
        pipeline.gateway.text.assert_not_called()
        with self.assertRaises(PipelineError) as caught:
            executor.run_reader(task, pipeline, root, 'cancel', True, lambda: False)
        self.assertEqual(caught.exception.code, 'reading_retry_required')
        pipeline.gateway.text.assert_not_called()

    def test_hash_mismatched_response_cannot_be_reused(self):
        task, _, root, raw = self.task_and_pipeline()
        executor._write(root / 'response.json', {'task_id': task['id'], 'raw': raw,
            'raw_sha256': sha(raw + 'tampered'), 'finish_reason': 'stop'})
        original = (root / 'response.json').read_bytes()
        self.assertIsNone(executor._cached_response(task, root))
        self.assertEqual((root / 'response.json').read_bytes(), original)

    def test_atomic_cache_write_does_not_follow_symlink(self):
        _, _, root, _ = self.task_and_pipeline()
        root.mkdir(parents=True)
        outside = self.root / 'preserved.json'; outside.write_text('original')
        link = root / 'response.json'; link.symlink_to(outside)
        with self.assertRaises(PipelineError) as caught:
            executor._write(link, {'overwrite': True})
        self.assertEqual(caught.exception.code, 'reading_cache_invalid')
        self.assertEqual(outside.read_text(), 'original')
        self.assertEqual(list(root.glob('.r-*')), [])

    def test_visual_cache_hit_still_checks_original_image_integrity(self):
        task, _, root, _ = self.task_and_pipeline()
        task = dict(task, visual_inputs=[{'fixture': True}])
        with patch.object(executor, '_cached_response', return_value={'reading_result': True}), \
                patch('services.wuyu_reading_visuals.verify_reader_image_integrity',
                      side_effect=PipelineError('fixture_image_changed', 'Fixture')) as check:
            cancel = lambda: False
            with self.assertRaises(PipelineError) as caught:
                executor._cached(task, root, self.config, cancel)
            self.assertEqual(caught.exception.code, 'fixture_image_changed')
            check.assert_called_once_with(task, self.config, cancel)


if __name__ == '__main__':
    unittest.main()
