"""Self-created source only; real queue/claim/executor/cache, mock gateway, no network."""
from copy import deepcopy
import json
import types
import unittest
from unittest.mock import patch

import test_wuyu_reading_replan as old
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_materials import parse_local_material
from services.wuyu_reading import encoded, finish_reading, prepare_reading, reader_prompt, sha
from services.wuyu_reading_synthesis import synthesis_task
from services.wuyu_deep_research import research_guidance
from services.wuyu_reading_workflow import _cached_response, _reader_context, _reader_scope, _write

OBSERVATIONS = []


class RecoveryTests(unittest.TestCase):
    setUp = old.ReadingReplanTests.setUp
    tearDown = old.ReadingReplanTests.tearDown
    raw_reader = staticmethod(old.ReadingReplanTests.raw_reader)
    cache_root = old.ReadingReplanTests.cache_root
    assert_cache_unchanged = old.ReadingReplanTests.assert_cache_unchanged
    change = old.ReadingReplanTests.change
    approve_source_draft = old.ReadingReplanTests.approve_source_draft
    run_cached_reading = old.ReadingReplanTests.run_cached_reading

    def enable_executor(self):
        self.calls = {'reader': [], 'synthesis': []}
        self.reader_tasks = {}
        self.invalid_synthesis = False
        self.app.config.update(self.config, WUYU_READING_SYNTHESIS_ENABLED=True,
            WUYU_TEMPLATE_LIBRARY_PATH=str(self.root / 'empty-fixture-template-library'))
        # The real executor and charges run; the only provider is this in-memory gateway.
        outer = self
        class Gateway:
            models = {}
            last_response_metadata = {'finish_reason': 'stop'}
            def text(self, kind, prompt, **kwargs):
                request = json.loads(prompt)
                if 'source_facts' in request:
                    outer.calls['synthesis'].append(deepcopy(request))
                    return '{"pages":[]}' if outer.invalid_synthesis else outer.raw_synthesis(request)
                task = outer.reader_tasks.get(prompt)
                if task is None:
                    raise AssertionError('Unexpected non-synthesis reader prompt')
                outer.calls['reader'].append(task['id'])
                return outer.raw_reader(task)
        self.gateway = Gateway()
        self.app.config['WUYU_PIPELINE_FACTORY'] = lambda config: types.SimpleNamespace(config=config, gateway=self.gateway)

    @staticmethod
    def raw_synthesis(request):
        fact = request['source_facts'][0]
        return encoded({'pages': [{'number': i, 'title': 'Verified evidence',
            'finding_ids': [fact['id']], 'source_asset_ids': [], 'visual_form': 'qualitative_diagram',
            'copy': fact['statement'], 'points': [fact['statement']], 'layout_rationale': 'Source-grounded evidence'}
            for i in range(1, request['user_request'].get('page_count', 12) + 1)]})

    def execute_read(self, action='analyze_materials', feedback=None):
        _, body = self.service._read(self.project)
        config = dict(self.app.config, WUYU_READING_EXCLUSIONS=deepcopy(body.get('reading_exclusions', {})),
                      WUYU_READING_ASSET_EXCLUSIONS=deepcopy(body.get('reading_asset_exclusions', {})))
        plan = prepare_reading(body['materials'], _reader_context(body), config)
        self.reader_tasks = {reader_prompt(task): task for task in plan['tasks']}
        payload = {} if feedback is None else {'feedback': feedback,
            'draft_revision': body['reading']['draft_revision']}
        with patch('services.wuyu_runtime_settings.get_research_policy', return_value=self.policy), \
                patch('services.wuyu_identity.authorize_paid'):
            self.service.execute_action(self.project, action, payload,
                expected_revision=self.service._read(self.project)[0])
            job = WuyuWorkflowJob.query.filter_by(project_id=self.project, status='queued').one()
            job_id = job.id
            self.assertTrue(self.service._claim(job_id))
            self.service._run(job_id)
        db.session.expire_all()
        job = db.session.get(WuyuWorkflowJob, job_id)
        return job, self.service._read(self.project)[1]

    def cache_fixture(self):
        body = self.run_cached_reading()
        records = [{'task_id': task['id'], 'status': 'completed',
            'result': _cached_response(task, self.cache_root(task))['result']} for task in self.original_plan['tasks']]
        report = finish_reading(self.original_plan, records, body['brief'])
        task = synthesis_task(self.original_plan, report, body['brief'], research_guidance(body))
        root = self.service._path(self.project, 'reading', 'synthesis/' + task['id'].split(':', 1)[1][:32])
        root.mkdir(parents=True, exist_ok=True)
        return body, report, task, root / 'response.json'

    def assert_bad_cache(self, raw, cause, finish_reason='stop', configure=None):
        body, report, task, path = self.cache_fixture()
        if configure:
            configure(task)
        _write(path, {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': finish_reason})
        original = path.read_bytes()
        job = types.SimpleNamespace(project_id=self.project, id='self-cache-replay')
        with patch('services.wuyu_reading_workflow.WorkflowCharge', side_effect=AssertionError('No reserve')), \
                patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            for attempt in range(2):
                # For the incomplete-statement fixture mutate only the in-memory synthesis task.
                with patch('services.wuyu_reading_synthesis.synthesis_task', return_value=task):
                    with self.assertRaises(PipelineError) as caught:
                        self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
                self.assertEqual(caught.exception.code, 'reading_cached_plan_invalid')
                self.assertEqual(caught.exception.cause_code, cause)
                self.assertEqual(caught.exception.diagnostics,
                    {'validation_code': cause, 'response_cached': True, 'provider_called': False})
                self.assertEqual(path.read_bytes(), original)

    def test_feedback_real_executor_reuses_readers_and_persists_latest_for_analyze(self):
        self.enable_executor()
        job, body = self.execute_read()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(body['reading']['status'], 'draft_ready')
        self.assertEqual((len(self.calls['reader']), len(self.calls['synthesis'])), (0, 1))
        old_revision = body['reading']['draft_revision']
        self.approve_source_draft()
        for feedback in ('Use a clearer problem-to-process narrative', 'Keep the verified evidence and simplify the order'):
            previous_calls = len(self.calls['synthesis'])
            job, body = self.execute_read('revise_draft', feedback)
            self.assertEqual(job.status, 'completed')
            self.assertEqual(body['reading']['status'], 'draft_ready')
            self.assertGreater(body['reading']['draft_revision'], old_revision)
            self.assertEqual(body['reading']['_reader_brief'], self.original_brief)
            self.assertEqual(body['reading']['_planning_feedback'], feedback)
            self.assertEqual(self.calls['synthesis'][-1]['user_request']['draft_revision_feedback'], feedback)
            self.assertEqual(len(self.calls['reader']), 0)
            self.assertEqual(len(self.calls['synthesis']), previous_calls + 1)
            self.assert_cache_unchanged()
            job, body = self.execute_read()
            self.assertEqual(job.status, 'completed')
            self.assertEqual(len(self.calls['synthesis']), previous_calls + 1)
        self.assertFalse(WuyuWorkflowJob.query.filter_by(project_id=self.project, action='generate_images').count())
        OBSERVATIONS.append({'fixture': 'pure_feedback', 'real_executor': True,
            'new_reader_calls': 0, 'new_synthesis_calls_per_new_feedback': 1,
            'subsequent_analyze_uses_feedback_cache': True, 'source_cache_bytes_unchanged': True})

    def test_semantic_topic_goal_requirements_each_reinvalidate_and_read_actual_source(self):
        self.enable_executor()
        self.assertEqual(self.execute_read()[0].status, 'completed')
        for changes in ({'topic': 'Another delivery question'}, {'goal': 'Explain source verification'},
                        {'requirements': 'Focus on verification conditions'}):
            before = self.service._read(self.project)[1]
            self.change(**changes)
            changed = self.service._read(self.project)[1]
            self.assertGreater(changed['material_revision'], before['material_revision'])
            self.assertNotIn('_reader_brief', changed['reading'])
            self.assertNotIn('_planning_feedback', changed['reading'])
            previous = len(self.calls['reader'])
            job, body = self.execute_read()
            self.assertEqual(job.status, 'completed', job.error_code)
            self.assertGreater(len(self.calls['reader']), previous)
            self.assert_cache_unchanged()

    def test_same_material_id_new_source_sha_requires_new_reader_and_new_evidence(self):
        self.enable_executor()
        self.assertEqual(self.execute_read()[0].status, 'completed')
        material_id = self.service._read(self.project)[1]['materials'][0]['id']
        self.path.write_text('A replacement source requires an explicit integrity check.\n'
                             'The revised conclusion depends on the new original evidence.', encoding='utf-8')
        parsed = parse_local_material(self.path, self.app.config)
        def replace(body):
            body['materials'][0].update(parsed, text='\n\n'.join(row['text'] for row in parsed['chunks']),
                bytes=self.path.stat().st_size)
            self.service._reading_invalidate(body)
        self.service._mutate(self.project, replace)
        job, body = self.execute_read()
        self.assertEqual(job.status, 'completed', job.error_code)
        self.assertEqual(body['materials'][0]['id'], material_id)
        self.assertEqual(len(self.calls['reader']), 1)
        self.assertIn('replacement source', self.calls['synthesis'][-1]['source_facts'][0]['statement'])
        self.assertNotIn('reliable delivery process', self.calls['synthesis'][-1]['source_facts'][0]['statement'])
        self.assert_cache_unchanged()

    def test_hash_mismatch_keeps_original_security_issue_and_makes_no_model_call(self):
        self.enable_executor()
        self.path.write_text('Changed bytes without updating the declared original SHA.', encoding='utf-8')
        job, body = self.execute_read()
        self.assertEqual(self.calls, {'reader': [], 'synthesis': []})
        codes = [issue.get('code') for issue in body['reading']['issues']]
        self.assertIn('material_hash_mismatch', codes)
        self.assertNotIn('reading_cached_plan_invalid', codes)
        self.assertFalse(body['reading']['coverage_complete'])

    def test_reader_context_scope_rejects_sha_role_and_exclusion_changes(self):
        self.run_cached_reading()
        self.change(page_count=8)
        body = self.service._read(self.project)[1]
        self.assertEqual(_reader_context(body), self.original_brief)
        self.assertIn('_reader_scope', body['reading'])
        for mutate in (lambda b: b['materials'][0].update(sha256='f'*64),
                       lambda b: b['materials'][0].update(role='template'),
                       lambda b: b.update(reading_exclusions={b['materials'][0]['id']: 'Explicit exclusion'}),
                       lambda b: b.update(reading_asset_exclusions={'fixture-asset': {'reason': 'Exclude'}})):
            changed = deepcopy(body); mutate(changed)
            self.assertNotEqual(_reader_scope(body), _reader_scope(changed))
            self.assertEqual(_reader_context(changed), changed['brief'])

    def test_cached_failure_can_revise_same_project_from_first_failed_positive_revision(self):
        self.enable_executor(); self.invalid_synthesis = True
        job, body = self.execute_read()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'reading_synthesis_invalid')
        failed_revision = body['reading']['draft_revision']
        self.assertGreater(failed_revision, 0)
        raw_paths = list(self.service._path(self.project, 'reading', 'synthesis').glob('*/response.json'))
        self.assertEqual(len(raw_paths), 1)
        raw_bytes = raw_paths[0].read_bytes()
        for attempt in range(2):
            job, body = self.execute_read()
            self.assertEqual((job.status, job.error_code), ('failed', 'reading_cached_plan_invalid'))
            self.assertEqual((len(self.calls['reader']), len(self.calls['synthesis'])), (0, 1))
            self.assertEqual(raw_paths[0].read_bytes(), raw_bytes)
        self.invalid_synthesis = False
        job, body = self.execute_read('revise_draft', 'Keep the verified material and reorganize into complete pages')
        self.assertEqual(job.status, 'completed', job.error_code)
        self.assertEqual(body['reading']['status'], 'draft_ready')
        self.assertEqual((len(self.calls['reader']), len(self.calls['synthesis'])), (0, 2))
        self.assertEqual(raw_paths[0].read_bytes(), raw_bytes)
        self.assert_cache_unchanged()
        OBSERVATIONS.append({'fixture': 'first_failed_plan_same_project_recovery',
            'first_failure_draft_revision': failed_revision, 'same_bad_cache_replays': 2,
            'bad_cache_replay_provider_calls': 0, 'explicit_feedback_new_reader_calls': 0,
            'explicit_feedback_new_synthesis_calls': 1, 'original_failed_response_unchanged': True,
            'final_status': body['reading']['status']})

    def test_raw_invalid_json_cached_classifies_without_repeated_provider(self):
        self.assert_bad_cache('not a JSON response', 'invalid_model_json')

    def test_raw_invalid_page_count_cached_classifies_without_repeated_provider(self):
        self.assert_bad_cache('{"pages":[]}', 'reading_synthesis_invalid')

    def test_raw_unknown_source_image_is_dropped_not_fatal(self):
        # op 176: an image ID outside the page's evidence is dropped (never re-bound),
        # instead of voiding every page of the paid synthesis.
        from services.wuyu_reading_synthesis import apply_synthesis
        body, report, task, path = self.cache_fixture()
        value = json.loads(self.raw_synthesis(json.loads(task['prompt'])))
        value['pages'][0]['source_asset_ids'] = ['not-a-source-asset']
        apply_synthesis(encoded(value), task, report, body['brief'])

    def test_raw_incomplete_statement_cached_classifies_without_repeated_provider(self):
        body, report, task, path = self.cache_fixture()
        raw = self.raw_synthesis(json.loads(task['prompt']))
        self.assert_bad_cache(raw, 'reading_synthesis_incomplete_statement',
            configure=lambda t: t['findings']['f1'].update(statement_context_complete=False))

    def test_raw_truncated_cached_classifies_without_repeated_provider(self):
        self.assert_bad_cache('{"pages":[]}', 'output_truncated', finish_reason='length')

    def test_cache_wrapper_and_path_errors_never_construct_provider(self):
        body, report, task, path = self.cache_fixture()
        raw = self.raw_synthesis(json.loads(task['prompt']))
        job = types.SimpleNamespace(project_id=self.project, id='bad-cache-container')
        variants = [b'not-wrapper-json', encoded({'task_id': 'another-task', 'raw': raw, 'raw_sha256': sha(raw)}).encode(),
            encoded({'task_id': task['id'], 'raw': raw, 'raw_sha256': '0'*64}).encode()]
        with patch('services.wuyu_reading_workflow.WorkflowCharge', side_effect=AssertionError('No reserve')), \
                patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            for contents in variants:
                path.write_bytes(contents)
                with self.assertRaises(PipelineError) as caught:
                    self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
                self.assertEqual(caught.exception.code, 'reading_cache_invalid')
                self.assertEqual(path.read_bytes(), contents)
            path.unlink(); path.mkdir()
            with self.assertRaises(PipelineError) as caught:
                self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
            self.assertEqual(caught.exception.code, 'reading_cache_invalid')
            path.rmdir(); path.symlink_to(path.parent / 'missing-self-fixture')
            with self.assertRaises(PipelineError) as caught:
                self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
            self.assertEqual(caught.exception.code, 'reading_cache_invalid')
            self.assertTrue(path.is_symlink())

    def test_security_cancel_budget_unknown_errors_are_not_relabelled_as_plan_cache(self):
        body, report, task, path = self.cache_fixture()
        raw = self.raw_synthesis(json.loads(task['prompt']))
        _write(path, {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
        original = path.read_bytes()
        job = types.SimpleNamespace(project_id=self.project, id='non-plan-error')
        with patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            for code in ('material_hash_mismatch', 'source_asset_hash_mismatch', 'unsafe_path', 'cancelled',
                         'reading_synthesis_budget', 'some_future_security_error'):
                with patch('services.wuyu_reading_synthesis.apply_synthesis', side_effect=PipelineError(code, 'Self fixture')):
                    with self.assertRaises(PipelineError) as caught:
                        self.service._reading_synthesize(job, self.original_plan, report, body['brief'], self.config)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(path.read_bytes(), original)

    def test_legacy_compatible_synthesis_cache_remains_valid_and_unmodified(self):
        body, report, task, path = self.cache_fixture()
        legacy_id = task['compatible_task_ids'][0]
        legacy = self.service._path(self.project, 'reading', 'synthesis/' + legacy_id.split(':', 1)[1][:32]) / 'response.json'
        raw = self.raw_synthesis(json.loads(task['prompt']))
        _write(legacy, {'task_id': legacy_id, 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
        original = legacy.read_bytes()
        with patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            result = self.service._reading_synthesize(types.SimpleNamespace(project_id=self.project, id='legacy'),
                self.original_plan, report, body['brief'], self.config)
        self.assertEqual(result['synthesis']['source_task_id'], legacy_id)
        self.assertTrue(result['synthesis']['response_reused'])
        self.assertEqual(legacy.read_bytes(), original)


if __name__ == '__main__':
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryTests),
                               unittest.defaultTestLoader.loadTestsFromTestCase(old.ReadingReplanTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('RECOVERY_RESULT_JSON=' + json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'observations': OBSERVATIONS}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
