"""Quota regression tests. Disposable state and a synthetic runner only."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from official import store as db
from official.jobs import Scheduler, public_turn, reduce_event, settle_accounting
from official.quota_limits import daily_exceeded


class FailureAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(db, 'DATA', Path(self.temp.name))
        self.data_patch.start()
        db.init()
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                   ('user', 'fixture@example.invalid', 'Fixture', 'Test', '其他', 'user', time.time()))
        db.execute('INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)',
                   ('conversation', 'user', 'Fixture', time.time(), time.time()))

    def tearDown(self):
        self.data_patch.stop()
        self.temp.cleanup()

    def row(self):
        turn = {'status': 'running', 'answer': '', 'reasoning': '', 'toolCalls': [],
                'request': {'max_tokens': 2048}}
        db.execute('INSERT INTO jobs(id,user_id,conversation_id,status,input,turn,created,reserved,inflight,day) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('job', 'user', 'conversation', 'running', '{}', json.dumps(turn), time.time(), 1040000, 32768, 'fixture-day'))
        return db.query('SELECT * FROM jobs WHERE id=?', ('job',), one=True)

    async def run_events(self, events):
        row = self.row()
        async def runner(row, output):
            for event in events:
                if isinstance(event, BaseException):
                    raise event
                yield event
        await Scheduler(runner).run(row, False)
        saved = db.query('SELECT * FROM jobs WHERE id=?', ('job',), one=True)
        return saved, json.loads(saved['turn'])

    async def test_first_upstream_502_releases_full_reservation(self):
        saved, turn = await self.run_events([{'type': 'model_round', 'tools_available': False},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual((saved['status'], saved['charged']), ('error', 0))
        self.assertEqual(turn['accounting']['basis'], 'upstream_rejected_before_output')
        end = json.loads(db.query("SELECT event FROM job_events WHERE job_id='job' ORDER BY sequence DESC", one=True)['event'])
        self.assertEqual(end['accounting'], turn['accounting'])
        self.assertNotIn('_accounting_upstream_rejection', turn)

    async def test_ordinary_account_can_reserve_next_job_after_rejection(self):
        await self.run_events([{'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        # Same aggregation used by submit; the failed question still counts once.
        with db.connect() as conn:
            tokens, questions = conn.execute("SELECT COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN reserved ELSE charged END),0),COUNT(*) FROM jobs WHERE user_id=? AND day=?", ('user', 'fixture-day')).fetchone()
        self.assertEqual((tokens, questions), (0, 1))
        self.assertFalse(daily_exceeded('user', {'daily_tokens': 2000000, 'daily_questions': 100,
                                               'job_token_budget': 1040000}, questions, tokens))

    async def test_trace_rejection_survives_final_error_without_status(self):
        saved, _ = await self.run_events([{'type': 'model_request'},
            {'type': 'model_response_error', 'upstream_status': 502},
            {'type': 'error', 'message': 'Synthetic final adapter error'}])
        self.assertEqual(saved['charged'], 0)

    async def test_retry_clears_previous_rejection_before_unknown_failure(self):
        saved, _ = await self.run_events([{'type': 'model_request'},
            {'type': 'model_response_error', 'upstream_status': 400},
            {'type': 'model_request'}, TimeoutError()])
        self.assertEqual(saved['charged'], 1040000)

    async def test_model_response_consumption_prevents_zero_charge(self):
        saved, _ = await self.run_events([{'type': 'model_response'},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual(saved['charged'], 1040000)

    async def test_partial_content_then_rejection_keeps_unknown_use(self):
        saved, _ = await self.run_events([{'type': 'content', 'text': 'Partial'},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual(saved['charged'], 1040000)

    async def test_reasoning_only_then_rejection_keeps_unknown_use(self):
        saved, _ = await self.run_events([{'type': 'reasoning', 'text': 'Partial reasoning'},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual(saved['charged'], 1040000)

    async def test_tool_plan_even_without_visible_answer_keeps_unknown_use(self):
        saved, _ = await self.run_events([{'type': 'tool_plan'},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual(saved['charged'], 1040000)

    async def test_existing_round_usage_prevents_free_multiround_failure(self):
        saved, _ = await self.run_events([{'type': 'usage', 'usage': {'total_tokens': 200}, 'estimated': False},
            {'type': 'error', 'message': 'Synthetic rejection', 'upstream_status': 502}])
        self.assertEqual(saved['charged'], 1040000)

    async def test_timeout_without_reported_rejection_stays_conservative(self):
        saved, _ = await self.run_events([TimeoutError()])
        self.assertEqual(saved['charged'], 1040000)

    async def test_connection_exception_without_rejection_stays_conservative(self):
        saved, _ = await self.run_events([RuntimeError('synthetic')])
        self.assertEqual(saved['charged'], 1040000)

    async def test_incomplete_with_actual_usage_preserves_exact_accounting(self):
        saved, turn = await self.run_events([{'type': 'content', 'text': 'Partial'},
            {'type': 'done', 'complete': False, 'usage': {'total_tokens': 207}, 'usage_estimated': False}])
        self.assertEqual(saved['charged'], 207)
        self.assertEqual(turn['accounting'], {'tokens': 207, 'estimated': False})

    async def test_estimated_usage_is_not_marked_exact(self):
        saved, turn = await self.run_events([{'type': 'done', 'complete': False,
            'usage': {'total_tokens': 207}, 'usage_estimated': True}])
        self.assertEqual(saved['charged'], 1040000)
        self.assertTrue(turn['accounting']['estimated'])

    def test_untrusted_status_type_does_not_release(self):
        for value in [True, '502', 200, 600, None]:
            turn = {'status': 'running', 'answer': '', 'reasoning': '', 'toolCalls': []}
            reduce_event(turn, {'type': 'error', 'message': 'synthetic', 'upstream_status': value})
            self.assertEqual(settle_accounting(turn, 1040000)['tokens'], 1040000)

    def test_public_turn_hides_internal_accounting_evidence(self):
        row = self.row()
        turn = json.loads(row['turn'])
        turn.update(_accounting_progress=True, _accounting_upstream_rejection=True)
        row['turn'] = json.dumps(turn)
        self.assertNotIn('_accounting_progress', public_turn(row))
        self.assertNotIn('_accounting_upstream_rejection', public_turn(row))


if __name__ == '__main__':
    unittest.main()
