"""Explicit, offline SQLite snapshot -> prebootstrapped empty PostgreSQL target.

This module is an operator tool, never an application startup migration. It
accepts a standalone read-only SQLite snapshot and an idle raw psycopg 3
connection. It creates no database/schema/table, never decrypts values, and
returns only counts and equality booleans. Approval and stopping all writers
remain operator responsibilities. Nothing runs when this module is imported.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import math
from pathlib import Path
import re
import secrets
import sqlite3
import struct

from .lexical_search import tokenize_terms
from .pg_store import ADAPTER_CONTRACT, SCHEMA_VERSION, SERIAL_COLUMNS


_LAYOUT = {
    'audit': 'id:INTEGER:1 actor:TEXT:0 action:TEXT:0 created:REAL:0',
    'conversations': 'id:TEXT:1 user_id:TEXT:0 title:TEXT:0 created:REAL:0 updated:REAL:0 deleted:INTEGER:0',
    'document_chunks': 'id:INTEGER:1 file_id:TEXT:0 user_id:TEXT:0 position:INTEGER:0 page:INTEGER:0 section:TEXT:0 modality:TEXT:0 text:TEXT:0 vector:BLOB:0 vector_model:TEXT:0',
    'document_indexes': 'file_id:TEXT:1 user_id:TEXT:0 version:TEXT:0 status:TEXT:0 chunks:INTEGER:0 embedded:INTEGER:0 error:TEXT:0 updated:REAL:0',
    'expert_feedback': 'id:TEXT:1 job_id:TEXT:0 reviewer_id:TEXT:0 revision:INTEGER:0 supersedes:TEXT:0 created:REAL:0 turn_revision:INTEGER:0 turn_digest:TEXT:0 content:TEXT:0 rule_snapshot:TEXT:0',
    'expert_feedback_rules': 'id:INTEGER:1 title:TEXT:0 rules:TEXT:0 criteria:TEXT:0 created:REAL:0 actor:TEXT:0',
    'expert_reviewers': 'user_id:TEXT:1 granted_by:TEXT:0 created:REAL:0',
    'files': 'id:TEXT:1 user_id:TEXT:0 size:INTEGER:0 created:REAL:0',
    'identity_accounts': 'user_id:TEXT:1 account_type:TEXT:0 legacy_alias:TEXT:0 work_user_id:TEXT:0 work_role:TEXT:0 linked_to_user_id:TEXT:0 created:REAL:0',
    'identity_link_audit': 'sequence:INTEGER:1 legacy_user_id:TEXT:0 primary_user_id:TEXT:0 actor_user_id:TEXT:0 created:REAL:0',
    'identity_operations': 'sequence:INTEGER:1 actor_user_id:TEXT:0 request_key:TEXT:0 operation:TEXT:0 fingerprint:TEXT:0 result:TEXT:0 created:REAL:0',
    'identity_permissions': 'user_id:TEXT:1 feature:TEXT:2 allowed:INTEGER:0 updated:REAL:0 actor_user_id:TEXT:0',
    'job_events': 'job_id:TEXT:1 sequence:INTEGER:2 created:REAL:0 event:TEXT:0',
    'jobs': 'id:TEXT:1 user_id:TEXT:0 conversation_id:TEXT:0 status:TEXT:0 input:TEXT:0 turn:TEXT:0 created:REAL:0 started:REAL:0 finished:REAL:0 reserved:INTEGER:0 charged:INTEGER:0 inflight:INTEGER:0 day:TEXT:0 revision:INTEGER:0',
    'memory_chunks': 'id:INTEGER:1 user_id:TEXT:0 conversation_id:TEXT:0 job_id:TEXT:0 kind:TEXT:0 file_id:TEXT:0 position:INTEGER:0 text:TEXT:0 created:REAL:0',
    'memory_indexed': 'job_id:TEXT:1 version:INTEGER:0',
    'memory_preferences': 'conversation_id:TEXT:1 user_id:TEXT:0 mode:TEXT:0 auto_compress:INTEGER:0',
    'memory_summaries': 'id:INTEGER:1 conversation_id:TEXT:0 user_id:TEXT:0 watermark:TEXT:0 budget:INTEGER:0 content:TEXT:0 token_estimate:INTEGER:0 created:REAL:0 method:TEXT:0',
    'password_attempts': 'email:TEXT:0 ip:TEXT:0 action:TEXT:0 created:REAL:0',
    'sessions': 'hash:TEXT:1 user_id:TEXT:0 expires:REAL:0',
    'settings': 'key:TEXT:1 value:TEXT:0',
    'user_passwords': 'user_id:TEXT:1 password_hash:TEXT:0 updated:REAL:0',
    'user_preferences': 'user_id:TEXT:1 value:TEXT:0 updated:REAL:0',
    'users': 'id:TEXT:1 email:TEXT:0 name:TEXT:0 organization:TEXT:0 identity:TEXT:0 role:TEXT:0 active:INTEGER:0 created:REAL:0',
    'voice_calls': 'id:TEXT:1 user_id:TEXT:0 created:REAL:0 seconds:REAL:0 status:TEXT:0 purpose:TEXT:0 target_job_id:TEXT:0 field:TEXT:0 transcript:TEXT:0 model_label:TEXT:0 credit_user_id:TEXT:0 credit_reservation_id:TEXT:0 credit_status:TEXT:0',
}
SEARCH_TABLES = frozenset(('memory_search', 'document_search'))
LOGICAL_TABLES = frozenset(_LAYOUT) | SEARCH_TABLES
SHADOW_TABLES = frozenset(t + s for t in SEARCH_TABLES for s in
                          ('_data', '_idx', '_content', '_docsize', '_config'))
_PG_TYPES = {'INTEGER': 'int8', 'REAL': 'float8', 'BLOB': 'bytea', 'TEXT': 'text'}
_NAME = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')


class MigrationError(RuntimeError):
    """Stable codes only: raw SQL/driver errors may contain private row values."""


def _fail(code):
    raise MigrationError('PLATFORM_MIGRATION_' + code)


def _ident(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _fail('IDENTIFIER_INVALID')
    return '"' + value + '"'


def _layout(table):
    if table in SEARCH_TABLES:
        return [('rowid', 'INTEGER', 1), ('terms', 'TEXT', 0)]
    return [(n, t, int(pk)) for n, t, pk in (s.split(':') for s in _LAYOUT[table].split())]


@contextlib.contextmanager
def _source(snapshot):
    path = Path(snapshot).resolve(strict=True)
    if not path.is_file() or any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        _fail('STANDALONE_SNAPSHOT_REQUIRED')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            _fail('SOURCE_INTEGRITY')
        yield conn
    finally:
        conn.close()


def _source_schema(conn):
    entries = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
    tables = {n: sql for n, sql in entries if not n.startswith('sqlite_')}
    if set(tables) != LOGICAL_TABLES | SHADOW_TABLES:
        _fail('SOURCE_TABLE_SET')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        _fail('SOURCE_EXECUTABLE_SCHEMA')
    fks = {}
    for table in sorted(LOGICAL_TABLES):
        columns = conn.execute('PRAGMA table_info(' + _ident(table) + ')').fetchall()
        actual = [(r[1], r[2].upper(), r[5]) for r in columns]
        if table in SEARCH_TABLES:
            if actual != [('terms', '', 0)] or not re.search(
                    r"USING\s+fts5\s*\(\s*terms\s*,\s*tokenize\s*=\s*'unicode61'\s*\)", tables[table], re.I):
                _fail('SOURCE_SEARCH_SCHEMA')
        elif actual != _layout(table):
            _fail('SOURCE_COLUMN_SCHEMA')
        fks[table] = {(r[3], r[2], r[4], r[5], r[6]) for r in
                      conn.execute('PRAGMA foreign_key_list(' + _ident(table) + ')')}
        if any(parent not in LOGICAL_TABLES for _, parent, _, _, _ in fks[table]):
            _fail('SOURCE_FOREIGN_KEY')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        _fail('SOURCE_FOREIGN_KEY')
    return fks


def _target_schema(conn, schema, expected_database, source_fks):
    if not isinstance(expected_database, str) or not expected_database:
        _fail('TARGET_DATABASE_REQUIRED')
    if conn.execute('SELECT current_database()').fetchone()[0] != expected_database:
        _fail('TARGET_DATABASE_MISMATCH')
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'", (schema,))}
    if tables != LOGICAL_TABLES | {'wuyu_platform_schema_meta'}:
        _fail('TARGET_TABLE_SET')
    marker = dict(conn.execute('SELECT key,value FROM ' + _ident(schema) + '.wuyu_platform_schema_meta'))
    if (set(marker) != {'schema_version', 'adapter_contract', 'schema_signature'} or
            marker.get('schema_version') != SCHEMA_VERSION or marker.get('adapter_contract') != ADAPTER_CONTRACT or
            not re.fullmatch('[0-9a-f]{64}', marker.get('schema_signature', ''))):
        _fail('TARGET_BOOTSTRAP_REQUIRED')
    for table in sorted(LOGICAL_TABLES):
        cols = list(conn.execute('SELECT column_name,udt_name FROM information_schema.columns '
                                'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (schema, table)))
        wanted = [(name, _PG_TYPES[kind]) for name, kind, _ in _layout(table)]
        if table in SEARCH_TABLES:
            wanted += [('tokens', '_text')]
        if cols != wanted:
            _fail('TARGET_COLUMN_SCHEMA')
        primary = list(conn.execute('SELECT a.attname FROM pg_constraint c '
                                   'JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace '
                                   'CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ord) '
                                   'JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum '
                                   "WHERE n.nspname=%s AND t.relname=%s AND c.contype='p' ORDER BY k.ord", (schema, table)))
        if [r[0] for r in primary] != [name for name, _, pk in sorted(_layout(table), key=lambda c: c[2]) if pk]:
            _fail('TARGET_PRIMARY_KEY')
        # This schema has only single-column FKs. Compare all actions as well.
        foreign = set(conn.execute('SELECT a.attname,p.relname,pa.attname,c.confupdtype,c.confdeltype,pn.nspname '
                                  'FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid '
                                  'JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_class p ON p.oid=c.confrelid '
                                  'JOIN pg_namespace pn ON pn.oid=p.relnamespace '
                                  'JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=c.conkey[1] '
                                  'JOIN pg_attribute pa ON pa.attrelid=p.oid AND pa.attnum=c.confkey[1] '
                                  "WHERE n.nspname=%s AND t.relname=%s AND c.contype='f'", (schema, table)))
        actions = {'NO ACTION': 'a', 'RESTRICT': 'r', 'CASCADE': 'c', 'SET NULL': 'n', 'SET DEFAULT': 'd'}
        wanted_fk = {(col, parent, pcol, actions[update], actions[delete], schema)
                     for col, parent, pcol, update, delete in source_fks[table]}
        if foreign != wanted_fk:
            _fail('TARGET_FOREIGN_KEY')


def _order(fks):
    pending = {table: {parent for _, parent, _, _, _ in refs if parent != table} for table, refs in fks.items()}
    result = []
    while pending:
        ready = sorted(t for t, parents in pending.items() if not parents)
        if not ready:
            _fail('FOREIGN_KEY_CYCLE')
        for table in ready:
            result.append(table)
            del pending[table]
        for parents in pending.values():
            parents.difference_update(ready)
    return result


def _select(table, schema=None):
    columns = ','.join(_ident(c[0]) for c in _layout(table))
    target = (_ident(schema) + '.' if schema else '') + _ident(table)
    return 'SELECT ' + columns + ' FROM ' + target


def _rows(source, table):
    cursor = source.execute(_select(table))
    if table == 'expert_feedback':
        # Self-referential supersedes can point to a row inserted later in SQLite.
        rows = cursor.fetchall()
        seen = set()
        while rows:
            ready = [r for r in rows if r[4] is None or r[4] in seen]
            if not ready:
                _fail('FOREIGN_KEY_CYCLE')
            for row in ready:
                yield row
                seen.add(row[0])
            rows = [r for r in rows if r[0] not in seen]
    else:
        yield from cursor


def _canonical(row, layout):
    result = bytearray()
    for value, (_, kind, pk) in zip(row, layout):
        if value is None:
            if pk:
                _fail('SOURCE_NULL_PRIMARY_KEY')
            data = b'n'
        elif kind == 'INTEGER' and type(value) is int and -(2**63) <= value < 2**63:
            data = b'i' + struct.pack('>q', value)
        elif kind == 'REAL' and type(value) is float and not math.isnan(value):
            data = b'f' + struct.pack('>d', value)
        elif kind == 'TEXT' and type(value) is str and '\0' not in value:
            data = b's' + value.encode('utf-8')
        elif kind == 'BLOB' and isinstance(value, (bytes, memoryview)):
            data = b'b' + bytes(value)
        else:
            _fail('VALUE_TYPE_NOT_REPRESENTABLE')
        result += struct.pack('>Q', len(data)) + data
    return bytes(result)


def _fingerprint(rows, layout, key):
    digests = [hmac.digest(key, _canonical(row, layout), 'sha256') for row in rows]
    # Sorting per-row MACs preserves duplicate multiplicity without depending on
    # physical row order. Memory is 32 bytes per row plus Python list overhead;
    # full text/BLOB values are not retained. MACs and key never leave the call.
    digest = hmac.new(key, struct.pack('>Q', len(digests)), hashlib.sha256)
    for value in sorted(digests):
        digest.update(value)
    return len(digests), digest.digest()


def _verify(source, conn, schema):
    key = secrets.token_bytes(32)
    report = {}
    for table in sorted(LOGICAL_TABLES):
        columns = _layout(table)
        count, original = _fingerprint(source.execute(_select(table)), columns, key)
        target_count, target = _fingerprint(conn.execute(_select(table, schema)), columns, key)
        tokens_match = True
        if table in SEARCH_TABLES:
            tokens_match = all(tokens == tokenize_terms(terms) for terms, tokens in
                               conn.execute('SELECT terms,tokens FROM ' + _ident(schema) + '.' + _ident(table)))
        report[table] = {'source_count': count, 'target_count': target_count,
                         'matched': count == target_count and hmac.compare_digest(original, target),
                         'tokens_matched': tokens_match}
    return {'matched': all(r['matched'] and r['tokens_matched'] for r in report.values()), 'tables': report}


def _sequence_state(source, conn, schema):
    highwater = dict(source.execute('SELECT name,seq FROM sqlite_sequence')) if source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone() else {}
    for table, column in SERIAL_COLUMNS.items():
        qualified = _ident(schema) + '.' + _ident(table)
        sequence = conn.execute('SELECT pg_get_serial_sequence(%s,%s)', (qualified, column)).fetchone()[0]
        if not sequence:
            _fail('TARGET_SEQUENCE_MISSING')
        # Resolve and validate ownership; never interpolate a driver-provided SQL
        # identifier. ALTER SEQUENCE RESTART is transactional (setval is not).
        seq = conn.execute('SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                           "WHERE c.oid=%s::regclass AND c.relkind='S'", (sequence,)).fetchone()
        if not seq or seq[0] != schema:
            _fail('TARGET_SEQUENCE_SCHEMA')
        largest = conn.execute('SELECT max(' + _ident(column) + ') FROM ' + qualified).fetchone()[0]
        value = max(1, (largest or 0) + 1, highwater.get(table, 0) + 1)
        if value >= 2**63:
            _fail('SOURCE_SEQUENCE_EXHAUSTED')
        yield seq, value


def _reset_sequences(source, conn, schema):
    for seq, value in _sequence_state(source, conn, schema):
        conn.execute('ALTER SEQUENCE ' + _ident(seq[0]) + '.' + _ident(seq[1]) + ' RESTART WITH ' + str(value))


def _verify_sequences(source, conn, schema):
    matched = True
    for seq, minimum in _sequence_state(source, conn, schema):
        qualified = _ident(seq[0]) + '.' + _ident(seq[1])
        last, called = conn.execute('SELECT last_value,is_called FROM ' + qualified).fetchone()
        increment = conn.execute('SELECT seqincrement FROM pg_sequence WHERE seqrelid=%s::regclass', (qualified,)).fetchone()[0]
        matched = matched and increment == 1 and (last + increment if called else last) >= minimum
    return matched


def _connection_ready(conn):
    # Raw psycopg connection only; nested/existing operator transactions could
    # make commit/rollback and read-only guarantees ambiguous.
    if not getattr(conn, 'autocommit', False) or int(conn.info.transaction_status) != 0:
        _fail('IDLE_AUTOCOMMIT_CONNECTION_REQUIRED')


def migrate_snapshot(snapshot_path, connection, *, expected_database, schema='public', require_empty=True):
    """Atomically import all 27 logical tables into an explicitly empty target.

    `connection` is a raw psycopg 3 autocommit connection, not the runtime store
    adapter. Caller bootstraps the schema first and obtains cutover approval.
    No DSN/key/value is returned or printed, including on database exceptions.
    """
    try:
        if require_empty is not True:
            _fail('EMPTY_TARGET_REQUIRED')
        _ident(schema)
        _connection_ready(connection)
        with _source(snapshot_path) as source, connection.transaction():
            connection.execute("SET LOCAL lock_timeout='10s'")
            connection.execute("SET LOCAL statement_timeout='60s'")
            fks = _source_schema(source)
            _target_schema(connection, schema, expected_database, fks)
            connection.execute('LOCK TABLE ' + ','.join(_ident(schema) + '.' + _ident(t) for t in sorted(LOGICAL_TABLES)) + ' IN ACCESS EXCLUSIVE MODE')
            for table in sorted(LOGICAL_TABLES):
                if connection.execute('SELECT EXISTS(SELECT 1 FROM ' + _ident(schema) + '.' + _ident(table) + ')').fetchone()[0]:
                    _fail('TARGET_NOT_EMPTY')
            for table in _order(fks):
                columns = _layout(table)
                names = [c[0] for c in columns] + (['tokens'] if table in SEARCH_TABLES else [])
                sql = 'INSERT INTO ' + _ident(schema) + '.' + _ident(table) + '(' + ','.join(map(_ident, names)) + ') VALUES(' + ','.join(['%s'] * len(names)) + ')'
                with connection.cursor() as cursor:
                    batch = []
                    for row in _rows(source, table):
                        _canonical(row, columns)
                        values = tuple(row)
                        if table in SEARCH_TABLES:
                            values += (tokenize_terms(row[1]),)
                        batch.append(values)
                        if len(batch) == 500:
                            cursor.executemany(sql, batch)
                            batch.clear()
                    if batch:
                        cursor.executemany(sql, batch)
            report = _verify(source, connection, schema)
            if not report['matched']:
                _fail('CONTENT_MISMATCH')
            _reset_sequences(source, connection, schema)
            report['sequences_matched'] = _verify_sequences(source, connection, schema)
            if not report['sequences_matched']:
                _fail('SEQUENCE_MISMATCH')
        return report
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('PLATFORM_MIGRATION_FAILED') from None


def verify_snapshot(snapshot_path, connection, *, expected_database, schema='public'):
    """Read-only, consistent typed comparison; detects row and token tampering."""
    try:
        _ident(schema)
        _connection_ready(connection)
        with _source(snapshot_path) as source, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            connection.execute("SET LOCAL statement_timeout='60s'")
            fks = _source_schema(source)
            _target_schema(connection, schema, expected_database, fks)
            report = _verify(source, connection, schema)
            report['sequences_matched'] = _verify_sequences(source, connection, schema)
            report['matched'] = report['matched'] and report['sequences_matched']
            return report
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('PLATFORM_MIGRATION_VERIFICATION_FAILED') from None
