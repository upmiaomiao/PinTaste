"""Self-made 27-table snapshots and isolated PostgreSQL migration fixtures.

No application files/config/credentials/SQL dumps are read. PG requires the
synthetic WUYU_PG_TEST_DSN database prefix; only UUID-owned schemas are removed.
"""
import ast
import builtins
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import symtable
import tempfile
import time
import types
import unittest
import uuid
from unittest.mock import patch

from . import platform_pg_migrate as migration
from . import pg_store


def schema_statements():
    """Use current app CREATE declarations without importing app side effects."""
    folder = Path(__file__).parent
    for module in ('store', 'integrated_identity', 'voice', 'expert_feedback', 'document_rag'):
        target = folder / (module + '.py')
        if not target.exists():
            target = folder.parents[2] / 'current-source/platform/official' / (module + '.py')
        tree = ast.parse(target.read_text(encoding='utf-8'))
        init = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'init')
        for node in ast.walk(init):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                    node.func.attr in ('execute', 'executescript') and node.args and
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                sql = node.args[0].value.strip()
                if sql.upper().startswith('CREATE '):
                    yield from pg_store.split_statements(sql)
        if module == 'voice':
            for node in ast.walk(init):
                if isinstance(node, ast.Dict):
                    try:
                        declarations = ast.literal_eval(node)
                    except (ValueError, TypeError):
                        continue
                    if 'purpose' in declarations:
                        for name, declaration in declarations.items():
                            yield 'ALTER TABLE voice_calls ADD COLUMN ' + name + ' ' + declaration


