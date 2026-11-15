"""Job timestamps and reasoning rounds use self-created data, never live jobs."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from .jobs import execution_timing, public_turn, reduce_event


class ExecutionStatusTests(unittest.TestCase):
    def test_queued_running_terminal_and_cancelled_queue_times(self):
        row = {'created': 10, 'started': None, 'finished': None, 'status': 'queued'}
        self.assertEqual(execution_timing(row, 15), {'sampled_at': 15, 'phase': 'queued',
            'queue_seconds': 5, 'execution_seconds': None, 'total_seconds': 5})
        row.update(started=14, status='running')
        self.assertEqual(execution_timing(row, 20), {'sampled_at': 20, 'phase': 'running',
            'queue_seconds': 4, 'execution_seconds': 6, 'total_seconds': 10})
        row.update(status='complete', finished=30)
        self.assertEqual(execution_timing(row, 99)['total_seconds'], 20)
        self.assertEqual(execution_timing(row, 99)['execution_seconds'], 16)
        row.update(status='stopped', started=None, finished=18)
        self.assertEqual(execution_timing(row, 99)['queue_seconds'], 8)
        self.assertIsNone(execution_timing(row, 99)['execution_seconds'])

    def test_missing_or_invalid_timestamps_do_not_invent_execution_times(self):
        value = execution_timing({'created': 10, 'status': 'running'}, 20)
        self.assertEqual(value['total_seconds'], 10)
        self.assertIsNone(value['queue_seconds']); self.assertIsNone(value['execution_seconds'])
        value = execution_timing({'created': 10, 'started': 14, 'status': 'error'}, 99)
        self.assertEqual(value['queue_seconds'], 4)
        self.assertIsNone(value['execution_seconds']); self.assertIsNone(value['total_seconds'])
        for bad in [None, True, float('inf'), '10']:
            self.assertIsNone(execution_timing({'created': bad, 'status': 'queued'}, 20))

    def test_public_read_derives_metadata_without_modifying_stored_turn_or_meta(self):
        turn = {'answer': 'self answer', 'reasoning': 'self thought', 'meta': {'seconds': 3, 'first_token_seconds': .2}, '_usage': {'total_tokens': 9}}
        row = {'turn': json.dumps(turn), 'created': 10, 'started': 12, 'finished': 20, 'status': 'complete', 'revision': 3}
        original = deepcopy(row)
        with patch('official.jobs.enrich_turn', side_effect=lambda value, _: value):
            first = public_turn(row); second = public_turn(row)
        self.assertEqual(row, original); self.assertEqual(json.loads(row['turn']), turn)
        self.assertEqual(first['executionTiming']['total_seconds'], 10)
        self.assertEqual(first['executionTiming']['queue_seconds'], 2)
        self.assertEqual(first['executionTiming']['execution_seconds'], 8)
        self.assertEqual(first['meta'], turn['meta']); self.assertNotIn('_usage', first)
        self.assertEqual(first['executionTiming']['total_seconds'], second['executionTiming']['total_seconds'])

    def test_reasoning_rounds_keep_exact_compatibility_text_and_order(self):
        turn = {'answer': '', 'reasoning': '', 'toolCalls': []}
        for event in [{'type': 'reasoning', 'round': 1, 'text': 'first '},
                      {'type': 'reasoning', 'round': 1, 'text': 'thought😀'},
                      {'type': 'content', 'text': 'preamble'}, {'type': 'tool_plan'},
                      {'type': 'reasoning', 'round': 2, 'text': 'second thought'}]:
            reduce_event(turn, event)
        self.assertEqual(turn['reasoning'], 'first thought😀second thought')
        self.assertEqual(turn['reasoningRounds'], [{'round': 1, 'text': 'first thought😀'}, {'round': 2, 'text': 'second thought'}])
        self.assertEqual(turn['answer'], '')

    def test_legacy_or_inconsistent_rounds_keep_all_original_reasoning(self):
        for rounds in [None, 'bad', [{'round': 1, 'text': 'not the stored reasoning'}]]:
            turn = {'answer': '', 'reasoning': 'legacy', 'toolCalls': [], 'reasoningRounds': rounds}
            reduce_event(turn, {'type': 'reasoning', 'round': 2, 'text': ' current'})
            self.assertEqual(turn['reasoningRounds'], [{'round': None, 'text': 'legacy'}, {'round': 2, 'text': ' current'}])
        turn = {'answer': '', 'reasoning': '', 'toolCalls': []}
        reduce_event(turn, {'type': 'reasoning', 'round': True, 'text': 'unknown'})
        self.assertEqual(turn['reasoningRounds'], [{'round': None, 'text': 'unknown'}])
        self.assertEqual(turn['reasoning'], 'unknown')


if __name__ == '__main__':
    unittest.main()
