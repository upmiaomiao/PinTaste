"""Adapter contracts without network; real PG integration is run separately."""
import copy
import importlib.util
import sqlite3
import types
import unittest
import os
import tempfile
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location('pg_store_contract_fixture', Path(__file__).with_name('pg_store.py'))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)
store_spec = importlib.util.spec_from_file_location('store_contract_fixture', Path(__file__).with_name('store.py'))
store = importlib.util.module_from_spec(store_spec)
store_spec.loader.exec_module(store)


class FakeFailure(Exception):
    sqlstate = '23505'


class FakeCursor:
    def __init__(self, names=(), rows=(), count=0):
        self.description = [types.SimpleNamespace(name=n) for n in names] if names else None
        self.rows = rows
        self.rowcount = count

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeRaw:
    def __init__(self):
        self.calls = []
        self.rollback_count = 0
        self.commit_count = 0
        self.fail_next_insert = False
        self.fail_lock = False

    def execute(self, sql, values=None):
        self.calls.append((sql, values))
        if self.fail_lock and 'pg_advisory_xact_lock' in sql:
            raise FakeFailure('private credential must not escape')
        if sql.startswith('INSERT') and self.fail_next_insert:
            self.fail_next_insert = False
            raise FakeFailure('private SQL values must not escape')
        if sql.startswith('INSERT') and ' RETURNING ' in sql:
            return FakeCursor(['id'], [(42,)], 1)
        if sql.startswith('SELECT'):
            return FakeCursor(['number', 'blob'], [(Decimal(6), memoryview(b'fixture'))], 1)
        return FakeCursor(count=1)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        pass


