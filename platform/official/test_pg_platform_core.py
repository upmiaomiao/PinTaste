"""Real disposable PostgreSQL integration; no application databases or models."""
import os
import json
import subprocess
import sys
import threading
import time
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from . import store as db
from .pg_test_fixture import PlatformPGFixture


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'explicit disposable PG fixture required')
class PGCoreTests(unittest.TestCase):
    def test_privileged_bootstrap_then_DML_only_runtime_without_SQLite(self):
        with PlatformPGFixture() as fixture, fixture.activate():
            from .runtime_bootstrap import initialize_runtime
            key = (fixture.data / 'vault.key').read_bytes()
            with patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLITE_MUST_NOT_BE_USED')):
                initialize_runtime()
                self.assertEqual((fixture.data / 'vault.key').read_bytes(), key)
                db.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)', ('fixture', 'bootstrap_verified', 1.25))
                self.assertEqual(db.query('SELECT action FROM audit', one=True)['action'], 'bootstrap_verified')
                with db.connect() as conn:
                    self.assertEqual(conn.execute('SELECT current_user').fetchone()[0], fixture.role)
                    self.assertFalse(conn.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0])
                    self.assertFalse(conn.execute('SELECT has_database_privilege(current_user,current_database(),?)', ('TEMP',)).fetchone()[0])
                    with self.assertRaises(sqlite3.OperationalError):
                        conn.execute('CREATE TABLE IF NOT EXISTS app_must_not_create(x TEXT)')
                with self.psycopg_connection(fixture) as raw:
                    with self.assertRaises(fixture.psycopg.errors.InsufficientPrivilege):
                        raw.execute('CREATE TABLE app_must_not_create(x TEXT)')
                    with self.assertRaises(fixture.psycopg.errors.InsufficientPrivilege):
                        raw.execute('CREATE TEMP TABLE app_must_not_create(x TEXT)')
                    with self.assertRaises(fixture.psycopg.errors.InsufficientPrivilege):
                        raw.execute("UPDATE wuyu_platform_schema_meta SET value='unsafe'")

    def test_actual_row_LASTROWID_UPSERT_and_statement_failure_recovery(self):
        with PlatformPGFixture() as fixture, fixture.activate(), patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLITE_MUST_NOT_BE_USED')):
            db.save_settings({'display_name': 'first-fixture'}, 'fixture')
            db.save_settings({'display_name': 'second-fixture'}, 'fixture')
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                first = conn.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)', ('fixture', 'lastrowid', 2.5))
                self.assertIsInstance(first.lastrowid, int)
                row = conn.execute('SELECT id,action,created FROM audit WHERE id=?', (first.lastrowid,)).fetchone()
                self.assertEqual(dict(row), {'id': first.lastrowid, 'action': 'lastrowid', 'created': 2.5})
                self.assertEqual(tuple(row), (first.lastrowid, 'lastrowid', 2.5))
                with self.assertRaisesRegex(sqlite3.IntegrityError, '^PLATFORM_DATABASE_INTEGRITY_ERROR$'):
                    conn.execute('INSERT INTO settings VALUES(?,?)', ('display_name', 'duplicate'))
                self.assertTrue(conn.in_transaction)
                conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps('after-savepoint'), 'display_name'))
            self.assertEqual(db.settings()['display_name'], 'after-savepoint')
            self.assertEqual(db.query('SELECT count(*) AS n FROM audit', one=True)['n'], 3)

    def test_102_pending_capacity_is_atomic_under_16_connections(self):
        from . import auth
        from fastapi import HTTPException
        with PlatformPGFixture() as fixture, fixture.activate(), patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLITE_MUST_NOT_BE_USED')):
            start = threading.Barrier(16)
            def claim(_):
                start.wait(timeout=10)
                try:
                    return ('claimed', auth._begin_login('fixture@example.invalid', '192.0.2.111'))
                except HTTPException as error:
                    self.assertEqual(error.status_code, 429)
                    self.assertGreaterEqual(int(error.headers['Retry-After']), 1)
                    return ('limited', None)
            with ThreadPoolExecutor(max_workers=16) as pool:
                rows = list(pool.map(claim, range(16)))
            claimed = [value for kind, value in rows if kind == 'claimed']
            self.assertEqual(len(claimed), 10)
            self.assertEqual(sum(kind == 'limited' for kind, _ in rows), 6)
            self.assertEqual(db.query('SELECT count(*) AS n FROM password_attempts', one=True)['n'], 10)
            auth._finish_login('fixture@example.invalid', '192.0.2.111', claimed[0], outcome='success')
            auth._begin_login('fixture@example.invalid', '192.0.2.111')
            auth._begin_login('fixture@example.invalid', '192.0.2.112')
            self.assertEqual(db.query('SELECT count(*) AS n FROM password_attempts', one=True)['n'], 11)

    def test_two_processes_share_the_original_single_writer_domain(self):
        child = """from official import store as db
import time,json
assert db.is_postgres()
assert db.query('SELECT current_database() AS name',one=True)['name'].startswith('qa111_core_')
for i in range(8):
 with db.connect() as c:
  c.execute('BEGIN IMMEDIATE')
  value=int(c.execute("SELECT value FROM settings WHERE key='fixture_counter'").fetchone()[0])
  time.sleep(.015)
  c.execute("UPDATE settings SET value=? WHERE key='fixture_counter'",(str(value+1),))
  c.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)',('fixture','counter',float(i)))
print(json.dumps({'committed':8}))
"""
        with PlatformPGFixture() as fixture, fixture.activate():
            db.execute('INSERT INTO settings VALUES(?,?)', ('fixture_counter', '0'))
            child_environment = dict(os.environ)
            child_environment['PYTHONPATH'] = os.pathsep.join([str(db.ROOT.parent), *sys.path])
            processes = [subprocess.Popen([sys.executable, '-c', child], env=child_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, 'fixture child failed')
                    self.assertEqual(json.loads(stdout), {'committed': 8})
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
            self.assertEqual(db.query("SELECT value FROM settings WHERE key='fixture_counter'", one=True)['value'], '16')
            self.assertEqual(db.query("SELECT count(*) AS n FROM audit WHERE action='counter'", one=True)['n'], 16)

    def test_actual_wait_timeout_rolls_back_and_releases_lock(self):
        with PlatformPGFixture() as fixture, fixture.activate():
            with db.connect() as owner:
                owner.execute('BEGIN IMMEDIATE')
                entered = threading.Event()
                def blocked():
                    with patch.dict(os.environ, {'WUYU_PLATFORM_PG_LOCK_TIMEOUT_MS': '250'}):
                        try:
                            with db.connect() as contender:
                                entered.set()
                                contender.execute('BEGIN IMMEDIATE')
                        except sqlite3.OperationalError as error:
                            return getattr(error, 'sqlstate', None)
                    return 'unexpected-success'
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(blocked)
                    self.assertTrue(entered.wait(timeout=5))
                    waiting = False
                    with fixture.psycopg.connect(fixture.admin_url, autocommit=True) as raw:
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline and not future.done():
                            waiting = raw.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))").fetchone()[0]
                            if waiting:
                                break
                            time.sleep(.01)
                    self.assertTrue(waiting, 'test must observe actual pg_locks wait')
                    self.assertEqual(future.result(timeout=5), '55P03')
            db.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)', ('fixture', 'after_lock_timeout', 1.0))
            self.assertEqual(db.query('SELECT count(*) AS n FROM audit', one=True)['n'], 1)

    def test_readonly_connection_and_health_do_not_initialize_or_write(self):
        with PlatformPGFixture() as fixture, fixture.activate(), patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLITE_MUST_NOT_BE_USED')):
            from . import task_health
            key = (fixture.data / 'vault.key').read_bytes()
            with db.connect(readonly=True, timeout=2) as conn:
                self.assertTrue(conn.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on')
                self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)', ('fixture', 'forbidden', 1.0))
            result = task_health.snapshot()
            self.assertIsNotNone(result['queue'])
            self.assertEqual(result['queue']['queued'], 0)
            self.assertEqual(result['queue']['running'], 0)
            self.assertNotIn('queue_unavailable', result['alerts'])
            self.assertEqual((fixture.data / 'vault.key').read_bytes(), key)
            self.assertEqual(db.query('SELECT count(*) AS n FROM audit', one=True)['n'], 0)

    def test_schema_version_signature_and_missing_schema_fail_closed(self):
        with PlatformPGFixture() as fixture, fixture.activate(), patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLITE_MUST_NOT_BE_USED')):
            key = (fixture.data / 'vault.key').read_bytes()
            with fixture.psycopg.connect(fixture.admin_url, autocommit=True) as raw:
                raw.execute("UPDATE wuyu_platform_schema_meta SET value='999' WHERE key='schema_version'")
                with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_SCHEMA_VERSION_MISMATCH$'):
                    db.init()
                raw.execute("UPDATE wuyu_platform_schema_meta SET value='1' WHERE key='schema_version'")
                constraint = raw.execute("SELECT conname FROM pg_constraint WHERE conrelid='users'::regclass AND contype='c'").fetchone()[0]
                from psycopg import sql
                raw.execute(sql.SQL('ALTER TABLE users DROP CONSTRAINT {}').format(sql.Identifier(constraint)))
                with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_SCHEMA_SIGNATURE_MISMATCH$'):
                    db.init()
                raw.execute('DROP TABLE wuyu_platform_schema_meta')
                with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_SCHEMA_MISSING$'):
                    db.init()
                self.assertFalse(raw.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='wuyu_platform_schema_meta')").fetchone()[0])
            self.assertEqual((fixture.data / 'vault.key').read_bytes(), key)

    def psycopg_connection(self, fixture):
        return fixture.psycopg.connect(fixture.app_url, autocommit=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
