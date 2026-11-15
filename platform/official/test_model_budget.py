"""Real API admission and SQLite quota regressions using synthetic identities only."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
from official import app_main as main, auth, quota_limits, store as db
from official.jobs import Scheduler


class ModelBudgetAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = patch.object(db, 'DATA', Path(self.temp.name))
        self.data.start()
        self.assertEqual(main.runtime.chat_execution_mode, 'api')
        self.assertIsNone(main.scheduler)
        self.client = TestClient(main.app, base_url='http://127.0.0.1:8920')
        self.client.__enter__()
        db.save_settings({'context_window': 260000, 'max_output_tokens': 160000,
            'job_token_budget': 1040000, 'daily_tokens': 2000000,
            'daily_questions': 30, 'max_parallel': 2, 'per_user_parallel': 1,
            'per_user_queue': 3, 'max_queue': 64, 'inflight_token_budget': 520000,
            'additional_models': [{'id': 'small', 'display_name': 'Synthetic small model',
                'model': 'synthetic-small', 'base_url': 'https://model.invalid/v1',
                'context_window': 32768, 'max_output_tokens': 2048,
                'protocol': 'baseline', 'enabled': True, 'supports_tools': True}],
            'default_model_id': 'small'}, 'self-fixture')
        db.save_secret('model', 'self-fixture-main-key')
        db.save_secret('provider_small', 'self-fixture-small-key')
        db.save_secret('search', 'self-fixture-search-key')
        self.user = self.new_user('ordinary')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.data.stop()
        self.temp.cleanup()

    def new_user(self, prefix):
        uid = prefix + uuid.uuid4().hex
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                   (uid, uid + '@example.invalid', 'Self fixture', 'Self fixture', '其他', 'user', time.time()))
        token = uuid.uuid4().hex
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (db.digest(token), uid, time.time() + 600))
        return {'id': uid, 'headers': {'Cookie': auth.COOKIE + '=' + token}}

    def conversation(self, user=None):
        user = self.user if user is None else user
        r = self.client.post('/api/conversations', json={}, headers=user['headers'])
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['id']

    def submit(self, *, user=None, cid=None, request_id=None, **extra):
        user = self.user if user is None else user
        return self.client.post('/api/jobs', headers=user['headers'], json={
            'conversation_id': cid or self.conversation(user),
            'request_id': request_id or uuid.uuid4().hex,
            'message': 'Synthetic budget question', 'model_id': 'small', **extra})

    def job(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return db.query('SELECT * FROM jobs WHERE id=?', (response.json()['id'],), one=True)

    def test_three_small_jobs_use_selected_window_and_fourth_keeps_queue_cap(self):
        rows = [self.job(self.submit()) for _ in range(3)]
        self.assertEqual([r['reserved'] for r in rows], [131072] * 3)
        self.assertEqual([r['inflight'] for r in rows], [32768] * 3)
        self.assertEqual(self.submit().status_code, 429)
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 3)
        self.assertEqual(db.settings()['job_token_budget'], 1040000)

    def test_network_and_plain_requests_keep_same_four_window_policy(self):
        plain = self.job(self.submit(network_search=False))
        network = self.job(self.submit(network_search=True))
        self.assertEqual((plain['reserved'], network['reserved']), (131072, 131072))
        self.assertTrue(json.loads(network['input'])['tools'])

    def test_default_model_resolution_uses_selected_small_budget(self):
        r = self.submit(model_id=None)
        row = self.job(r)
        self.assertEqual(row['reserved'], 131072)
        self.assertEqual(json.loads(row['input'])['config']['model_id'], 'small')

    def test_base_and_small_share_daily_ledger_with_different_reservations(self):
        big = self.job(self.submit(model_id='team'))
        small = self.job(self.submit())
        self.assertEqual((big['reserved'], small['reserved']), (1040000, 131072))
        self.assertEqual(self.submit(model_id='team').status_code, 429)
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 2)

    def test_exact_daily_balance_allows_boundary_and_rejects_one_token_short(self):
        db.save_settings({'daily_tokens': 131072}, 'self-fixture')
        self.job(self.submit())
        other = self.new_user('other')
        db.save_settings({'daily_tokens': 131071}, 'self-fixture')
        self.assertEqual(self.submit(user=other).status_code, 429)
        self.assertEqual(len(db.query('SELECT id FROM jobs WHERE user_id=?', (other['id'],))), 0)

    def test_operator_global_cap_remains_hard_upper_bound(self):
        db.save_settings({'context_window': 32768, 'max_output_tokens': 2048,
                          'job_token_budget': 100000}, 'self-fixture')
        self.assertEqual(self.job(self.submit())['reserved'], 100000)

    def test_existing_job_and_replay_keep_original_reservation_after_model_change(self):
        cid = self.conversation(); request_id = uuid.uuid4().hex
        first = self.job(self.submit(cid=cid, request_id=request_id))
        saved = first['input']; turn = first['turn']
        db.save_settings({'job_token_budget': 65536, 'additional_models': []}, 'self-fixture')
        repeat = self.job(self.submit(cid=cid, request_id=request_id))
        self.assertEqual((repeat['reserved'], repeat['input'], repeat['turn']), (131072, saved, turn))
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 1)

    def test_legacy_existing_job_is_not_repriced_by_idempotent_replay(self):
        cid = self.conversation(); request_id = uuid.uuid4().hex
        row = self.job(self.submit(cid=cid, request_id=request_id))
        db.execute('UPDATE jobs SET reserved=? WHERE id=?', (1040000, row['id']))
        original = db.query('SELECT * FROM jobs WHERE id=?', (row['id'],), one=True)
        repeated = self.job(self.submit(cid=cid, request_id=request_id))
        self.assertEqual(repeated, original)

    def test_pending_reserved_and_terminal_charged_have_distinct_ledger_meanings(self):
        first = self.job(self.submit())
        db.save_settings({'daily_tokens': 131172}, 'self-fixture')
        self.assertEqual(self.submit().status_code, 429)
        db.execute("UPDATE jobs SET status='complete',charged=100 WHERE id=?", (first['id'],))
        self.assertEqual(self.job(self.submit())['reserved'], 131072)

    def test_daily_question_limit_is_not_bypassed_by_small_reservation(self):
        db.save_settings({'daily_questions': 1}, 'self-fixture')
        first = self.job(self.submit())
        db.execute("UPDATE jobs SET status='error',charged=0 WHERE id=?", (first['id'],))
        self.assertEqual(self.submit().status_code, 429)

    def test_queue_cancel_releases_selected_reservation_without_model_call(self):
        db.save_settings({'daily_tokens': 131072}, 'self-fixture')
        first = self.job(self.submit())
        self.assertEqual(self.submit().status_code, 429)
        r = self.client.post('/api/jobs/' + first['id'] + '/cancel', json={}, headers=self.user['headers'])
        self.assertEqual(r.status_code, 200)
        current = db.query('SELECT * FROM jobs WHERE id=?', (first['id'],), one=True)
        self.assertEqual((current['status'], current['charged'], current['reserved']), ('stopped', 0, 131072))
        self.assertEqual(self.job(self.submit())['reserved'], 131072)

    def test_101_first_rejection_releases_new_small_job_reservation(self):
        row = self.job(self.submit())
        async def reject(row, output):
            yield {'type': 'error', 'message': 'Synthetic HTTP rejection', 'upstream_status': 502}
        asyncio.run(Scheduler(reject).run(row, False))
        current = db.query('SELECT * FROM jobs WHERE id=?', (row['id'],), one=True)
        self.assertEqual((current['status'], current['reserved'], current['charged']), ('error', 131072, 0))
        self.assertEqual(json.loads(current['turn'])['accounting']['tokens'], 0)

    def test_same_request_concurrent_replay_creates_one_frozen_job(self):
        cid = self.conversation(); request_id = uuid.uuid4().hex
        barrier = threading.Barrier(2)
        original_build = main.memory.build
        def overlap(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_build(*args, **kwargs)
        def send(_):
            barrier.wait(timeout=5)
            return self.submit(cid=cid, request_id=request_id)
        with patch.object(main.memory, 'build', overlap), ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(send, range(2)))
        rows = [self.job(r) for r in responses]
        self.assertEqual(rows[0]['id'], rows[1]['id'])
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 1)

    def test_concurrent_distinct_jobs_cannot_oversubscribe_daily_reservation(self):
        db.save_settings({'daily_tokens': 131072}, 'self-fixture')
        cids = [self.conversation(), self.conversation()]
        barrier = threading.Barrier(2)
        original_build = main.memory.build
        def overlap(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_build(*args, **kwargs)
        def send(cid):
            barrier.wait(timeout=5)
            return self.submit(cid=cid)
        with patch.object(main.memory, 'build', overlap), ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(send, cids))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 429])
        rows = db.query('SELECT * FROM jobs')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reserved'], 131072)

    def test_request_cannot_override_reservation_or_operator_budget(self):
        for field in ['reserved', 'reservation', 'job_token_budget']:
            self.assertEqual(self.submit(**{field: 1}).status_code, 422)
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 0)

    def test_bad_model_budget_fails_closed_before_inserting_job(self):
        db.save_settings({'job_token_budget': True}, 'self-fixture')
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(db.query('SELECT id FROM jobs')), 0)


class ReservationContractTests(unittest.TestCase):
    def test_selected_context_cap_and_explicit_low_operator_cap(self):
        for cap, window, expected in [(1040000,32768,131072), (1040000,260000,1040000),
                                      (100000,32768,100000), (65536,8192,32768)]:
            with self.subTest(cap=cap, window=window):
                self.assertEqual(quota_limits.job_reservation({'job_token_budget':cap,'context_window':window}), expected)

    def test_invalid_budget_types_and_nonpositive_limits_fail_closed(self):
        for cap, window in [(True,32768), (1040000,False), (0,32768), (-1,32768),
                            (1040000,0), ('1040000',32768), (1040000,32768.0)]:
            with self.subTest(cap=cap, window=window):
                self.assertRaises(ValueError, quota_limits.job_reservation,
                                  {'job_token_budget':cap,'context_window':window})


if __name__ == '__main__':
    unittest.main()
