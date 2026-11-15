"""Real PostgreSQL login, identity, preference and export boundaries.

Each case owns one disposable loopback fixture database and a DML-only role.
Only synthetic users are created; no live account, bridge or model is used.
"""
import json
import os
import sqlite3
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from . import test_model_budget as api
from . import data_export, integrated_identity as identity
from .pg_test_fixture import PlatformPGFixture


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PGPlatformBusinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PlatformPGFixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.close)
        activation = self.fixture.activate()
        activation.__enter__()
        self.addCleanup(activation.__exit__, None, None, None)
        guard = patch.object(sqlite3, 'connect', side_effect=AssertionError('PG_BUSINESS_MUST_NOT_CONNECT_SQLITE'))
        guard.start()
        self.addCleanup(guard.stop)
        self.assertEqual(api.main.runtime.chat_execution_mode, 'api')
        self.assertIsNone(api.main.scheduler)
        self.client = api.TestClient(api.main.app, base_url='http://127.0.0.1:8920')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.user = self.new_user()

    def new_user(self):
        uid = uuid.uuid4().hex
        email = uid + '@example.invalid'
        password = 'Synthetic PG passphrase 123'
        encoded = api.auth.hash_password(password)
        with api.db.connect() as conn:
            conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                         (uid, email, 'Synthetic profile', 'Synthetic organization', '其他', 'user', time.time()))
            conn.execute('INSERT INTO user_passwords VALUES(?,?,?)', (uid, encoded, time.time()))
            token = uuid.uuid4().hex
            conn.execute('INSERT INTO sessions VALUES(?,?,?)', (api.db.digest(token), uid, time.time() + 600))
        return {'id': uid, 'email': email, 'password': password,
                'headers': {'Cookie': api.auth.COOKIE + '=' + token}}

    def login(self, password=None):
        return self.client.post('/api/auth/login', json={
            'email': self.user['email'], 'password': password or self.user['password']})

    def test_102_concurrent_pending_claims_are_atomic_and_source_scoped(self):
        gate = threading.Barrier(16)
        email = self.user['email']

        def claim(_):
            gate.wait(timeout=10)
            try:
                return ('ok', api.auth._begin_login(email, '198.51.100.10'))
            except HTTPException as error:
                return ('error', error)

        # Freeze only auth's local clock. A real clock sampled before the write
        # lock can otherwise produce ceil(300 + subsecond lock scheduling)=301
        # even when the policy is correct (also on the original SQLite path).
        fixed = time.time()
        with patch.object(api.auth, 'time', SimpleNamespace(time=lambda: fixed)), ThreadPoolExecutor(16) as workers:
            results = list(workers.map(claim, range(16)))
        accepted = [value for kind, value in results if kind == 'ok']
        rejected = [value for kind, value in results if kind == 'error']
        self.assertEqual((len(accepted), len(rejected)), (10, 6))
        self.assertTrue(all(error.status_code == 429 and 1 <= int(error.headers['Retry-After']) <= 300
                            for error in rejected))
        self.assertEqual(len(api.db.query('SELECT * FROM password_attempts')), 10)
        # The same account on another source is still admitted.
        other = api.auth._begin_login(email, '198.51.100.11')
        self.assertTrue(other.startswith('login_pending_v2:'))

    def test_102_success_clears_only_own_bucket_and_repeated_success_never_locks(self):
        self.assertEqual(self.login('Synthetic wrong password').status_code, 401)
        row = api.db.query('SELECT * FROM password_attempts', one=True)
        ip = row['ip']
        self.assertEqual(row['action'], 'login_failed_v2')
        for email, source in [(self.user['email'], '198.51.100.12'), ('other@example.invalid', ip)]:
            token = api.auth._begin_login(email, source)
            api.auth._finish_login(email, source, token, outcome='failed')
        for _ in range(13):
            response = self.login()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['user']['id'], self.user['id'])
        rows = api.db.query('SELECT email,ip,action FROM password_attempts')
        self.assertEqual({(r['email'], r['ip'], r['action']) for r in rows}, {
            (self.user['email'], '198.51.100.12', 'login_failed_v2'),
            ('other@example.invalid', ip, 'login_failed_v2')})

    def test_102_failed_API_logins_keep_retry_after_and_unavailable_does_not_consume(self):
        for _ in range(10):
            response = self.login('Synthetic wrong password')
            self.assertEqual(response.status_code, 401, response.text)
        blocked = self.login()
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertTrue(1 <= int(blocked.headers['Retry-After']) <= 600)
        self.assertEqual(len(api.db.query("SELECT * FROM password_attempts WHERE action='login_failed_v2'")), 10)

        def unavailable():
            raise HTTPException(503, 'Synthetic unavailable')

        for _ in range(12):
            with self.assertRaises(HTTPException) as raised:
                api.auth._login_attempt(self.user['email'], '198.51.100.13', unavailable)
            self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(api.db.query('SELECT * FROM password_attempts WHERE ip=?', ('198.51.100.13',)), [])

    def test_identity_two_initial_misses_provision_one_deterministic_principal(self):
        gate = threading.Barrier(2)
        uid = self.user['id']

        class OverlappingRead:
            def __init__(self, connection):
                self.connection = connection
                self.first = True

            def execute(self, sql, params=()):
                cursor = self.connection.execute(sql, params)
                if self.first and sql.strip().startswith('SELECT * FROM identity_accounts'):
                    self.first = False
                    gate.wait(timeout=10)  # Both empty SELECT results precede either INSERT.
                return cursor

        def provision(_):
            with api.db.connect() as conn:
                return identity._account(OverlappingRead(conn), uid)

        with ThreadPoolExecutor(2) as workers:
            accounts = list(workers.map(provision, range(2)))
        self.assertEqual(accounts[0], accounts[1])
        self.assertEqual(accounts[0]['work_user_id'], str(uuid.uuid5(identity.NAMESPACE, 'platform-work:' + uid)))
        self.assertEqual(len(api.db.query('SELECT * FROM identity_accounts WHERE user_id=?', (uid,))), 1)

    def test_identity_stale_miss_reuses_committed_canonical_role(self):
        read_empty = threading.Event()
        committed = threading.Event()
        uid = self.user['id']

        class StaleRead:
            def __init__(self, connection):
                self.connection = connection
                self.first = True

            def execute(self, sql, params=()):
                cursor = self.connection.execute(sql, params)
                if self.first and sql.strip().startswith('SELECT * FROM identity_accounts'):
                    self.first = False
                    read_empty.set()
                    if not committed.wait(timeout=10):
                        raise AssertionError('canonical writer did not commit')
                return cursor

        def stale():
            with api.db.connect() as conn:
                return identity._account(StaleRead(conn), uid)

        with ThreadPoolExecutor(1) as workers:
            result = workers.submit(stale)
            try:
                self.assertTrue(read_empty.wait(timeout=10))
                with api.db.connect() as conn:
                    canonical = identity._account(conn, uid)
                    conn.execute("UPDATE identity_accounts SET work_role='admin' WHERE user_id=?", (uid,))
            finally:
                committed.set()
            account = result.result(timeout=10)
        self.assertEqual(account['work_user_id'], canonical['work_user_id'])
        self.assertEqual(account['work_role'], 'admin')
        self.assertEqual(len(api.db.query('SELECT * FROM identity_accounts WHERE user_id=?', (uid,))), 1)

    def test_preferences_concurrent_merge_legacy_put_and_account_guard(self):
        headers = self.user['headers']
        self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 200)
        gate = threading.Barrier(2)

        def update(body):
            gate.wait(timeout=10)
            return self.client.patch('/api/me/preferences', headers=headers, json=body)

        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(update, [{'ui_language': 'en'}, {'theme': 'dark'}]))
        self.assertTrue(all(response.status_code == 200 for response in results), [r.text for r in results])
        expected = {'ui_language': 'en', 'theme': 'dark', 'font_size': 'standard', 'send_key': 'enter'}
        self.assertEqual(self.client.get('/api/me/preferences', headers=headers).json(), expected)
        legacy = self.client.put('/api/me/preferences', headers=headers, json={'font_size': 'larger'})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        expected['font_size'] = 'larger'
        self.assertEqual(legacy.json(), expected)
        changed = self.client.patch('/api/me/preferences', headers=headers | {'X-Wuyu-Preferences-User-ID': 'different-user'}, json={'ui_language': 'zh-CN'})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.client.get('/api/me/preferences', headers=headers).json(), expected)
        other = self.new_user()
        self.assertEqual(self.client.get('/api/me/preferences', headers=other['headers']).json()['theme'], 'light')

    def test_export_uses_consistent_readonly_snapshot_and_owner_download(self):
        uid = self.user['id']
        headers = self.user['headers']
        other = self.new_user()
        original_preferences = {'theme': 'light', 'font_size': 'standard', 'send_key': 'enter', 'ui_language': 'zh-CN'}
        self.assertEqual(self.client.put('/api/me/preferences', headers=headers, json=original_preferences).status_code, 200)
        config = data_export.expert_feedback.config
        writer_ran = []

        def update_after_snapshot(connection=None):
            # build_export has already selected users. Commit a concurrent write
            # before it selects preferences; both reads must see the old version.
            def write():
                with api.db.connect() as writer:
                    writer.execute('UPDATE users SET name=? WHERE id=?', ('Changed after snapshot', uid))
                    writer.execute('UPDATE user_preferences SET value=? WHERE user_id=?',
                                   (json.dumps(original_preferences | {'theme': 'dark'}), uid))
            with ThreadPoolExecutor(1) as workers:
                workers.submit(write).result(timeout=10)
            writer_ran.append(True)
            return config(connection)

        with patch.object(data_export.expert_feedback, 'config', side_effect=update_after_snapshot):
            response = self.client.post('/api/me/exports', headers=headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(writer_ran, [True])
        download_url = response.json()['download_url']
        self.assertEqual(self.client.get(download_url, headers=other['headers']).status_code, 404)
        downloaded = self.client.get(download_url, headers=headers)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        exported = downloaded.json()
        self.assertEqual(exported['manifest']['user_count'], 1)
        self.assertEqual(exported['users'][0]['profile']['id'], uid)
        self.assertEqual(exported['users'][0]['profile']['name'], 'Synthetic profile')
        self.assertEqual(exported['users'][0]['preferences'], original_preferences)
        self.assertEqual(api.db.query('SELECT name FROM users WHERE id=?', (uid,), one=True)['name'], 'Changed after snapshot')
        self.assertEqual(json.loads(api.db.query('SELECT value FROM user_preferences WHERE user_id=?', (uid,), one=True)['value'])['theme'], 'dark')
        for forbidden in (other['email'], 'password_hash', 'Synthetic PG passphrase', 'sessions'):
            self.assertNotIn(forbidden, downloaded.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
