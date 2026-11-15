"""Count/language replans retain the exact verified reader cache and revoke approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types
import unittest
import uuid
from unittest.mock import patch

import test_wuyu_relayout as fixture
from models import db, Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_materials import parse_local_material
from services.wuyu_reading import encoded, prepare_reading, reader_acknowledgement, sha
from services.wuyu_reading_partial import accepted_reading
from services.wuyu_reading_workflow import _cached_response, _write, run_reader
from services.wuyu_workflow_service import CancelledWork, WorkflowAPIError
from services.wuyu_errors import PipelineError


class ReadingReplanTests(unittest.TestCase):
    tearDown = fixture.RelayoutTests.tearDown

    def setUp(self):
        fixture.RelayoutTests.setUp(self)
        self.path = self.root / self.project / 'materials' / 'evidence.txt'
        self.path.write_text('Project goal is a reliable delivery process.\n'
                             'Materials are checked before pages are generated.\n'
                             'Successful work is retained for later review.', encoding='utf-8')
        parsed = parse_local_material(self.path, self.app.config)
        material = {**parsed, 'id': str(uuid.uuid4()), 'role': 'material', 'status': 'completed',
                    'relative_path': self.path.relative_to(self.root).as_posix(),
                    'bytes': self.path.stat().st_size,
                    'text': '\n\n'.join(row['text'] for row in parsed['chunks'])}
        def initialize(body):
            body['materials'] = [material]
            body['brief'].update(task_mode='create', topic='Reliable delivery', goal='Explain reliable delivery',
                                 page_count=12, language='zh', creation_kind='deck')
            self.service._reading_invalidate(body)
        self.service._mutate(self.project, initialize)
        self.original_brief = deepcopy(self.service._read(self.project)[1]['brief'])
        self.policy = {'reading_workers': 1, 'reading_max_calls': 4,
                       'reading_max_points': 1000, 'reading_input_tokens': 18000}
        self.config = dict(self.app.config, WUYU_READING_WORKERS=1, WUYU_READING_MAX_CALLS=4,
                           WUYU_READING_MAX_POINTS=1000, WUYU_READING_INPUT_TOKENS=18000,
                           WUYU_READING_SYNTHESIS_ENABLED=False)
        self.original_plan = prepare_reading([material], self.original_brief, self.config)
        self.cache_bytes = {}
        for task in self.original_plan['tasks']:
            root = self.cache_root(task)
            raw = self.raw_reader(task)
            path = root / 'response.json'
            _write(path, {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
            self.cache_bytes[path] = path.read_bytes()

    @staticmethod
    def raw_reader(task):
        segment = task['segments'][0]
        quote = segment['text']
        return encoded({**reader_acknowledgement(task), 'findings': [
            {'segment_id': segment['id'], 'quote': quote, 'summary': quote, 'topic': '项目目标',
             'relevance': 4, 'source_asset_ids': [], 'data_ids': []}], 'issues': []})

    def cache_root(self, task):
        return self.service._path(self.project, 'reading', 'tasks') / task['id'].split(':', 1)[1][:32]

    def run_cached_reading(self):
        _, body = self.service._read(self.project)
        task = Task(project_id=self.project, task_type='WUYU_WORKFLOW', status='PENDING')
        db.session.add(task); db.session.flush()
        job = WuyuWorkflowJob(project_id=self.project, task_id=task.id, action='analyze_materials',
                              input_epoch=body['epoch'], status='running',
                              payload=encoded({'__reading_policy': self.policy}))
        db.session.add(job); db.session.commit()
        job._runtime_config = self.config
        def no_provider(*args, **kwargs):
            raise AssertionError('A complete verified reader cache must not construct a provider')
        with patch('services.wuyu_identity.authorize_paid'), patch.object(self.service, '_pipeline', side_effect=no_provider):
            self.service._reading_run(job, types.SimpleNamespace(config=self.config))
        job.status = 'completed'; db.session.commit()
        return self.service._read(self.project)[1]

    def approve_source_draft(self):
        def approve(body):
            body['reading'].update(status='accepted', coverage_complete=True, issues=[])
        self.service._mutate(self.project, approve)

    def change(self, **brief):
        revision = self.service._read(self.project)[0]
        return self.service.execute_action(self.project, 'update_brief', brief, expected_revision=revision)

    def assert_cache_unchanged(self):
        for path, original in self.cache_bytes.items():
            self.assertEqual(path.read_bytes(), original)

    def test_first_cached_run_records_its_reader_context(self):
        body = self.run_cached_reading()
        self.assertEqual(body['reading']['_reader_brief'], self.original_brief)
        self.assert_cache_unchanged()

    def test_two_count_language_replans_reuse_readers_and_revoke_each_draft_approval(self):
        body = self.run_cached_reading(); source_revision = body['material_revision']
        for changes in ({'page_count': 8}, {'language': 'en'}):
            self.approve_source_draft()
            self.change(**changes)
            _, changed = self.service._read(self.project)
            self.assertEqual(changed['material_revision'], source_revision)
            self.assertEqual(changed['reading']['status'], 'idle')
            self.assertEqual(changed['reading']['_reader_brief'], self.original_brief)
            self.assertFalse(accepted_reading(changed['reading'], source_revision))
            with self.assertRaises(WorkflowAPIError) as caught:
                self.service._reading_validate_job(changed, 'generate_images', {})
            self.assertEqual(caught.exception.code, 'DRAFT_REVIEW_REQUIRED')
            with patch('services.wuyu_reading_workflow.finish_reading', wraps=__import__(
                    'services.wuyu_reading', fromlist=['finish_reading']).finish_reading) as finish:
                body = self.run_cached_reading()
            self.assertEqual(finish.call_args.args[2]['page_count'], body['brief']['page_count'])
            self.assertEqual(finish.call_args.args[2]['language'], body['brief']['language'])
            report = next(a for a in body['artifacts'] if a['kind'] == 'reading_report')
            # Read the current report; earlier versions remain separate artifacts.
            report_id = body['reading']['artifacts']['report'].removeprefix('artifact:')
            report = next(a for a in body['artifacts'] if a['id'] == report_id)
            raw = json.loads((self.root / report['relative_path']).read_text(encoding='utf-8'))
            self.assertEqual(raw['reading_usage']['new_logical_calls'], 0)
            self.assert_cache_unchanged()

    def test_legacy_reader_context_is_captured_from_exact_prechange_brief(self):
        self.assertNotIn('_reader_brief', self.service._read(self.project)[1].get('reading', {}))
        self.change(page_count=8)
        body = self.run_cached_reading()
        self.assertEqual(body['reading']['_reader_brief'], self.original_brief)
        self.assert_cache_unchanged()

    def test_ten_slide_deck_to_one_page_reuses_evidence_and_revokes_approval(self):
        self.service._mutate(self.project, lambda body: body['brief'].update(page_count=10))
        _, initial = self.service._read(self.project)
        self.original_brief = deepcopy(initial['brief'])
        self.original_plan = prepare_reading(initial['materials'], self.original_brief, self.config)
        for task in self.original_plan['tasks']:
            path = self.cache_root(task) / 'response.json'
            raw = self.raw_reader(task)
            _write(path, {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
            self.cache_bytes[path] = path.read_bytes()
        self.assertEqual(self.original_brief['creation_kind'], 'deck')
        self.assertEqual(self.original_brief['page_count'], 10)
        read = self.run_cached_reading()
        self.approve_source_draft()
        self.change(page_count=1, creation_kind='pages')
        _, changed = self.service._read(self.project)
        self.assertEqual(changed['material_revision'], read['material_revision'])
        self.assertEqual(changed['reading']['status'], 'idle')
        self.assertEqual(changed['reading']['_reader_brief'], self.original_brief)
        self.assertFalse(accepted_reading(changed['reading'], changed['material_revision']))
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service._reading_validate_job(changed, 'generate_focused', {'generation_requested': True})
        self.assertEqual(caught.exception.code, 'DRAFT_REVIEW_REQUIRED')
        completed = self.run_cached_reading()
        self.assertEqual(completed['brief']['page_count'], 1)
        self.assertEqual(completed['brief']['creation_kind'], 'pages')
        self.assertEqual(completed['reading']['_reader_brief'], self.original_brief)
        report_id = completed['reading']['artifacts']['report'].removeprefix('artifact:')
        artifact = next(a for a in completed['artifacts'] if a['id'] == report_id)
        report = json.loads((self.root / artifact['relative_path']).read_text(encoding='utf-8'))
        self.assertEqual(report['reading_usage']['new_logical_calls'], 0)
        self.assert_cache_unchanged()

    def test_goal_change_discards_reader_context_and_cannot_reuse_old_tasks(self):
        self.run_cached_reading()
        before = self.service._read(self.project)[1]
        self.change(goal='Explain a different source question')
        _, body = self.service._read(self.project)
        self.assertGreater(body['material_revision'], before['material_revision'])
        self.assertNotIn('_reader_brief', body['reading'])
        plan = prepare_reading(body['materials'], body['brief'], self.config)
        self.assertNotEqual([t['id'] for t in plan['tasks']], [t['id'] for t in self.original_plan['tasks']])
        task = plan['tasks'][0]
        self.assertIsNone(_cached_response(task, self.cache_root(task)))
        gateway = types.SimpleNamespace(last_response_metadata={'finish_reason': 'stop'},
                                        text=lambda *args, **kwargs: self.raw_reader(task))
        with patch.object(gateway, 'text', wraps=gateway.text) as text:
            result = run_reader(task, types.SimpleNamespace(config=self.config, gateway=gateway),
                                self.cache_root(task), 'offline-semantic-change', False, lambda: False)
        self.assertEqual(text.call_count, 1)
        self.assertTrue(result['model_report']['provider_called'])
        self.assert_cache_unchanged()

    def test_source_invalidation_clears_preserved_context_and_approval(self):
        self.run_cached_reading(); self.approve_source_draft(); self.change(page_count=8)
        _, before = self.service._read(self.project)
        self.service._mutate(self.project, self.service._reading_invalidate)
        _, body = self.service._read(self.project)
        self.assertEqual(body['material_revision'], before['material_revision'] + 1)
        self.assertNotIn('_reader_brief', body['reading'])
        self.assertFalse(accepted_reading(body['reading'], body['material_revision']))
        self.assert_cache_unchanged()

    def test_old_epoch_reader_cannot_write_after_count_change(self):
        _, body = self.service._read(self.project)
        old_job = types.SimpleNamespace(input_epoch=body['epoch'])
        self.change(page_count=8)
        revision, before = self.service._read(self.project)
        with self.assertRaises(CancelledWork):
            self.service._mutate(self.project, lambda current: current.update(unsafe_old_output=True), job=old_job)
        self.assertEqual(self.service._read(self.project), (revision, before))

    def test_invalid_plan_cache_explains_explicit_replanning_without_dispatch_or_rewrite(self):
        from services.wuyu_reading import finish_reading
        from services.wuyu_reading_synthesis import synthesis_task
        from services.wuyu_deep_research import research_guidance
        body = self.run_cached_reading()
        results = [{'task_id': task['id'], 'status': 'completed',
                    'result': _cached_response(task, self.cache_root(task))['result']}
                   for task in self.original_plan['tasks']]
        report = finish_reading(self.original_plan, results, body['brief'])
        guidance = research_guidance(body)
        task = synthesis_task(self.original_plan, report, body['brief'], guidance)
        path = self.service._path(self.project, 'reading',
                                  'synthesis/' + task['id'].split(':', 1)[1][:32]) / 'response.json'
        raw = '{"pages":[]}'
        _write(path, {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
        original = path.read_bytes()
        job = types.SimpleNamespace(project_id=self.project, id='cached-invalid-plan')
        with patch('services.wuyu_reading_workflow.WorkflowCharge', side_effect=AssertionError('No reserve')),\
                patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            for _ in range(2):
                with self.assertRaises(PipelineError) as caught:
                    self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
                self.assertEqual(caught.exception.code, 'reading_cached_plan_invalid')
                self.assertEqual(caught.exception.cause_code, 'reading_synthesis_invalid')
                self.assertEqual(path.read_bytes(), original)
        revised = {**body['brief'], 'draft_revision_feedback': 'Reorganize the verified evidence into a clearer plan'}
        self.assertNotEqual(synthesis_task(self.original_plan, report, revised, guidance)['id'], task['id'])
        self.assert_cache_unchanged()


if __name__ == '__main__':
    unittest.main()
