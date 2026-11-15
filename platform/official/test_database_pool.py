"""Official pool + actual adapter against guarded disposable PG only."""
import concurrent.futures
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from . import pg_pool
from . import pg_store
from . import quota_limits
from . import store as db
from .pg_test_fixture import PlatformPGFixture


class LogicalLeaseTests(unittest.TestCase):
    def test_returned_wrapper_never_operates_on_another_borrower(self):
        class Raw:
            calls = []
            def execute(self, *args): self.calls.append(args); raise AssertionError('must not reach released raw')
        raw = Raw(); returned = []
        connection = pg_store.Connection(raw, release=lambda: returned.append(True))
        connection.close(); connection.close()
        self.assertEqual(returned, [True])
        for operation in (lambda: connection.execute('SELECT 1'), connection.commit, connection.rollback):
            with self.assertRaisesRegex(sqlite3.ProgrammingError, '^PLATFORM_DATABASE_CONNECTION_CLOSED$'): operation()
        self.assertEqual(raw.calls, [])

    def test_pool_logger_never_keeps_driver_body_dsn_or_traceback(self):
        record = logging.LogRecord('psycopg.pool', logging.WARNING, __file__, 1,
                                  'connection DSN %s', ('synthetic-private-fixture',), (ValueError, ValueError('private'), None))
        record.stack_info = 'synthetic-private-stack'
        self.assertTrue(pg_pool._SafePoolLog().filter(record))
        self.assertEqual(record.getMessage(), 'PostgreSQL pool maintenance event')
        self.assertIsNone(record.exc_info); self.assertIsNone(record.stack_info)

    def test_quota_exemption_reuses_admission_connection(self):
        # This executes actual quota SQL/helper logic with an actual disposable
        # SQLite connection; a second borrow is forbidden, not silently faked.
        raw = sqlite3.connect(':memory:'); raw.row_factory = sqlite3.Row
        raw.executescript('CREATE TABLE users(id TEXT PRIMARY KEY,role TEXT); CREATE TABLE settings(key TEXT,value TEXT);')
        raw.execute("INSERT INTO users VALUES('actor-A','user')")
        try:
            with patch.object(db, 'query', side_effect=AssertionError('nested connection forbidden')):
                self.assertFalse(quota_limits.daily_exceeded('actor-A',
                    {'daily_tokens':1000,'daily_questions':10,'job_token_budget':100}, 0, 0, conn=raw))
        finally: raw.close()


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'requires guarded disposable PG fixture')
class OfficialPoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PlatformPGFixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.close)
        self.activation = self.fixture.activate()
        self.activation.__enter__()
        self.addCleanup(self.activation.__exit__, None, None, None)

    def test_physical_connection_reuse_and_session_reset(self):
        pids = []
        for _ in range(12):
            with db.connect(readonly=True, timeout=1) as connection:
                pids.append(connection.raw.execute('SELECT pg_backend_pid()').fetchone()[0])
                self.assertEqual(connection.raw.execute('SHOW default_transaction_read_only').fetchone()[0], 'on')
                connection.raw.execute("SET search_path='pg_catalog'")
            time.sleep(.01)
        with db.connect() as connection:
            self.assertEqual(connection.raw.execute('SHOW search_path').fetchone()[0], 'public')
            self.assertEqual(connection.raw.execute('SHOW default_transaction_read_only').fetchone()[0], 'off')
            self.assertEqual(connection.raw.execute('SHOW statement_timeout').fetchone()[0], '0')
        statistics = pg_pool.stats()['statistics']
        self.assertLessEqual(len(set(pids)), pg_pool.MAX_SIZE)
        self.assertLessEqual(statistics['connections_num'], pg_pool.MAX_SIZE)
        self.assertGreater(statistics['requests_num'], statistics['connections_num'])

    def test_rollback_and_closed_lease_do_not_bleed_into_next_account(self):
        with self.assertRaisesRegex(ValueError, '^synthetic rollback$'):
            with db.connect() as connection:
                connection.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                    ('rollback-A','rollback-A@example.invalid','Fixture','Test','其他','user',time.time()))
                raise ValueError('synthetic rollback')
        self.assertIsNone(db.query('SELECT id FROM users WHERE id=?', ('rollback-A',), one=True))
        with self.assertRaisesRegex(sqlite3.ProgrammingError, '^PLATFORM_DATABASE_CONNECTION_CLOSED$'):
            connection.execute('SELECT 1')
        with db.connect() as next_connection:
            next_connection.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                ('success-B','success-B@example.invalid','Fixture','Test','其他','user',time.time()))
        self.assertEqual(db.query('SELECT id FROM users WHERE id=?', ('success-B',), one=True)['id'], 'success-B')
        self.assertIsNone(db.query('SELECT id FROM users WHERE id=?', ('rollback-A',), one=True))

    def test_broken_connection_is_discarded_and_error_is_sanitized(self):
        with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_DATABASE_UNAVAILABLE$'):
            with db.connect() as connection:
                old_pid = connection.raw.execute('SELECT pg_backend_pid()').fetchone()[0]
                connection.raw.close()
        with db.connect() as connection:
            new_pid = connection.raw.execute('SELECT pg_backend_pid()').fetchone()[0]
            self.assertNotEqual(old_pid, new_pid)
        self.assertEqual(db.query('SELECT 7 AS n', one=True)['n'], 7)

    def test_capacity_and_wait_timeout_are_bounded(self):
        stack = contextlib.ExitStack()
        try:
            held = [stack.enter_context(db.connect()) for _ in range(pg_pool.MAX_SIZE)]
            pids = [c.raw.execute('SELECT pg_backend_pid()').fetchone()[0] for c in held]
            self.assertEqual(len(set(pids)), pg_pool.MAX_SIZE)
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_DATABASE_UNAVAILABLE$'):
                with db.connect(timeout=.1): self.fail('fifth physical connection must not be created')
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual(pg_pool.stats()['statistics']['pool_size'], pg_pool.MAX_SIZE)
        finally: stack.close()
        self.assertEqual(db.query('SELECT 3 AS n', one=True)['n'], 3)

    def test_same_lock_blocks_writer_but_plain_reader_remains_available(self):
        entered = threading.Event()
        def waiting_writer():
            with db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE'); entered.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with db.connect() as owner:
                owner.execute('BEGIN IMMEDIATE')
                waiting = executor.submit(waiting_writer)
                time.sleep(.05)
                self.assertFalse(entered.is_set())
                self.assertEqual(db.query('SELECT 11 AS n', one=True)['n'], 11)
            waiting.result(timeout=3)
        self.assertTrue(entered.is_set())

    def test_changed_dsn_fails_closed_instead_of_growing_second_pool(self):
        self.assertEqual(db.query('SELECT 1 AS n', one=True)['n'], 1)
        with self.assertRaisesRegex(RuntimeError, '^PLATFORM_DATABASE_POOL_RESTART_REQUIRED$'):
            pg_pool._get_pool('postgresql://different.invalid/fixture')
        self.assertEqual(db.query('SELECT 2 AS n', one=True)['n'], 2)


if __name__ == '__main__': unittest.main()
