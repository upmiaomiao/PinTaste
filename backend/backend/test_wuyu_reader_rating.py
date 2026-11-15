"""Narrow reader ranking recovery; synthetic text, temporary caches, no network."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

from services.wuyu_errors import PipelineError
from services.wuyu_reading import (
    encoded, markdown_draft, reader_acknowledgement, reader_prompt, sha,
    validate_grounded_reader, validate_reader,
)
from services import wuyu_reading_executor as executor


class ReaderRatingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='reader-rating-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'tasks' / 'reader'
        text = 'Materials are checked before pages are generated.'
        segment = {
            'id': 'seg:rating-fixture', 'material_id': 'material-rating-fixture',
            'chunk_id': 'chunk-rating-fixture', 'text': text,
            'text_sha256': sha(text), 'char_start': 0, 'char_end': len(text),
            'chunk_line_start': 1, 'chunk_line_end': 1,
            'source': {'filename': 'self-created.txt', 'line': 1,
                       'material_id': 'material-rating-fixture'},
        }
        self.task = {
            'id': 'read:rating-fixture', 'reader_contract': 3,
            'segments': [segment], 'visual_inputs': [],
            'asset_catalog': {segment['material_id']: []},
            'data_catalog': {segment['material_id']: []},
            'cache_keys': {segment['id']: sha('rating-fixture-cache-key')},
            'brief': {'topic': 'Reliable delivery', 'goal': 'Explain validation',
                      'language': 'zh', 'creation_kind': 'deck', 'page_count': 2},
            'input_tokens': 300, 'output_tokens': 4000,
            'output_limits': {'max_findings': 16},
        }

    def finding(self, relevance=3, **changes):
        segment = self.task['segments'][0]
        value = {'segment_id': segment['id'], 'quote': segment['text'],
                 'summary': segment['text'], 'topic': '原文证据',
                 'relevance': relevance, 'source_asset_ids': [], 'data_ids': []}
        value.update(changes)
        return value

    def response(self, findings=None, task=None, **changes):
        task = self.task if task is None else task
        value = {**reader_acknowledgement(task),
                 'findings': [self.finding()] if findings is None else findings,
                 'issues': []}
        value.update(changes)
        return value

    def correction(self, original=8):
        return {'code': 'out_of_range_relevance_discarded',
                'segment_id': self.task['segments'][0]['id'],
                'field': 'relevance', 'original_value': original,
                'normalized_value': 3}

    def assert_error(self, value, code, task=None, grounded=False):
        validate = validate_grounded_reader if grounded else validate_reader
        with self.assertRaises(PipelineError) as caught:
            validate(value, self.task if task is None else task)
        self.assertEqual(caught.exception.code, code)

    def save_response(self, raw=None, **changes):
        raw = encoded(self.response([self.finding(8)])) if raw is None else raw
        value = {'task_id': self.task['id'], 'raw': raw,
                 'raw_sha256': sha(raw), 'finish_reason': 'stop'}
        value.update(changes)
        path = self.root / 'response.json'
        executor._write(path, value)
        return path

    def segment_path(self):
        key = self.task['cache_keys'][self.task['segments'][0]['id']]
        return self.root.parent / 'segments' / (key[:32] + '.json')

    def segment_value(self):
        segment = self.task['segments'][0]
        return {
            'cache_key': self.task['cache_keys'][segment['id']],
            'segment_id': segment['id'], 'source_text_sha256': segment['text_sha256'],
            'findings': [self.finding(3)], 'issues': [],
            'normalizations': [self.correction()], 'partial_validation': False,
        }

    def write_segment(self, value=None):
        path = self.segment_path()
        executor._write(path, self.segment_value() if value is None else value)
        return path

    def test_prompt_states_integer_range_and_preserves_supplied_evidence(self):
        prompt = json.loads(reader_prompt(self.task))
        self.assertIn('relevance must be an integer from 1 to 5', prompt['task'])
        self.assertIn('use 3 when uncertain', prompt['task'])
        self.assertEqual(prompt['schema']['findings'][0]['relevance'], 1)
        self.assertEqual(prompt['original_materials'][0]['text'],
                         self.task['segments'][0]['text'])

    def test_valid_integer_scores_are_unchanged_without_corrections(self):
        for rank in range(1, 6):
            with self.subTest(rank=rank):
                result = validate_reader(self.response([self.finding(rank)]), self.task)
                self.assertEqual(result['findings'][0]['relevance'], rank)
                self.assertNotIn('normalizations', result)

    def test_missing_score_keeps_existing_neutral_default(self):
        finding = self.finding()
        del finding['relevance']
        result = validate_reader(self.response([finding]), self.task)
        self.assertEqual(result['findings'][0]['relevance'], 3)
        self.assertNotIn('normalizations', result)

    def test_observed_out_of_range_integer_scores_are_discarded_not_rescaled(self):
        for rank in range(6, 11):
            with self.subTest(rank=rank):
                raw = encoded(self.response([self.finding(rank)]))
                result = validate_reader(raw, self.task)
                finding = result['findings'][0]
                self.assertEqual(finding['relevance'], 3)
                self.assertEqual(result['normalizations'], [self.correction(rank)])
                self.assertEqual(finding['quote'], self.task['segments'][0]['text'])
                self.assertEqual(finding['summary'], self.task['segments'][0]['text'])
                self.assertEqual(json.loads(raw)['findings'][0]['relevance'], rank)

    def test_existing_fractional_scale_compatibility_is_unchanged(self):
        for rank, expected in [(0.0, 1), (0.25, 2), (0.5, 3), (0.75, 4), (1.0, 5)]:
            with self.subTest(rank=rank):
                result = validate_reader(self.response([self.finding(rank)]), self.task)
                self.assertEqual(result['findings'][0]['relevance'], expected)
                self.assertEqual(result['normalizations'][0]['code'],
                                 'fractional_relevance_normalized_to_integer_scale')

    def test_other_invalid_values_remain_rejected(self):
        invalid = [0, -1, 11, 100, 1.5, 8.0, -0.1, '8', True, False, None,
                   float('nan'), float('inf'), float('-inf')]
        for rank in invalid:
            with self.subTest(rank=repr(rank), kind=type(rank).__name__):
                self.assert_error(self.response([self.finding(rank)]), 'invalid_reading')

    def test_legacy_contracts_do_not_accept_out_of_range_scores(self):
        for contract in (1, 2):
            with self.subTest(contract=contract):
                task = dict(self.task, reader_contract=contract)
                self.assert_error(self.response([self.finding(8)], task=task),
                                  'invalid_reading', task=task)

    def test_invalid_quote_is_not_rescued_by_discarding_its_score(self):
        value = self.response([self.finding(8, quote='This text is absent.')])
        self.assert_error(value, 'reading_quote_mismatch', grounded=True)

    def test_partial_grounded_result_excludes_bad_quote_and_keeps_only_valid_correction(self):
        bad = self.finding(10, quote='This text is absent.')
        good = self.finding(8)
        result = validate_grounded_reader(self.response([bad, good]), self.task)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['quote'], good['quote'])
        self.assertEqual(result['findings'][0]['relevance'], 3)
        self.assertEqual(result['normalizations'], [self.correction(8)])
        self.assertEqual([r['code'] for r in result['rejected_findings']],
                         ['reading_quote_mismatch'])

    def test_numeric_topic_guard_is_still_strict(self):
        self.assert_error(self.response([self.finding(8, topic='Result 999')]),
                          'reading_numeric_mismatch', grounded=True)

    def test_unsupported_summary_number_keeps_existing_exact_quote_replacement(self):
        result = validate_reader(self.response([self.finding(8, summary='99 new results')]),
                                 self.task)
        self.assertEqual(result['findings'][0]['summary'], self.task['segments'][0]['text'])
        self.assertEqual([n['code'] for n in result['normalizations']],
                         ['unsupported_numeric_summary_replaced_with_exact_quote',
                          'out_of_range_relevance_discarded'])

    def test_foreign_image_and_data_ids_remain_rejected(self):
        for field in ('source_asset_ids', 'data_ids'):
            with self.subTest(field=field):
                finding = self.finding(8, **{field: ['foreign-id']})
                self.assert_error(self.response([finding]), 'reading_source_id_mismatch',
                                  grounded=True)

    def test_scope_ack_cannot_be_replaced_by_a_valid_finding(self):
        self.assert_error(self.response([self.finding(8)], read_material_ids=[]),
                          'reading_coverage_missing', grounded=True)

    def test_unknown_root_and_finding_fields_remain_rejected(self):
        self.assert_error(self.response([self.finding(8)], ignored=True),
                          'invalid_reading', grounded=True)
        self.assert_error(self.response([self.finding(8, ignored=True)]),
                          'invalid_reading', grounded=True)

    def test_invalid_issues_remain_atomic(self):
        for issues in ('not-a-list', [False], [{}]):
            with self.subTest(issues=issues):
                self.assert_error(self.response([self.finding(8)], issues=issues),
                                  'invalid_reading', grounded=True)

    def test_visual_transcription_scores_are_not_in_the_new_recovery_scope(self):
        task = deepcopy(self.task)
        task['segments'][0]['source'].update(visual_only=True, source_asset_id='image-fixture')
        task['visual_inputs'] = [{'id': 'image-fixture', 'sha256': sha('self-created-image')}]
        value = self.response([], task=task, image_findings=[
            {'image_id': 'i1', 'text': 'Visible fixture text',
             'topic': '原文证据', 'relevance': 8}])
        self.assert_error(value, 'reading_source_id_mismatch', task=task, grounded=True)

    def test_existing_raw_cache_replays_without_gateway_or_writes(self):
        path = self.save_response()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*')
                  if p.is_file()}
        gateway = types.SimpleNamespace(text=Mock(side_effect=AssertionError('NO_PROVIDER')))
        pipeline = types.SimpleNamespace(config={}, gateway=gateway)
        result = executor.run_reader(self.task, pipeline, self.root, 'new-job', False,
                                     lambda: False)
        gateway.text.assert_not_called()
        self.assertEqual(result['model_report'],
                         {'provider_called': False, 'response_reused': True})
        self.assertEqual(result['result']['normalizations'], [self.correction()])
        self.assertEqual(result['result']['findings'][0]['relevance'], 3)
        self.assertEqual({p.relative_to(self.root): p.read_bytes()
                          for p in self.root.rglob('*') if p.is_file()}, before)
        self.assertEqual(json.loads(path.read_text())['raw_sha256'],
                         sha(json.loads(path.read_text())['raw']))

    def test_bad_raw_cache_hash_is_not_reused(self):
        path = self.save_response(raw_sha256=sha('wrong'))
        before = path.read_bytes()
        self.assertIsNone(executor._cached_response(self.task, self.root))
        self.assertEqual(path.read_bytes(), before)

    def test_foreign_task_cache_is_not_reused(self):
        path = self.save_response(task_id='read:foreign-task')
        before = path.read_bytes()
        self.assertIsNone(executor._cached_response(self.task, self.root))
        self.assertEqual(path.read_bytes(), before)

    def test_length_limited_raw_cache_is_not_reused(self):
        path = self.save_response(finish_reason='length')
        before = path.read_bytes()
        self.assertIsNone(executor._cached_response(self.task, self.root))
        self.assertEqual(path.read_bytes(), before)

    def test_fresh_mock_response_persists_raw_and_audited_segment_cache(self):
        raw = encoded(self.response([self.finding(9)]))
        gateway = types.SimpleNamespace(text=Mock(return_value=raw),
            last_response_metadata={'finish_reason': 'stop', 'usage': {'total_tokens': 31}})
        pipeline = types.SimpleNamespace(config={}, gateway=gateway)
        result = executor.run_reader(self.task, pipeline, self.root, 'mock-job', False,
                                     lambda: False)
        self.assertEqual(gateway.text.call_count, 1)
        self.assertTrue(result['model_report']['provider_called'])
        saved = json.loads((self.root / 'attempt-mock-job' / 'response.json').read_text())
        self.assertEqual(saved['raw'], raw)
        self.assertEqual(json.loads(saved['raw'])['findings'][0]['relevance'], 9)
        cache = json.loads(self.segment_path().read_text())
        self.assertEqual(cache['findings'][0]['relevance'], 3)
        self.assertEqual(cache['normalizations'], [self.correction(9)])

    def test_segment_only_cache_preserves_audited_neutral_score(self):
        path = self.write_segment()
        before = path.read_bytes()
        result = executor._cached_response(self.task, self.root)
        self.assertEqual(result['model_report'],
                         {'provider_called': False, 'response_reused': True})
        self.assertEqual(result['result']['normalizations'], [self.correction()])
        self.assertEqual(result['result']['findings'][0]['relevance'], 3)
        self.assertEqual(path.read_bytes(), before)

    def test_segment_correction_whitelist_rejects_wrong_keys_types_and_values(self):
        changes = [
            {'extra': 'field'}, {'code': 'untrusted-correction'},
            {'segment_id': 'seg:foreign'}, {'field': 'quote'},
            {'original_value': 5}, {'original_value': 11}, {'original_value': 8.0},
            {'original_value': True}, {'normalized_value': 4},
            {'normalized_value': 3.0}, {'normalized_value': True},
        ]
        for change in changes:
            with self.subTest(change=change):
                cache = self.segment_value()
                cache['normalizations'][0].update(change)
                self.write_segment(cache)
                self.assertIsNone(executor._cached_response(self.task, self.root))
        cache = self.segment_value()
        del cache['normalizations'][0]['original_value']
        self.write_segment(cache)
        self.assertIsNone(executor._cached_response(self.task, self.root))

    def test_segment_correction_requires_whole_material_contract(self):
        self.write_segment()
        for contract in (1, 2):
            with self.subTest(contract=contract):
                self.assertIsNone(executor._cached_response(
                    dict(self.task, reader_contract=contract), self.root))

    def test_segment_correction_requires_neutral_integer_finding_in_this_segment(self):
        variants = [[], [self.finding(4)], [self.finding(3.0)],
                    [self.finding(3, segment_id='seg:foreign')]]
        for findings in variants:
            with self.subTest(findings=findings):
                cache = self.segment_value()
                cache['findings'] = findings
                self.write_segment(cache)
                self.assertIsNone(executor._cached_response(self.task, self.root))

    def test_partial_validation_segment_cache_is_not_promoted_to_full_scope(self):
        cache = self.segment_value()
        cache['partial_validation'] = True
        self.write_segment(cache)
        self.assertIsNone(executor._cached_response(self.task, self.root))

    def test_segment_source_hash_and_key_remain_bound(self):
        for field in ('source_text_sha256', 'cache_key', 'segment_id'):
            with self.subTest(field=field):
                cache = self.segment_value()
                cache[field] = 'foreign-value'
                self.write_segment(cache)
                self.assertIsNone(executor._cached_response(self.task, self.root))

    def test_markdown_explains_discarded_scores_without_claiming_new_evidence(self):
        result = validate_reader(self.response([self.finding(8)]), self.task)
        report = {
            'reader_contract': 3, 'materials': [{'material_id': 'material-rating-fixture',
                'filename': 'self-created.txt', 'status': 'prepared'}],
            'slide_specs': [], 'total_materials': 1, 'total_segments': 1,
            'completed_segments': 1, 'failed_segments': 0, 'coverage_complete': True,
            'submitted_materials': 1, 'selected_materials': 1, 'excluded_materials': 0,
            'issues': [], 'findings': result['findings'],
            'normalizations': result['normalizations'], 'normalization_count': 1,
            'data_alignment': {'candidate_differences': []},
            'web_supplement': {'status': 'not_requested', 'sources': []},
        }
        draft = markdown_draft(report, self.task['brief'], self.task['segments'])
        self.assertIn('舍弃超范围的相关性排序分数 1 处，使用中性默认值', draft)
        self.assertIn('未改变证据内容', draft)
        self.assertIn(self.task['segments'][0]['text'], draft)
        self.assertIn('原文引用、数字单位与完整原文确认仍经过完整校验', draft)


if __name__ == '__main__':
    unittest.main()
