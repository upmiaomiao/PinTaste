"""Reading-attempt status regressions: self-created sources and mock provider only."""
from copy import deepcopy
import hashlib
import json
import unittest
import uuid

import test_wuyu_reading_project_recovery as fixture
from models import db
from services.wuyu_materials import parse_local_material
from services.wuyu_reading import encoded, prepare_reading
from services.wuyu_errors import PipelineError
from services.wuyu_reading_partial import continuation_options, reading_failure_code
from services.wuyu_workflow_service import _failure_message


class ReadingStatusTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting/re-running its test suite.
    setUp = fixture.RecoveryTests.setUp
    tearDown = fixture.RecoveryTests.tearDown
    raw_reader = staticmethod(fixture.RecoveryTests.raw_reader)
    raw_synthesis = staticmethod(fixture.RecoveryTests.raw_synthesis)
    cache_root = fixture.RecoveryTests.cache_root
    change = fixture.RecoveryTests.change
    execute_read = fixture.RecoveryTests.execute_read
    enable_executor = fixture.RecoveryTests.enable_executor

    def fresh_single_page(self):
        self.change(creation_kind='pages', page_count=1, topic='Fresh source-reading fixture')
        self.enable_executor()

    def artifact(self, body, name):
        ident = body['reading']['artifacts'][name].removeprefix('artifact:')
        record = next(a for a in body['artifacts'] if a['id'] == ident)
        return self.root / record['relative_path']

    def report(self, body):
        return json.loads(self.artifact(body, 'report').read_text(encoding='utf-8'))

    def reader_responses(self):
        return list(self.service._path(self.project, 'reading', 'tasks').glob('*/attempt-*/response.json'))

    def assert_failed(self, job, body, code, *, coverage_complete=False):
        self.assertEqual((job.status, job.error_code), ('failed', code))
        self.assertEqual(body['reading']['status'], 'draft_incomplete')
        self.assertEqual(body['reading']['coverage_complete'], coverage_complete)
        self.assertGreater(body['reading']['draft_revision'], 0)
        public = self.service.get_job(self.project, job.id)
        self.assertEqual(public['error_code'], code)
        self.assertTrue(public['error_message'])
        return public

    def test_invalid_reader_fails_after_saving_raw_draft_and_job_log(self):
        self.fresh_single_page()
        raw = '{"read_material_\\nids":[],"findings":[],"issues":[]}'
        self.raw_reader = lambda task: raw
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertLogs(self.app.logger, level='WARNING') as captured:
            job, body = self.execute_read()
        public = self.assert_failed(job, body, 'invalid_reading')
        report = self.report(body)
        self.assertEqual(report['reading_usage']['complete_tasks'], 0)
        self.assertEqual(body['reading']['completed_reader_tasks'], 0)
        self.assertTrue(self.artifact(body, 'markdown').is_file())
        self.assertTrue(self.artifact(body, 'evidence').is_file())
        responses = self.reader_responses()
        self.assertEqual(len(responses), 1)
        self.assertEqual(json.loads(responses[0].read_text())['raw'], raw)
        self.assertIn('job=' + job.id, '\n'.join(captured.output))
        self.assertNotIn(raw, '\n'.join(captured.output))
        self.assertEqual(public['diagnostics']['validation_code'], 'invalid_reading')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original)
        self.assertEqual((len(self.calls['reader']), len(self.calls['synthesis'])), (1, 0))

    def test_quote_mismatch_keeps_original_code_and_cannot_continue(self):
        self.fresh_single_page()
        valid = self.raw_reader
        def wrong_quote(task):
            value = json.loads(valid(task))
            value['findings'][0]['quote'] = 'This sentence is absent from the uploaded original.'
            return encoded(value)
        self.raw_reader = wrong_quote
        job, body = self.execute_read()
        self.assert_failed(job, body, 'reading_quote_mismatch')
        options = self.service.snapshot(self.project)['reading']['continuation_options']
        self.assertEqual(options['failure_category'], 'runtime')
        self.assertFalse(options['can_continue_with_omissions'])
        self.assertEqual(self.report(body)['evidence_validation']['accepted_findings'], 0)

    def test_truncated_reader_retains_paid_response_without_synthesis(self):
        self.fresh_single_page()
        self.gateway.last_response_metadata = {'finish_reason': 'length'}
        job, body = self.execute_read()
        self.assert_failed(job, body, 'output_truncated')
        response = json.loads(self.reader_responses()[0].read_text())
        self.assertEqual(response['finish_reason'], 'length')
        self.assertTrue(response['raw'])
        self.assertEqual((len(self.calls['reader']), len(self.calls['synthesis'])), (1, 0))

    def test_complete_reader_and_plan_still_complete(self):
        self.fresh_single_page()
        job, body = self.execute_read()
        self.assertEqual((job.status, job.error_code), ('completed', None))
        self.assertEqual(body['reading']['status'], 'draft_ready')
        self.assertTrue(body['reading']['coverage_complete'])
        self.assertEqual(body['reading']['completed_reader_tasks'], 1)
        self.assertTrue(body['reading']['content_plan_complete'])
        self.assertFalse(self.service.get_job(self.project, job.id)['error_message'])

    def test_partial_reader_failure_preserves_verified_other_material(self):
        self.fresh_single_page()
        self.path.write_text('Verified source evidence is retained for later review. ' * 65, encoding='utf-8')
        first_parsed = parse_local_material(self.path, self.app.config)
        second = self.path.with_name('second-evidence.txt')
        second.write_text('Independent maintenance evidence must be read separately. ' * 65, encoding='utf-8')
        parsed = parse_local_material(second, self.app.config)
        material = {**parsed, 'id': str(uuid.uuid4()), 'role': 'material', 'status': 'completed',
                    'relative_path': second.relative_to(self.root).as_posix(),
                    'bytes': second.stat().st_size,
                    'text': '\n\n'.join(row['text'] for row in parsed['chunks'])}
        def add(body):
            body['materials'][0].update(first_parsed, bytes=self.path.stat().st_size,
                text='\n\n'.join(row['text'] for row in first_parsed['chunks']))
            body['materials'].append(material)
            self.service._reading_invalidate(body)
        self.service._mutate(self.project, add)
        body = self.service._read(self.project)[1]
        # Use the real admission/grouping rules to obtain independent requests.
        # The source texts are equal-sized so one material fits per task.
        for budget in range(1500, 6001, 100):
            try:
                plan = prepare_reading(body['materials'], body['brief'],
                    dict(self.app.config, WUYU_READING_INPUT_TOKENS=budget))
            except PipelineError as error:
                if error.code != 'reading_budget_invalid':
                    raise
                continue
            task_materials = [{s['material_id'] for s in task['segments']} for task in plan['tasks']]
            if len(task_materials) == 2 and all(len(ids) == 1 for ids in task_materials):
                break
        else:
            self.fail('Self-created sources must form two independently admitted reader tasks')
        self.app.config['WUYU_READING_INPUT_TOKENS'] = budget
        self.config['WUYU_READING_INPUT_TOKENS'] = budget
        self.policy['reading_input_tokens'] = budget
        valid = self.raw_reader
        def independent_response(task):
            if any(s['material_id'] == material['id'] for s in task['segments']):
                return '{"unrecognized_reader_root":true}'
            # Quote one complete sentence. Quoting the whole budget-split
            # segment would include a truncated last sentence, correctly
            # excluded from synthesis by the existing context guard.
            quote = 'Verified source evidence is retained for later review.'
            segment = next(s for s in task['segments'] if quote in s['text'])
            value = json.loads(valid(task))
            value['findings'][0].update(segment_id=segment['id'], quote=quote,
                summary='Verified source evidence is retained for later review.', topic='结论依据')
            return encoded(value)
        self.raw_reader = independent_response
        job, body = self.execute_read()
        self.assert_failed(job, body, 'invalid_reading')
        report = self.report(body)
        self.assertEqual(report['reading_usage']['complete_tasks'], 1)
        self.assertGreater(report['completed_segments'], 0)
        self.assertGreater(report['evidence_validation']['accepted_findings'], 0)
        self.assertTrue(report['findings'])
        self.assertEqual(body['reading']['completed_reader_tasks'], 1)
        self.assertEqual(len(self.reader_responses()), 2)
        self.assertTrue(second.is_file())

    def test_one_page_format_failure_does_not_claim_insufficient_source(self):
        self.fresh_single_page()
        self.raw_reader = lambda task: '{"unexpected":true}'
        job, body = self.execute_read()
        self.assert_failed(job, body, 'invalid_reading')
        text = self.artifact(body, 'markdown').read_text(encoding='utf-8')
        messages = [i.get('message', '') for i in self.report(body)['planning_issues']]
        self.assertNotIn('请补充来源或减少页数', text)
        self.assertNotIn('请减少页数', text)
        self.assertNotIn('请补充来源或减少页数', ' '.join(messages))

    def test_source_hash_failure_is_failed_without_provider_calls(self):
        self.fresh_single_page()
        self.path.write_text('Altered bytes without updating the registered source SHA.', encoding='utf-8')
        job, body = self.execute_read()
        self.assert_failed(job, body, 'material_hash_mismatch')
        self.assertEqual(self.calls, {'reader': [], 'synthesis': []})
        self.assertIn('material_hash_mismatch', [i['code'] for i in body['reading']['issues']])

    def test_cached_synthesis_cause_is_public_without_new_calls_or_raw_rewrite(self):
        self.fresh_single_page()
        self.invalid_synthesis = True
        first, _ = self.execute_read()
        self.assertEqual((first.status, first.error_code), ('failed', 'reading_synthesis_invalid'))
        paths = list(self.service._path(self.project, 'reading', 'synthesis').glob('*/response.json'))
        self.assertEqual(len(paths), 1)
        original = paths[0].read_bytes()
        calls = deepcopy(self.calls)
        job, body = self.execute_read()
        self.assert_failed(job, body, 'reading_cached_plan_invalid', coverage_complete=True)
        public = self.service.get_job(self.project, job.id)
        self.assertEqual(public['diagnostics']['validation_code'], 'reading_synthesis_invalid')
        self.assertTrue(public['diagnostics']['response_cached'])
        self.assertFalse(public['diagnostics']['provider_called'])
        self.assertEqual(self.calls, calls)
        self.assertEqual(paths[0].read_bytes(), original)

    def test_public_job_uses_fixed_message_and_filters_arbitrary_error_payload(self):
        self.fresh_single_page()
        self.raw_reader = lambda task: '{"unexpected":true}'
        job, body = self.execute_read()
        sentinel = 'arbitrary-private-upstream-payload'
        job.progress = encoded({'error': {'message': sentinel, 'exception': sentinel,
            'diagnostics': {'validation_code': 'invalid_reading', 'raw': sentinel, 'exception': sentinel}}})
        db.session.commit()
        public = self.service.get_job(self.project, job.id)
        serialized = encoded(public)
        self.assertNotIn(sentinel, serialized)
        self.assertEqual(public['error_message'], _failure_message('invalid_reading'))
        self.assertEqual(public['diagnostics'], {'validation_code': 'invalid_reading'})

    def test_explicit_exclusions_and_planning_only_shortage_are_not_reader_failure(self):
        for report in ({'issues': [{'code': 'source_assets_excluded_by_user'}]},
                       {'issues': [], 'planning_issues': [{'code': 'presentation_evidence_insufficient'}]}):
            with self.subTest(report=report):
                self.assertIsNone(reading_failure_code(report, [{'status': 'completed'}]))

    def test_security_reason_takes_priority_over_generic_reader_failure(self):
        report = {'issues': [{'code': 'invalid_reading'}, {'code': 'material_hash_mismatch'}]}
        self.assertEqual(reading_failure_code(report, [{'status': 'failed', 'code': 'reader_failed'}]),
                         'material_hash_mismatch')

    def test_original_failed_reader_code_survives_generic_coverage_issue(self):
        for code in ('reading_quote_mismatch', 'reading_numeric_mismatch', 'output_truncated'):
            with self.subTest(code=code):
                self.assertEqual(reading_failure_code({'issues': [{'code': 'reader_not_completed'}]},
                    [{'status': 'failed', 'code': code}]), code)

    def test_unusable_response_has_runtime_guidance_without_valid_plan(self):
        for code in ('invalid_reading', 'invalid_model_json', 'reading_quote_mismatch',
                     'reading_numeric_mismatch', 'output_truncated'):
            with self.subTest(code=code):
                options = continuation_options({'status': 'draft_incomplete', 'issues': [{'code': code}],
                    'coverage_complete': False, 'content_plan_complete': False,
                    'evidence_validation': {'accepted_findings': 0}, '_slide_specs': []})
                self.assertEqual(options['failure_category'], 'runtime')
                self.assertFalse(options['can_continue_with_omissions'])

    def test_existing_verified_partial_plan_keeps_explicit_continue_route(self):
        options = continuation_options({'status': 'draft_incomplete', 'issues': [{'code': 'invalid_reading'}],
            'coverage_complete': False, 'content_plan_complete': True,
            'evidence_validation': {'accepted_findings': 1}, '_slide_specs': [{'number': 1}]})
        self.assertEqual(options['failure_category'], 'source_unread')
        self.assertTrue(options['content_plan_valid'])
        self.assertTrue(options['can_continue_with_omissions'])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ReadingStatusTests))
    print('READING_STATUS_RESULT_JSON=' + json.dumps({'passed': result.wasSuccessful(),
        'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
