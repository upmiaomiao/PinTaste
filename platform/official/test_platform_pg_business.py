"""Portable business-SQL/readonly contracts with synthetic SQLite fixtures.

No application bootstrap, external service, credential, live file or model call.
The PG adapter and real PG concurrency are exercised by the integration suite.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
import uuid

HERE = Path(__file__).parent


def functions(filename, names, namespace):
    tree = ast.parse((HERE / filename).read_text(encoding='utf-8'))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    exec(compile(ast.Module(selected, type_ignores=[]), filename, 'exec'), namespace)
    return namespace


class HTTPException(Exception):
    def __init__(self, status_code, detail, headers=None):
        super().__init__(detail)
        self.status_code, self.detail, self.headers = status_code, detail, headers


class FixtureStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'fixture.sqlite3'

    @contextmanager
    def connect(self, *, readonly=False, timeout=15):
        uri = self.path.as_uri() + '?mode=ro' if readonly else str(self.path)
        conn = sqlite3.connect(uri, uri=readonly, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class PortableUpsertTests(unittest.TestCase):
    CASES = [
        ('auth.py', 'user_passwords', 'user_id TEXT PRIMARY KEY,password_hash TEXT NOT NULL,updated REAL NOT NULL',
         'INSERT OR REPLACE INTO user_passwords VALUES(?,?,?)', ('u', 'synthetic-hash', 1.5), ('u', 'new-synthetic-hash', 2.5)),
        ('app_main.py', 'memory_preferences', 'conversation_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,mode TEXT NOT NULL,auto_compress INTEGER NOT NULL',
         'INSERT OR REPLACE INTO memory_preferences(conversation_id,user_id,mode,auto_compress) VALUES(?,?,?,?)',
         ('c', 'u', 'auto', True), ('c', 'u', 'off', False)),
        ('user_settings.py', 'user_preferences', 'user_id TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL',
         'INSERT OR REPLACE INTO user_preferences VALUES(?,?,?)', ('u', '{"ui_language":"en"}', 1.0), ('u', '{"theme":"dark","ui_language":"en"}', 2.0)),
        ('expert_feedback.py', 'expert_reviewers', 'user_id TEXT PRIMARY KEY,granted_by TEXT NOT NULL,created REAL NOT NULL',
         'INSERT OR REPLACE INTO expert_reviewers VALUES(?,?,?)', ('u', 'a', 1.0), ('u', 'b', 2.0)),
        ('expert_feedback.py', 'settings', 'key TEXT PRIMARY KEY,value TEXT NOT NULL',
         'INSERT OR REPLACE INTO settings VALUES(?,?)', ('expert_feedback_config', '{"rule_version":1}'), ('expert_feedback_config', '{"rule_version":2}')),
        ('tool_registry.py', 'settings', 'key TEXT PRIMARY KEY,value TEXT NOT NULL',
         'INSERT OR REPLACE INTO settings VALUES(?,?)', ('tool_catalog', '{"revision":1}'), ('tool_catalog', '{"revision":2}')),
        ('voice.py', 'settings', 'key TEXT PRIMARY KEY,value TEXT NOT NULL',
         'INSERT OR REPLACE INTO settings VALUES(?,?)', ('voice_config', '{"enabled":false}'), ('voice_config', '{"enabled":false,"max_seconds":120}')),
    ]

    def replacement(self, filename, table):
        tree = ast.parse((HERE / filename).read_text(encoding='utf-8'))
        candidates = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and n.value.startswith('INSERT INTO ' + table + '(') and 'ON CONFLICT' in n.value}
        self.assertEqual(len(candidates), 1)
        return candidates.pop()

    def test_insert_and_existing_row_update_match_original_sqlite_contract(self):
        for filename, table, schema, old, first, second in self.CASES:
            with self.subTest(module=filename, table=table):
                new = self.replacement(filename, table)
                states = []
                for sql in (old, new):
                    with sqlite3.connect(':memory:') as conn:
                        conn.execute('CREATE TABLE ' + table + '(' + schema + ')')
                        conn.execute(sql, first)
                        conn.execute(sql, second)
                        conn.commit()
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 1)
                        states.append(conn.execute('SELECT * FROM ' + table).fetchall())
                        conn.execute('BEGIN IMMEDIATE')
                        conn.execute(sql, first)
                        conn.rollback()
                        self.assertEqual(conn.execute('SELECT * FROM ' + table).fetchall(), states[-1])
                self.assertEqual(states[0], states[1])


class AuthLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = FixtureStore(self.temp.name)
        with self.db.connect() as conn:
            conn.execute('CREATE TABLE password_attempts(email TEXT,ip TEXT,action TEXT,created REAL)')
        self.ns = functions('auth.py', ['_begin_login', '_finish_login'],
                            {'db': self.db, 'time': types.SimpleNamespace(time=lambda: 1000.0),
                             'uuid': uuid, 'math': math, 'HTTPException': HTTPException})

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_prefix_matches_legacy_glob_with_case_and_window_boundaries(self):
        actions = ['login_pending_v2:', 'login_pending_v2:uuid', 'login_pending_v2:a:b',
                   'LOGIN_pending_v2:uuid', 'login_pending_v2', 'login_pending_v20:uuid',
                   'xlogin_pending_v2:uuid', 'login_pending_v2:*', 'login_failed_v2', 'register', 'login']
        with self.db.connect() as conn:
            for number, action in enumerate(actions):
                for created in (700.0, 700.001, 1000.0):
                    conn.execute('INSERT INTO password_attempts VALUES(?,?,?,?)', (str(number), 'ip', action, created))
            old = conn.execute("SELECT email,created,action FROM password_attempts WHERE ip=? AND ((action='login_failed_v2' AND created>?) OR (action GLOB 'login_pending_v2:*' AND created>?)) ORDER BY email,created", ('ip', 400, 700)).fetchall()
            new = conn.execute("SELECT email,created,action FROM password_attempts WHERE ip=? AND ((action='login_failed_v2' AND created>?) OR (substr(action,1,17)='login_pending_v2:' AND created>?)) ORDER BY email,created", ('ip', 400, 700)).fetchall()
            self.assertEqual([tuple(r) for r in old], [tuple(r) for r in new])

    def test_actual_atomic_claim_counts_pending_and_returns_retry_after(self):
        def claim(_):
            try:
                return ('accepted', self.ns['_begin_login']('u', 'ip'))
            except HTTPException as error:
                return (error.status_code, error.headers['Retry-After'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(sum(r[0] == 'accepted' for r in results), 10)
        self.assertEqual(sum(r == (429, '300') for r in results), 6)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM password_attempts').fetchone()[0], 10)

    def test_success_clears_only_same_account_source_failures_and_its_lease(self):
        token = self.ns['_begin_login']('u', 'ip')
        with self.db.connect() as conn:
            for row in [('u', 'ip', 'login_failed_v2', 999), ('other', 'ip', 'login_failed_v2', 999),
                        ('u', 'another-ip', 'login_failed_v2', 999), ('u', 'ip', 'register', 999)]:
                conn.execute('INSERT INTO password_attempts VALUES(?,?,?,?)', row)
        self.ns['_finish_login']('u', 'ip', token, outcome='success')
        with self.db.connect() as conn:
            remaining = [tuple(r)[:3] for r in conn.execute('SELECT * FROM password_attempts')]
        self.assertCountEqual(remaining, [('other', 'ip', 'login_failed_v2'), ('u', 'another-ip', 'login_failed_v2'), ('u', 'ip', 'register')])


class ReadonlyHealthTests(unittest.TestCase):
    def namespace(self, db):
        runtime = types.SimpleNamespace(chat_execution_mode='combined', task_queue_warn_seconds=300)
        return functions('task_health.py', ['snapshot'], {'db': db, 'runtime': runtime,
            'sqlite3': sqlite3, 'math': math, 'time': types.SimpleNamespace(time=lambda: 1000.0)})

    def test_missing_sqlite_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            db = FixtureStore(directory)
            result = self.namespace(db)['snapshot']()
            self.assertEqual(result['alerts'], ['queue_unavailable'])
            self.assertFalse(db.path.exists())
            self.assertNotIn(directory, json.dumps(result))

    def test_selected_backend_readonly_connection_is_used_without_accessing_data_path(self):
        calls = []
        class SelectedBackend:
            @property
            def DATA(self):
                raise AssertionError('Diagnostics must not fall back to a SQLite file')
            @contextmanager
            def connect(self, **kwargs):
                calls.append(kwargs)
                yield types.SimpleNamespace(execute=lambda sql: types.SimpleNamespace(fetchone=lambda: (2, 1, 500.0)))
        result = self.namespace(SelectedBackend())['snapshot']()
        self.assertEqual(calls, [{'readonly': True, 'timeout': 2}])
        self.assertEqual(result['queue'], {'queued': 2, 'running': 1, 'oldest_queued_age_seconds': 500.0})
        self.assertEqual(result['alerts'], ['queue_wait_high'])

    def test_sqlite_health_leaves_file_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            db = FixtureStore(directory)
            with db.connect() as conn:
                conn.execute('CREATE TABLE jobs(status TEXT,created REAL)')
                conn.execute("INSERT INTO jobs VALUES('queued',900)")
            before = db.path.read_bytes()
            result = self.namespace(db)['snapshot']()
            self.assertEqual(result['queue']['queued'], 1)
            self.assertEqual(before, db.path.read_bytes())


class IdentityProvisionTests(unittest.TestCase):
    def test_lazy_provision_reuses_canonical_account_after_a_stale_read_miss(self):
        ns = functions('integrated_identity.py', ['_account'], {'uuid': uuid,
            'NAMESPACE': uuid.UUID('b303fe2b-5890-4a48-9e19-f0973db573bd'),
            'time': types.SimpleNamespace(time=lambda: 1000.0),
            'IdentityError': lambda code, status: ValueError(code)})
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE users(id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO users VALUES(?)', ('u',))
            conn.execute('CREATE TABLE identity_accounts(user_id TEXT PRIMARY KEY,account_type TEXT,legacy_alias TEXT UNIQUE,work_user_id TEXT UNIQUE,work_role TEXT,linked_to_user_id TEXT,created REAL)')
            first = ns['_account'](conn, 'u')
            self.assertEqual(first['work_user_id'], str(uuid.uuid5(ns['NAMESPACE'], 'platform-work:u')))
            conn.execute("UPDATE identity_accounts SET work_role='admin' WHERE user_id='u'")
            expected = dict(conn.execute("SELECT * FROM identity_accounts WHERE user_id='u'").fetchone())
            class StaleReadMiss:
                missed = False
                def execute(self, sql, args=()):
                    if sql.strip() == 'SELECT * FROM identity_accounts WHERE user_id=?' and not self.missed:
                        self.missed = True
                        return types.SimpleNamespace(fetchone=lambda: None)
                    return conn.execute(sql, args)
            # Another writer inserted the account after a reader observed no row.
            # This exercises the actual function; existing role/identity survive.
            self.assertEqual(ns['_account'](StaleReadMiss(), 'u'), expected)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM identity_accounts').fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, '^USER_NOT_FOUND$'):
                ns['_account'](conn, 'missing')


class MetadataDispatchTests(unittest.TestCase):
    def test_export_uses_explicit_consistent_snapshot_and_backend_metadata(self):
        source = (HERE / 'data_export.py').read_text(encoding='utf-8')
        self.assertIn('db.begin_snapshot(conn)', source)
        self.assertIn("db.table_exists(conn, 'voice_calls')", source)
        self.assertNotIn('sqlite_master', source)
        self.assertLess(source.index('db.begin_snapshot(conn)'), source.index("SELECT id,email,name"))
        voice = (HERE / 'voice.py').read_text(encoding='utf-8')
        self.assertIn("db.table_columns(conn, 'voice_calls')", voice)
        self.assertNotIn('PRAGMA table_info', voice)

    def test_host_monitor_probes_container_health_without_its_own_sqlite_reader(self):
        source = (HERE / 'task_monitor.py').read_text(encoding='utf-8')
        self.assertIn("'official.task_health'", source)
        self.assertNotIn('sqlite3.connect', source)
        self.assertNotIn('platform.sqlite3', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