def make_snapshot(path):
    with contextlib.closing(sqlite3.connect(path)) as source, source:
        for sql in schema_statements():
            source.execute(sql)
        source.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                       ('fixture-user', 'fixture@example.invalid', '中文 fixture', '', 'teacher', 'admin', 1, 1.125))
        source.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?)', ('chat', 'fixture-user', '混合 Alpha', 1.25, 2.375, 0))
        source.execute('INSERT INTO files VALUES(?,?,?,?)', ('file', 'fixture-user', 2**53 + 7, 1.5))
        source.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('job', 'fixture-user', 'chat', 'complete', '{"prompt":"synthetic"}', '{"state":"ok"}',
                        1.25, 2.5, 3.75, 0, 0, 0, '2026-11-08', 7))
        source.execute('INSERT INTO job_events VALUES(?,?,?,?)', ('job', 9, 3.5, '{"event":"synthetic"}'))
        source.execute('INSERT INTO settings VALUES(?,?)', ('provider_fixture_secret', json.dumps('gAAAA_SYNTHETIC_ciphertext_unchanged')))
        source.execute('INSERT INTO user_preferences VALUES(?,?,?)', ('fixture-user', '{"ui_language":"en"}', 3.125))
        source.execute('INSERT INTO user_passwords VALUES(?,?,?)', ('fixture-user', 'SYNTHETIC_HASH_NOT_A_PASSWORD', 3.25))
        source.execute('INSERT INTO sessions VALUES(?,?,?)', ('SYNTHETIC_SESSION_HASH', 'fixture-user', 999.875))
        source.executemany('INSERT INTO password_attempts VALUES(?,?,?,?)',
                           [('fixture@example.invalid', '192.0.2.1', 'login', 11.125)] * 2)
        source.execute('INSERT INTO memory_chunks VALUES(?,?,?,?,?,?,?,?,?)',
                       (17, 'fixture-user', 'chat', 'job', 'answer', None, 0, '中文 Alpha fixture', 3.625))
        source.execute('INSERT INTO memory_indexed VALUES(?,?)', ('job', 2))
        source.execute('INSERT INTO memory_preferences VALUES(?,?,?,?)', ('chat', 'fixture-user', 'auto', 1))
        source.execute('INSERT INTO memory_summaries VALUES(?,?,?,?,?,?,?,?,?)',
                       (23, 'chat', 'fixture-user', 'job', 4096, 'summary 中文', 4, 4.125, 'synthetic'))
        source.executemany('INSERT INTO memory_search(rowid,terms) VALUES(?,?)', [(17, '中文 alpha-beta'), (987, '孤立 alpha_beta')])
        source.execute('INSERT INTO document_chunks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (29, 'file', 'fixture-user', 0, None, 'section', 'text', '合成 document', b'\x00\x01\xff\x80\x00', 'fixture/model@v1:dim3'))
        source.execute('INSERT INTO document_indexes VALUES(?,?,?,?,?,?,?,?)', ('file', 'fixture-user', 'sha-literal', 'ready', 1, 1, '', 4.25))
        source.executemany('INSERT INTO document_search(rowid,terms) VALUES(?,?)', [(29, '合成 document'), (988, '孤立 document')])
        source.execute('INSERT INTO audit VALUES(?,?,?,?)', (43, None, 'fixture', 0.10000000000000002))
        source.execute('INSERT INTO identity_accounts VALUES(?,?,?,?,?,?,?)', ('fixture-user', 'platform', None, 'work-fixture', 'admin', None, 5.25))
        source.execute('INSERT INTO identity_permissions VALUES(?,?,?,?,?)', ('fixture-user', 'chat', 1, 5.375, 'fixture-user'))
        source.execute('INSERT INTO identity_operations VALUES(?,?,?,?,?,?,?)', (53, 'fixture-user', 'fixture-key', 'fixture', 'literal-fingerprint', '{"ok":true}', 5.5))
        source.execute('INSERT INTO identity_link_audit VALUES(?,?,?,?,?)', (57, 'fixture-user', 'fixture-user', 'fixture-user', 5.625))
        source.execute('INSERT INTO expert_feedback_rules VALUES(?,?,?,?,?,?)', (11, 'fixture', 'rule', 'criteria', 5.75, 'fixture-user'))
        source.execute('INSERT INTO expert_feedback_rules VALUES(?,?,?,?,?,?)', (99, 'deleted high water', '', '', 5.875, 'fixture-user'))
        source.execute('DELETE FROM expert_feedback_rules WHERE id=99')
        # FK checks are enabled on the read-only snapshot after both exist. The
        # child is deliberately physically earlier than its parent.
        source.executemany('INSERT INTO expert_feedback VALUES(?,?,?,?,?,?,?,?,?,?)', [
            ('child', 'job', 'fixture-user', 2, 'parent', 6.0, 7, 'digest', 'child', '{}'),
            ('parent', 'job', 'fixture-user', 1, None, 5.875, 7, 'digest', 'parent', '{}')])
        source.execute('INSERT INTO voice_calls(id,user_id,created,seconds,status) VALUES(?,?,?,?,?)',
                       ('voice', 'fixture-user', 6.125, 1.375, 'complete'))
        # expert_reviewers intentionally remains empty.


class SnapshotUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'self-made.sqlite3'
        make_snapshot(self.path)

    def test_exact_27_logical_plus_10_shadow_tables_and_fk_topology(self):
        before = self.path.read_bytes()
        with migration._source(self.path) as conn:
            fks = migration._source_schema(conn)
            self.assertEqual(len(fks), 27)
            self.assertEqual(len(migration.SHADOW_TABLES), 10)
            order = migration._order(fks)
            self.assertLess(order.index('users'), order.index('jobs'))
            self.assertLess(order.index('jobs'), order.index('expert_feedback'))
            self.assertEqual([r[0] for r in migration._rows(conn, 'expert_feedback')], ['parent', 'child'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_table_and_live_sidecar_refuse_closed(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE unknown_business(value TEXT)')
        with migration._source(self.path) as conn, self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_SOURCE_TABLE_SET$'):
            migration._source_schema(conn)
        sidecar = Path(str(self.path) + '-wal')
        sidecar.write_bytes(b'synthetic')
        with self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_STANDALONE_SNAPSHOT_REQUIRED$'):
            with migration._source(self.path):
                pass

    def test_typed_comparison_distinguishes_types_duplicates_and_float_bits(self):
        key = b'synthetic-ephemeral-test-key'
        layout = [('x', 'REAL', 0)]
        self.assertNotEqual(migration._fingerprint([(0.0,)], layout, key), migration._fingerprint([(-0.0,)], layout, key))
        self.assertNotEqual(migration._fingerprint([(1.25,)], layout, key), migration._fingerprint([(1.25,), (1.25,)], layout, key))
        self.assertEqual(migration._fingerprint([(1.25,), (2.5,)], layout, key), migration._fingerprint([(2.5,), (1.25,)], layout, key))
        self.assertEqual(migration._fingerprint([(float('inf'),)], layout, key), migration._fingerprint([(float('inf'),)], layout, key))
        for value in (1, '1.25', float('nan')):
            with self.assertRaises(migration.MigrationError):
                migration._fingerprint([(value,)], layout, key)
        self.assertNotEqual(migration._canonical([(b'x')], [('v', 'BLOB', 0)]), migration._canonical(['x'], [('v', 'TEXT', 0)]))


class PostgreSQLMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get('WUYU_PG_TEST_DSN')
        if not dsn:
            raise unittest.SkipTest('synthetic PostgreSQL fixture DSN not provided')
        import psycopg
        cls.psycopg = psycopg
        cls.dsn = dsn
        with psycopg.connect(dsn, autocommit=True) as conn:
            cls.database = conn.execute('SELECT current_database()').fetchone()[0]
            if not re.fullmatch(r'(?:wuyu_qa111_fixture|qa111_fixture)[a-z0-9_]*', cls.database):
                raise RuntimeError('synthetic database prefix required')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'self-made.sqlite3'
        make_snapshot(self.path)
        self.conn = self.psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.schema = 'qa111_migrate_' + uuid.uuid4().hex
        self.conn.execute('CREATE SCHEMA ' + migration._ident(self.schema))
        self.addCleanup(self.drop_own_schema)
        self.conn.execute("SELECT set_config('search_path',%s,false)", (self.schema,))
        for sql in schema_statements():
            for translated in pg_store.translate_ddl(sql):
                self.conn.execute(translated)
        self.conn.execute('CREATE TABLE wuyu_platform_schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        with self.conn.cursor() as cursor:
            cursor.executemany('INSERT INTO wuyu_platform_schema_meta VALUES(%s,%s)', [
                ('schema_version', pg_store.SCHEMA_VERSION), ('adapter_contract', pg_store.ADAPTER_CONTRACT), ('schema_signature', '0' * 64)])

    def drop_own_schema(self):
        if not re.fullmatch(r'qa111_migrate_[a-f0-9]{32}', self.schema):
            raise RuntimeError('owned schema guard')
        self.conn.execute('DROP SCHEMA ' + migration._ident(self.schema) + ' CASCADE')

    def migrate(self):
        return migration.migrate_snapshot(self.path, self.conn, expected_database=self.database, schema=self.schema)

    def verify(self):
        return migration.verify_snapshot(self.path, self.conn, expected_database=self.database, schema=self.schema)

    def test_all_columns_opaque_ciphertext_blob_float64_duplicates_orphans_and_empty(self):
        report = self.migrate()
        self.assertTrue(report['matched'])
        self.assertEqual(len(report['tables']), 27)
        self.assertEqual(report['tables']['password_attempts']['target_count'], 2)
        self.assertEqual(report['tables']['expert_reviewers']['target_count'], 0)
        self.assertEqual(self.conn.execute('SELECT vector,vector_model FROM document_chunks').fetchone(), (b'\x00\x01\xff\x80\x00', 'fixture/model@v1:dim3'))
        self.assertEqual(self.conn.execute('SELECT size FROM files').fetchone()[0], 2**53 + 7)
        self.assertEqual(self.conn.execute('SELECT rowid FROM memory_search ORDER BY rowid').fetchall(), [(17,), (987,)])
        self.assertEqual(self.conn.execute('SELECT rowid FROM document_search ORDER BY rowid').fetchall(), [(29,), (988,)])
        serialized = json.dumps(report)
        self.assertNotIn('gAAAA_', serialized)
        self.assertNotIn('SYNTHETIC_', serialized)
        self.assertTrue(self.verify()['matched'])

    def test_sequence_restarts_include_deleted_SQLite_AUTOINCREMENT_highwater(self):
        self.migrate()
        self.assertTrue(self.verify()['sequences_matched'])
        sequence = self.conn.execute("SELECT pg_get_serial_sequence(%s,'id')", (migration._ident(self.schema) + '.expert_feedback_rules',)).fetchone()[0]
        # Sequence-only corruption leaves every business row identical.
        self.conn.execute('ALTER SEQUENCE ' + sequence + ' RESTART WITH 12')
        bad = self.verify()
        self.assertFalse(bad['sequences_matched'])
        self.assertTrue(all(t['matched'] for t in bad['tables'].values()))
        self.conn.execute('ALTER SEQUENCE ' + sequence + ' RESTART WITH 100')
        expected = {'audit': 44, 'memory_chunks': 18, 'memory_summaries': 24, 'document_chunks': 30,
                    'expert_feedback_rules': 100, 'identity_operations': 54, 'identity_link_audit': 58}
        for table, column in pg_store.SERIAL_COLUMNS.items():
            value = self.conn.execute('SELECT nextval(pg_get_serial_sequence(%s,%s))',
                                      (migration._ident(self.schema) + '.' + migration._ident(table), column)).fetchone()[0]
            self.assertEqual(value, expected[table])
        # A genuine default-generated INSERT also succeeds, beyond all old IDs.
        value = self.conn.execute('INSERT INTO audit(actor,action,created) VALUES(NULL,%s,%s) RETURNING id', ('new fixture', 7.25)).fetchone()[0]
        self.assertEqual(value, 45)

    def test_each_original_column_tamper_detected_even_when_counts_unchanged(self):
        self.migrate()
        updates = [
            ("UPDATE settings SET value=value||'x'", 'settings', "UPDATE settings SET value=substr(value,1,length(value)-1)", ()),
            ("UPDATE document_chunks SET vector=%s", 'document_chunks', "UPDATE document_chunks SET vector=%s", (b'changed',)),
            ("UPDATE jobs SET reserved=reserved+1", 'jobs', 'UPDATE jobs SET reserved=reserved-1', ()),
            ("UPDATE voice_calls SET seconds=seconds+0.125", 'voice_calls', 'UPDATE voice_calls SET seconds=seconds-0.125', ()),
            ("UPDATE memory_search SET terms=terms||' alpha' WHERE rowid=987", 'memory_search', "UPDATE memory_search SET terms='孤立 alpha_beta' WHERE rowid=987", ()),
        ]
        for sql, table, undo, value in updates:
            previous_blob = self.conn.execute('SELECT vector FROM document_chunks').fetchone()[0] if table == 'document_chunks' else None
            self.conn.execute(sql, value or None)
            result = self.verify()
            self.assertFalse(result['matched'])
            self.assertFalse(result['tables'][table]['matched'])
            self.conn.execute(undo, (previous_blob,) if table == 'document_chunks' else None)
            self.assertTrue(self.verify()['matched'])
        # The matching flag changes independently for derived token corruption.
        self.conn.execute("UPDATE document_search SET tokens=ARRAY['wrong'] WHERE rowid=988")
        self.assertFalse(self.verify()['tables']['document_search']['tokens_matched'])

    def test_actual_memory_and_document_index_functions_write_upserts_on_PG(self):
        self.migrate()
        outer = self

        class Database:
            @contextlib.contextmanager
            def connect(self):
                raw = outer.psycopg.connect(outer.dsn, autocommit=True)
                raw.execute("SELECT set_config('search_path',%s,false)", (outer.schema,))
                conn = pg_store.Connection(raw)
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

            def query(self, sql, values=(), one=False):
                with self.connect() as conn:
                    rows = [dict(row) for row in conn.execute(sql, values).fetchall()]
                return (rows[0] if rows else None) if one else rows

            def execute(self, sql, values=()):
                with self.connect() as conn:
                    return conn.execute(sql, values)

            def save_secret(self, *_):
                pass

            def audit(self, actor, action):
                self.execute('INSERT INTO audit(actor,action,created) VALUES(?,?,?)', (actor, action, 8.125))

        def load(module, names, namespace):
            tree = ast.parse((Path(__file__).parent / (module + '.py')).read_text(encoding='utf-8'))
            funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
            for func in funcs:
                func.decorator_list = []
                if func.name == 'save_config':
                    func.args.defaults = []
                func.returns = None
                for arg in func.args.args:
                    arg.annotation = None
            selected = ast.Module(funcs, type_ignores=[])
            exec(compile(selected, 'actual-' + module, 'exec'), namespace)
            scopes = symtable.symtable(ast.unparse(selected), 'actual-' + module, 'exec').get_children()
            missing = set().union(*(set(scope.get_globals()) for scope in scopes)) - namespace.keys() - vars(builtins).keys()
            self.assertEqual(missing, set(), 'AST fixture must provide every referenced function global')

        from . import lexical_search
        db = Database()
        attachment = types.ModuleType('attachments')
        attachment.read_file = lambda _: {'kind': 'document', 'name': 'self-made', 'text': '文档 Alpha synthetic'}
        memory = {'db': db, 'lexical_search': lexical_search, 'json': json, 're': re, 'INDEX_VERSION': 1}
        load('memory', {'terms', 'segments', 'answer_text', 'index_job'}, memory)
        self.conn.execute('UPDATE jobs SET turn=%s', (json.dumps({'user': 'question 中文', 'answer': 'answer Alpha'}),))
        with patch.dict(sys.modules, {'attachments': attachment}):
            memory['index_job']('job')
            memory['index_job']('job')
        self.assertEqual(self.conn.execute('SELECT version FROM memory_indexed').fetchall(), [(1,)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM memory_chunks').fetchone()[0], 2)
        document = {'db': db, 'lexical_search': lexical_search, 'attachments': attachment, 'time': time, 'json': json, 'Path': Path,
                    'memory': types.SimpleNamespace(terms=memory['terms']), 'VERSION': 'synthetic-version-v2',
                    'owned': lambda *_: None, 'HTTPException': RuntimeError, 'admin_config': lambda user: db.query(
                        "SELECT value FROM settings WHERE key='document_rag_config'", one=True)}
        load('document_rag', {'split_text', 'ensure_index', 'save_config'}, document)
        value = document['ensure_index']('fixture-user', 'file')
        self.assertEqual(value['version'], 'synthetic-version-v2')
        self.assertEqual(document['ensure_index']('fixture-user', 'file')['chunks'], 1)
        for dimensions in (128, 64):
            body = types.SimpleNamespace(model_dump=lambda **_: {'enabled': False, 'semantic_enabled': False, 'dimensions': dimensions},
                                         api_key=types.SimpleNamespace(get_secret_value=lambda: ''))
            document['save_config'](body, {'id': 'fixture-user'})
        result = self.conn.execute("SELECT value FROM settings WHERE key='document_rag_config'").fetchall()
        self.assertEqual(len(result), 1)
        self.assertEqual(json.loads(result[0][0])['dimensions'], 64)

    def test_target_nonempty_wrong_database_unknown_source_and_nesting_refuse(self):
        self.conn.execute('INSERT INTO settings VALUES(%s,%s)', ('existing', 'synthetic'))
        with self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_TARGET_NOT_EMPTY$'):
            self.migrate()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM users').fetchone()[0], 0)
        with self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_TARGET_DATABASE_MISMATCH$'):
            migration.migrate_snapshot(self.path, self.conn, expected_database='wrong', schema=self.schema)
        with self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_EMPTY_TARGET_REQUIRED$'):
            migration.migrate_snapshot(self.path, self.conn, expected_database=self.database, schema=self.schema, require_empty=False)
        with self.conn.transaction(), self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_IDLE_AUTOCOMMIT_CONNECTION_REQUIRED$'):
            self.migrate()
        with contextlib.closing(sqlite3.connect(self.path)) as source, source:
            source.execute('CREATE TABLE unknown_business(value TEXT)')
        with self.assertRaisesRegex(migration.MigrationError, '^PLATFORM_MIGRATION_SOURCE_TABLE_SET$'):
            self.migrate()

    def test_failed_comparison_rolls_back_all_rows_and_redacts_driver_error(self):
        with patch.object(migration, '_verify', return_value={'matched': False}), self.assertRaisesRegex(
                migration.MigrationError, '^PLATFORM_MIGRATION_CONTENT_MISMATCH$'):
            self.migrate()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM users').fetchone()[0], 0)
        with patch.object(migration, '_verify', side_effect=RuntimeError('SYNTHETIC_PRIVATE_VALUE')), self.assertRaisesRegex(
                migration.MigrationError, '^PLATFORM_MIGRATION_FAILED$') as error:
            self.migrate()
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', str(error.exception))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM settings').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