class Contracts(unittest.TestCase):
    def config(self, values=None):
        clean = {k: v for k, v in os.environ.items() if not k.startswith('WUYU_PLATFORM_DATABASE_')}
        clean.update(values or {})
        return patch.dict(os.environ, clean, clear=True)

    def test_SQLite_default_remains_and_readonly_does_not_create_missing_DB(self):
        with self.config(), tempfile.TemporaryDirectory() as directory, patch.object(store, 'DATA', Path(directory)):
            self.assertFalse(store.is_postgres())
            with self.assertRaises(sqlite3.OperationalError):
                with store.connect(readonly=True, timeout=2):
                    pass
            self.assertFalse((Path(directory) / 'platform.sqlite3').exists())

    def test_SQLite_core_init_upsert_snapshot_and_metadata_helpers(self):
        with self.config(), tempfile.TemporaryDirectory() as directory, patch.object(store, 'DATA', Path(directory)):
            store.init()
            store.save_settings({'display_name': 'fictional-fixture'}, 'fixture')
            store.save_settings({'display_name': 'fixture-revision'}, 'fixture')
            self.assertEqual(store.settings()['display_name'], 'fixture-revision')
            with store.connect(readonly=True) as conn:
                self.assertTrue(store.table_exists(conn, 'users'))
                self.assertFalse(store.table_exists(conn, 'not_present'))
                self.assertIn('email', store.table_columns(conn, 'users'))
                store.begin_snapshot(conn)
                self.assertEqual(conn.execute('SELECT count(*) FROM audit').fetchone()[0], 2)

    def test_expected_PG_missing_or_wrong_DSN_never_falls_back(self):
        for config in [{'WUYU_PLATFORM_DATABASE_EXPECTED_DIALECT': 'postgresql'},
                       {'WUYU_PLATFORM_DATABASE_URL': 'sqlite:///unwanted'},
                       {'WUYU_PLATFORM_DATABASE_URL': 'postgresql://fixture/db', 'WUYU_PLATFORM_DATABASE_EXPECTED_DIALECT': 'sqlite'},
                       {'WUYU_PLATFORM_DATABASE_URL': 'postgresql://fixture/db', 'WUYU_PLATFORM_DATABASE_URL_FILE': '/fixture'}]:
            with self.config(config), self.assertRaises(sqlite3.OperationalError):
                store.database_url()

    def test_PG_missing_vault_fails_before_any_connection_or_key_creation(self):
        with self.config({'WUYU_PLATFORM_DATABASE_URL': 'postgresql://fixture/db'}), tempfile.TemporaryDirectory() as directory, patch.object(store, 'DATA', Path(directory)):
            with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_VAULT_KEY_REQUIRED$'):
                store.init()
            self.assertFalse((Path(directory) / 'vault.key').exists())

    def test_bind_literals_comments_percent_and_boolean(self):
        sql, values = pg.bind_sql("SELECT '?', \"?\", ? -- ? %\n WHERE x LIKE '%x%' AND y=?", (True, 'x'))
        self.assertEqual(values, (1, 'x'))
        self.assertIn("SELECT '?', \"?\", %s", sql)
        self.assertIn("LIKE '%%x%%'", sql)
        self.assertIn('-- ? %%', sql)
        self.assertEqual(pg.bind_sql("SELECT '100%'"), ("SELECT '100%'", None))
        with self.assertRaises(sqlite3.ProgrammingError):
            pg.bind_sql('SELECT ?')

    def test_statement_splitting_keeps_semicolon_literals(self):
        self.assertEqual(pg.split_statements("SELECT ';'; SELECT 'it''s;valid';"), ["SELECT ';'", "SELECT 'it''s;valid'"])
        with self.assertRaises(sqlite3.ProgrammingError):
            pg.split_statements("SELECT 'unfinished")

    def test_row_contract_decimal_and_bytes_are_JSON_compatible(self):
        row = pg.Row(['n', 'ratio', 'blob'], [Decimal(7), Decimal('1.5'), memoryview(b'x')])
        self.assertEqual(tuple(row), (7, 1.5, b'x'))
        self.assertEqual(row[0], row['n'])
        self.assertEqual(dict(row), {'n': 7, 'ratio': 1.5, 'blob': b'x'})
        self.assertEqual(row[-1], b'x')
        self.assertEqual(row[:2], (7, 1.5))

    def test_DDL_types_preserve_SQLite_numeric_width_and_literals(self):
        sql = pg.translate_ddl("CREATE TABLE IF NOT EXISTS fixture(id INTEGER PRIMARY KEY AUTOINCREMENT,n INTEGER,t REAL,b BLOB,label TEXT DEFAULT 'INTEGER REAL BLOB')")[0]
        self.assertIn('id BIGSERIAL PRIMARY KEY', sql)
        self.assertIn('n BIGINT,t DOUBLE PRECISION,b BYTEA', sql)
        self.assertIn("DEFAULT 'INTEGER REAL BLOB'", sql)
        table, index = pg.translate_ddl("CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(terms,tokenize='unicode61')")
        self.assertIn('tokens TEXT[] NOT NULL', table)
        self.assertIn('USING GIN(tokens)', index)
        with self.assertRaises(sqlite3.ProgrammingError):
            pg.translate_ddl('DROP TABLE users')

    def test_first_write_owns_transaction_lock_once_until_commit(self):
        raw = FakeRaw()
        conn = pg.Connection(raw)
        conn.execute('INSERT INTO settings VALUES(?,?)', ('a', 'b'))
        conn.execute('UPDATE settings SET value=? WHERE key=?', ('c', 'a'))
        self.assertEqual(sum('pg_advisory_xact_lock' in s for s, _ in raw.calls), 1)
        self.assertIn('READ COMMITTED', raw.calls[0][0])
        self.assertEqual(raw.calls[2][1], pg.WRITE_LOCK)
        self.assertTrue(conn.in_transaction)
        conn.commit()
        self.assertFalse(conn.in_transaction)
        conn.execute('DELETE FROM settings WHERE key=?', ('a',))
        self.assertEqual(sum('pg_advisory_xact_lock' in s for s, _ in raw.calls), 2)

    def test_pure_reads_do_not_hold_a_writer_transaction(self):
        raw = FakeRaw()
        conn = pg.Connection(raw)
        row = conn.execute('SELECT number,blob FROM fixture').fetchone()
        self.assertEqual(dict(row), {'number': 6, 'blob': b'fixture'})
        self.assertFalse(conn.in_transaction)
        self.assertFalse(any('BEGIN' in s for s, _ in raw.calls))

    def test_snapshot_and_readonly_reject_writes(self):
        for readonly in [True, False]:
            raw = FakeRaw()
            conn = pg.Connection(raw, readonly=readonly)
            if not readonly:
                conn.begin_snapshot()
                self.assertIn('REPEATABLE READ READ ONLY', raw.calls[0][0])
            before = copy.deepcopy(raw.calls)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('UPDATE settings SET value=?', ('x',))
            self.assertEqual(raw.calls, before)

    def test_statement_integrity_error_rolls_back_savepoint_without_erasing_transaction(self):
        raw = FakeRaw()
        conn = pg.Connection(raw)
        conn.execute('BEGIN IMMEDIATE')
        raw.fail_next_insert = True
        with self.assertRaisesRegex(sqlite3.IntegrityError, '^PLATFORM_DATABASE_INTEGRITY_ERROR$'):
            conn.execute('INSERT INTO settings VALUES(?,?)', ('a', 'b'))
        self.assertTrue(conn.in_transaction)
        self.assertTrue(any(s.startswith('ROLLBACK TO SAVEPOINT') for s, _ in raw.calls))
        conn.execute('UPDATE settings SET value=? WHERE key=?', ('c', 'a'))
        self.assertEqual(raw.rollback_count, 0)

    def test_failed_lock_is_not_retried_or_left_open(self):
        raw = FakeRaw()
        raw.fail_lock = True
        conn = pg.Connection(raw)
        with self.assertRaises(sqlite3.Error):
            conn.execute('BEGIN IMMEDIATE')
        self.assertFalse(conn.in_transaction)
        self.assertEqual(raw.rollback_count, 1)
        self.assertEqual(sum('pg_advisory_xact_lock' in s for s, _ in raw.calls), 1)

    def test_serial_lastrowid_and_explicit_returning_contract(self):
        raw = FakeRaw()
        conn = pg.Connection(raw)
        result = conn.execute('INSERT INTO memory_chunks(text) VALUES(?)', ('fixture',))
        self.assertEqual(result.lastrowid, 42)
        self.assertIsNone(result.fetchone())
        explicit = conn.execute('INSERT INTO memory_chunks(text) VALUES(?) RETURNING id', ('fixture',))
        self.assertEqual(explicit.fetchone()[0], 42)
        self.assertEqual(sum(s.count(' RETURNING ') for s, _ in raw.calls if s.startswith('INSERT')), 2)

    def test_broken_rollback_keeps_original_sanitized_error_and_clears_transaction(self):
        class TransportFailure(Exception):
            sqlstate = '08006'

        class BrokenRaw(FakeRaw):
            def __init__(self, phase):
                super().__init__()
                self.phase = phase

            def execute(self, sql, values=None):
                if (self.phase == 'begin' and sql.startswith('BEGIN')) or sql.startswith(('INSERT', 'ROLLBACK TO SAVEPOINT')):
                    self.calls.append((sql, values))
                    raise TransportFailure('synthetic private transport detail')
                return super().execute(sql, values)

            def rollback(self):
                self.rollback_count += 1
                raise RuntimeError('synthetic raw cleanup detail')

        for phase in ('begin', 'statement'):
            with self.subTest(phase=phase):
                raw = BrokenRaw(phase)
                conn = pg.Connection(raw)
                with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_DATABASE_UNAVAILABLE$') as caught:
                    conn.execute('INSERT INTO settings VALUES(?,?)', ('a', 'b'))
                self.assertEqual(caught.exception.sqlstate, '08006')
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertFalse(conn.in_transaction)
                self.assertFalse(conn._transaction_readonly)
                self.assertEqual(raw.rollback_count, 1)

    def test_connect_cleanup_keeps_original_error_and_user_interrupt(self):
        class BrokenRollback(FakeRaw):
            closed = False

            def rollback(self):
                raise RuntimeError('synthetic raw cleanup detail')

            def close(self):
                self.closed = True

        for original in (ValueError('original operation error'), KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(original=type(original).__name__):
                raw = BrokenRollback()
                with self.config(), patch.object(store.sqlite3, 'connect', return_value=raw):
                    with self.assertRaises(type(original)) as caught:
                        with store.connect():
                            raise original
                self.assertIs(caught.exception, original)
                self.assertTrue(raw.closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
