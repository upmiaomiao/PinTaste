"""Real HTTP admission + official pool on disposable PG; no scheduler/model."""
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from . import store as db
from . import app_main as main
from . import test_model_budget as admission_cases
from .pg_test_fixture import PlatformPGFixture


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'requires guarded disposable PG fixture')
class PostgreSQLAdmissionPoolTests(unittest.TestCase):
    def setUp(self):
        fixture = PlatformPGFixture()
        fixture.__enter__()
        self.addCleanup(fixture.close)
        activation = fixture.activate()
        activation.__enter__()
        self.addCleanup(activation.__exit__, None, None, None)
        guard = patch.object(sqlite3, 'connect', side_effect=AssertionError('PG_ADMISSION_MUST_NOT_CONNECT_SQLITE'))
        guard.start(); self.addCleanup(guard.stop)
        self._client_entered = False
        self.addCleanup(self.close_admission)
        original_directory = admission_cases.tempfile.TemporaryDirectory
        def temporary_data(*args, **kwargs):
            directory = original_directory(*args, **kwargs)
            try:
                # Only this disposable PG fixture's own key, never a live key.
                key = Path(directory.name)/'vault.key'
                shutil.copyfile(fixture.data/'vault.key', key)
                key.chmod(0o600)
            except BaseException:
                directory.cleanup()
                raise
            return directory
        owner = self
        class AdmissionClient(admission_cases.TestClient):
            def __enter__(client):
                result = super().__enter__()
                owner._client_entered = True
                return result
        with patch.object(admission_cases.tempfile, 'TemporaryDirectory', side_effect=temporary_data), \
                patch.object(admission_cases, 'TestClient', AdmissionClient):
            admission_cases.ModelBudgetAdmissionTests.setUp(self)
        self.assertTrue(db.is_postgres())
        self.assertFalse((db.DATA/'platform.sqlite3').exists())

    def close_admission(self):
        try:
            if self._client_entered:
                self._client_entered = False
                self.client.__exit__(None, None, None)
        finally:
            try:
                if hasattr(self, 'data'): self.data.stop()
            finally:
                if hasattr(self, 'temp'): self.temp.cleanup()

    new_user = admission_cases.ModelBudgetAdmissionTests.new_user
    conversation = admission_cases.ModelBudgetAdmissionTests.conversation
    submit = admission_cases.ModelBudgetAdmissionTests.submit
    job = admission_cases.ModelBudgetAdmissionTests.job
    test_concurrent_same_request_is_exactly_once = admission_cases.ModelBudgetAdmissionTests.test_same_request_concurrent_replay_creates_one_frozen_job
    test_concurrent_requests_keep_atomic_daily_quota = admission_cases.ModelBudgetAdmissionTests.test_concurrent_distinct_jobs_cannot_oversubscribe_daily_reservation
    test_frozen_existing_job_not_repriced = admission_cases.ModelBudgetAdmissionTests.test_existing_job_and_replay_keep_original_reservation_after_model_change
    def test_four_simultaneous_accounts_share_pool_without_nested_borrow_or_data_bleed(self):
        users = [self.user] + [self.new_user('separate') for _ in range(3)]
        conversations = [self.conversation(user) for user in users]
        barrier = threading.Barrier(4)
        original_build = main.memory.build
        def overlap(*args, **kwargs):
            barrier.wait(timeout=15)
            return original_build(*args, **kwargs)
        def send(index):
            barrier.wait(timeout=10)
            return self.submit(user=users[index], cid=conversations[index])
        with patch.object(main.memory, 'build', overlap), ThreadPoolExecutor(4) as workers:
            responses = list(workers.map(send, range(4)))
        rows = [self.job(response) for response in responses]
        self.assertEqual(len({row['id'] for row in rows}), 4)
        self.assertEqual([row['user_id'] for row in rows], [user['id'] for user in users])
        self.assertEqual([row['reserved'] for row in rows], [131072] * 4)
        for index, user in enumerate(users):
            self.assertEqual(len(db.query('SELECT id FROM jobs WHERE user_id=?', (user['id'],))), 1)
            own = self.client.get('/api/conversations/'+conversations[index], headers=user['headers'])
            self.assertEqual(own.status_code, 200)
            self.assertEqual([turn['id'] for turn in own.json()['turns']], [rows[index]['id']])
            other = self.client.get('/api/conversations/'+conversations[(index+1)%4], headers=user['headers'])
            self.assertEqual(other.status_code, 404)


if __name__ == '__main__': unittest.main()
