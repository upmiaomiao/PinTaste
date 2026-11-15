"""Narrow PostgreSQL DB-API contract for the existing platform SQL callers.

No SQLite fallback, implicit schema creation, unbounded retry or SQL error text
is exposed. SQLite remains a separate store backend for compatibility tests.
"""
from __future__ import annotations

import math
import hashlib
import json
import re
import sqlite3
from decimal import Decimal

SCHEMA_VERSION = '1'
ADAPTER_CONTRACT = 'platform-pg111-v1'
WRITE_LOCK = (0x57555955, 0x504C4154)
SERIAL_COLUMNS = {
    'memory_chunks': 'id', 'memory_summaries': 'id', 'audit': 'id',
    'document_chunks': 'id', 'expert_feedback_rules': 'id',
    'identity_operations': 'sequence', 'identity_link_audit': 'sequence',
}
SEARCH_TABLES = {'memory_search', 'document_search'}


def _error(error):
    state = getattr(error, 'sqlstate', None)
    if isinstance(state, str) and state.startswith('23'):
        result = sqlite3.IntegrityError('PLATFORM_DATABASE_INTEGRITY_ERROR')
    elif isinstance(state, str) and state.startswith(('42', '22')):
        result = sqlite3.OperationalError('PLATFORM_DATABASE_STATEMENT_ERROR')
    else:
        result = sqlite3.OperationalError('PLATFORM_DATABASE_UNAVAILABLE')
    if isinstance(state, str) and re.fullmatch('[A-Z0-9]{5}', state):
        result.sqlstate = state
    return result


def _value(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


class Row:
    """sqlite3.Row's key/index and value-iteration contract."""
    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(_value(v) for v in values)
        self._index = {}
        for i, name in enumerate(self._columns):
            self._index.setdefault(name, i)

    def keys(self):
        return list(self._columns)

    def __getitem__(self, key):
        return self._values[self._index[key]] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class Cursor:
    def __init__(self, rows=(), columns=(), *, rowcount=-1, lastrowid=None):
        self._rows = list(rows)
        self._position = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = tuple((c, None, None, None, None, None, None) for c in columns) if columns else None

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        result = self._rows[self._position]
        self._position += 1
        return result

    def fetchall(self):
        result = self._rows[self._position:]
        self._position = len(self._rows)
        return result

    def fetchmany(self, size=1):
        result = self._rows[self._position:self._position + size]
        self._position += len(result)
        return result

    def close(self):
        self._rows = []

    def __iter__(self):
        while True:
            value = self.fetchone()
            if value is None:
                return
            yield value


def _scan(sql, *, parameters=False, split=False):
    """Recognize SQLite literals/comments; bind only unquoted question marks."""
    out = []
    parts = []
    count = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if sql.startswith('--', i):
            end = sql.find('\n', i)
            end = len(sql) if end < 0 else end
            text = sql[i:end]
            out.append(text.replace('%', '%%') if parameters else text)
            i = end
        elif sql.startswith('/*', i):
            end = sql.find('*/', i + 2)
            if end < 0:
                raise sqlite3.ProgrammingError('PLATFORM_SQL_LITERAL_INVALID')
            text = sql[i:end + 2]
            out.append(text.replace('%', '%%') if parameters else text)
            i = end + 2
        elif ch in "'\"`[":
            quote = ']' if ch == '[' else ch
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote and quote != ']':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise sqlite3.ProgrammingError('PLATFORM_SQL_LITERAL_INVALID')
            text = sql[start:i]
            out.append(text.replace('%', '%%') if parameters else text)
        elif ch == '?' and not split:
            out.append('%s')
            count += 1
            i += 1
        elif ch == ';' and split:
            if ''.join(out).strip():
                parts.append(''.join(out).strip())
            out = []
            i += 1
        else:
            out.append('%%' if ch == '%' and parameters else ch)
            i += 1
    text = ''.join(out).strip()
    if split:
        if text:
            parts.append(text)
        return parts
    return text, count


def split_statements(script):
    return _scan(script, split=True)


def bind_sql(sql, values=()):
    if values is None:
        values = ()
    if isinstance(values, dict):
        raise sqlite3.ProgrammingError('PLATFORM_SQL_BINDINGS_INVALID')
    parameters = tuple(int(v) if isinstance(v, bool) else v for v in values)
    translated, count = _scan(sql, parameters=bool(parameters))
    if count != len(parameters):
        raise sqlite3.ProgrammingError('PLATFORM_SQL_BINDINGS_INVALID')
    return translated, parameters or None


def translate_ddl(sql):
    virtual = re.fullmatch(r"CREATE\s+VIRTUAL\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(memory_search|document_search)\s+USING\s+fts5\(terms,\s*tokenize='unicode61'\)", sql.strip(), re.I)
    if virtual:
        name = virtual.group(1).lower()
        return [f"CREATE TABLE IF NOT EXISTS {name}(rowid BIGINT PRIMARY KEY,terms TEXT NOT NULL,tokens TEXT[] NOT NULL DEFAULT '{{}}')",
                f'CREATE INDEX IF NOT EXISTS {name}_tokens ON {name} USING GIN(tokens)']
    if not re.match(r'^(CREATE\s+(?:TABLE|INDEX)|ALTER\s+TABLE)\b', sql.strip(), re.I):
        raise sqlite3.ProgrammingError('PLATFORM_SCHEMA_STATEMENT_UNSUPPORTED')
    # These scripts are fixed repository DDL, not user input. Replace only type
    # tokens outside literals so defaults/check expressions keep their bytes.
    pieces = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(pieces), 2):
        value = pieces[i]
        value = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY(?:\s+AUTOINCREMENT)?\b', 'BIGSERIAL PRIMARY KEY', value, flags=re.I)
        value = re.sub(r'\bINTEGER\b', 'BIGINT', value, flags=re.I)
        value = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', value, flags=re.I)
        value = re.sub(r'\bBLOB\b', 'BYTEA', value, flags=re.I)
        pieces[i] = value
    return [''.join(pieces)]


def _columns(body):
    result = []
    start = 0
    depth = 0
    quote = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "'":
            if quote and i + 1 < len(body) and body[i + 1] == "'":
                i += 2
                continue
            quote = not quote
        elif not quote:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                result.append(body[start:i].strip())
                start = i + 1
        i += 1
    result.append(body[start:].strip())
    return result


class Connection:
    dialect = 'postgresql'

    def __init__(self, raw, *, readonly=False, allow_schema=False, lock_timeout_ms=10000, release=None):
        self.raw = raw
        self._release = release
        self._closed = False
        self.readonly = readonly
        self.allow_schema = allow_schema
        self.lock_timeout_ms = lock_timeout_ms
        self._transaction = False
        self._transaction_readonly = False
        self._lastrowid = None
        self._savepoint = 0

    @property
    def in_transaction(self):
        return self._transaction

    def _ensure_open(self):
        if self._closed:
            raise sqlite3.ProgrammingError('PLATFORM_DATABASE_CONNECTION_CLOSED')

    def _begin(self, *, snapshot=False, write=True):
        self._ensure_open()
        if self._transaction:
            raise sqlite3.OperationalError('PLATFORM_TRANSACTION_ALREADY_ACTIVE')
        read_only = snapshot or self.readonly
        try:
            self.raw.execute('BEGIN ISOLATION LEVEL ' + ('REPEATABLE READ' if snapshot else 'READ COMMITTED') + (' READ ONLY' if read_only else ' READ WRITE'))
            self._transaction = True
            self._transaction_readonly = read_only
            if write and not read_only:
                self.raw.execute('SET LOCAL lock_timeout = ' + str(self.lock_timeout_ms))
                self.raw.execute('SELECT pg_advisory_xact_lock(%s,%s)', WRITE_LOCK)
        except Exception as error:
            self._rollback_after_error()
            raise _error(error) from None

    def _rollback_after_error(self):
        # A broken transport may also reject rollback. Keep the original
        # sanitized error and never expose cleanup's raw driver exception.
        try:
            self.raw.rollback()
        except Exception:
            pass
        finally:
            self._transaction = False
            self._transaction_readonly = False

    def begin_snapshot(self):
        self._begin(snapshot=True, write=False)

    def _run(self, sql, values=(), *, implicit_last_id=False):
        self._ensure_open()
        translated, parameters = bind_sql(sql, values)
        sp = None
        if self._transaction:
            self._savepoint += 1
            sp = 'platform_stmt_' + str(self._savepoint)
        saved = False
        try:
            if sp:
                self.raw.execute('SAVEPOINT ' + sp)
                saved = True
            cursor = self.raw.execute(translated, parameters)
            description = cursor.description
            columns = [c.name for c in description] if description else []
            fetched = cursor.fetchall() if description else []
            rows = [Row(columns, r) for r in fetched]
            rowcount = cursor.rowcount if not re.match(r'^(SELECT|WITH|SHOW)\b', sql.lstrip(), re.I) else -1
            if implicit_last_id and rows:
                self._lastrowid = rows[0][0]
                rows, columns = [], []
            cursor.close()
            if saved:
                self.raw.execute('RELEASE SAVEPOINT ' + sp)
            return Cursor(rows, columns, rowcount=rowcount, lastrowid=self._lastrowid)
        except Exception as error:
            if saved:
                try:
                    self.raw.execute('ROLLBACK TO SAVEPOINT ' + sp)
                    self.raw.execute('RELEASE SAVEPOINT ' + sp)
                except Exception:
                    self._rollback_after_error()
            raise _error(error) from None

    def execute(self, sql, values=()):
        self._ensure_open()
        text = sql.strip().rstrip(';').strip()
        if not text:
            raise sqlite3.ProgrammingError('PLATFORM_SQL_EMPTY')
        if re.fullmatch(r'BEGIN(?:\s+(?:IMMEDIATE|DEFERRED|TRANSACTION))?', text, re.I):
            self._begin(write=not self.readonly)
            return Cursor()
        if re.fullmatch(r'COMMIT(?:\s+TRANSACTION)?', text, re.I):
            self.commit()
            return Cursor()
        if re.fullmatch(r'ROLLBACK(?:\s+TRANSACTION)?', text, re.I):
            self.rollback()
            return Cursor()
        if re.match(r'^PRAGMA\b', text, re.I):
            raise sqlite3.ProgrammingError('PLATFORM_SQLITE_PRAGMA_NOT_ALLOWED')
        if re.match(r'^(CREATE|ALTER|DROP)\b', text, re.I):
            if values:
                raise sqlite3.ProgrammingError('PLATFORM_SCHEMA_BINDINGS_NOT_ALLOWED')
            for statement in translate_ddl(text):
                if self.allow_schema:
                    if self.readonly:
                        raise sqlite3.OperationalError('PLATFORM_DATABASE_READ_ONLY')
                    if not self._transaction:
                        self._begin()
                    self._run(statement)
                    self.validate_ddl(statement)
                else:
                    self.validate_ddl(statement)
            return Cursor()
        code = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/", ' ', text, flags=re.S)
        read = bool(re.match(r'^(SELECT|WITH|SHOW|EXPLAIN)\b', text, re.I)) and not re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b', code, re.I)
        if not read and (self.readonly or self._transaction_readonly):
            raise sqlite3.OperationalError('PLATFORM_DATABASE_READ_ONLY')
        if not read and not self._transaction:
            self._begin()
        last_id = False
        match = re.match(r'^INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\b', text, re.I)
        if match and match.group(1).lower() in SERIAL_COLUMNS and not re.search(r'\bRETURNING\b', text, re.I):
            text += ' RETURNING ' + SERIAL_COLUMNS[match.group(1).lower()]
            last_id = True
        return self._run(text, values, implicit_last_id=last_id)

    def executemany(self, sql, parameters):
        total = 0
        for values in parameters:
            cursor = self.execute(sql, values)
            total += max(0, cursor.rowcount)
        return Cursor(rowcount=total)

    def executescript(self, script):
        # sqlite3.executescript first commits an already pending transaction.
        self.commit()
        for statement in split_statements(script):
            self.execute(statement)
        return Cursor()

    def commit(self):
        self._ensure_open()
        try:
            self.raw.commit()
        except Exception as error:
            raise _error(error) from None
        finally:
            self._transaction = False
            self._transaction_readonly = False

    def rollback(self):
        self._ensure_open()
        try:
            self.raw.rollback()
        except Exception as error:
            raise _error(error) from None
        finally:
            self._transaction = False
            self._transaction_readonly = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._transaction = self._transaction_readonly = False
        try:
            if self._release is not None:
                self._release()
            else:
                self.raw.close()
        except Exception as error:
            raise _error(error) from None

    def validate_ddl(self, sql):
        alter = re.fullmatch(r'ALTER\s+TABLE\s+([a-z_][a-z0-9_]*)\s+ADD\s+COLUMN\s+(.+)', sql.strip(), re.I | re.S)
        if alter:
            table_name, definition = alter.groups()
            return self.validate_ddl('CREATE TABLE IF NOT EXISTS ' + table_name + '(' + definition + ')')
        table = re.fullmatch(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)\s*\((.*)\)', sql.strip(), re.I | re.S)
        if table:
            name, body = table.groups()
            actual = self._run("SELECT column_name,data_type,is_nullable,column_default,udt_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (name.lower(),)).fetchall()
            found = {r['column_name']: r for r in actual}
            if not found:
                raise sqlite3.OperationalError('PLATFORM_SCHEMA_MISSING')
            for definition in _columns(body):
                match = re.match(r'([a-z_][a-z0-9_]*)\s+(BIGSERIAL|BIGINT|DOUBLE\s+PRECISION|TEXT\[\]|TEXT|BYTEA)(?=\s|$)', definition, re.I)
                if not match:
                    if re.match(r'^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b', definition, re.I):
                        continue
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_DEFINITION_UNSUPPORTED')
                column, kind = match.groups()
                kind = re.sub(r'\s+', ' ', kind.upper())
                expected = {'BIGSERIAL': 'bigint', 'BIGINT': 'bigint', 'DOUBLE PRECISION': 'double precision', 'TEXT': 'text', 'TEXT[]': 'ARRAY', 'BYTEA': 'bytea'}[kind]
                value = found.get(column.lower())
                if value is None or value['data_type'] != expected:
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_TYPE_MISMATCH')
                if kind == 'TEXT[]' and value['udt_name'] != '_text':
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_TYPE_MISMATCH')
                if re.search(r'\b(NOT\s+NULL|PRIMARY\s+KEY)\b', definition, re.I) and value['is_nullable'] != 'NO':
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_NULLABILITY_MISMATCH')
                if kind == 'BIGSERIAL' and not str(value['column_default']).startswith('nextval('):
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_SEQUENCE_MISSING')
                default = re.search(r'\bDEFAULT\s+(.+)$', definition, re.I)
                if default and value['column_default'] is None:
                    raise sqlite3.OperationalError('PLATFORM_SCHEMA_DEFAULT_MISSING')
            return
        index = re.fullmatch(r'CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)\s+ON\s+([a-z_][a-z0-9_]*)\s*(?:USING\s+(GIN)\s*)?\(([^)]+)\)', sql.strip(), re.I)
        if index:
            name, table_name, method, columns = index.groups()
            row = self._run('SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename=? AND indexname=?', (table_name.lower(), name.lower())).fetchone()
            wanted = re.sub(r'\s+', '', columns).lower()
            if row is None or ('(' + wanted + ')') not in re.sub(r'[\s"]+', '', row[0]).lower() or ('usinggin' if method else 'usingbtree') not in re.sub(r'\s+', '', row[0]).lower():
                raise sqlite3.OperationalError('PLATFORM_SCHEMA_INDEX_MISMATCH')
            return
        raise sqlite3.OperationalError('PLATFORM_SCHEMA_STATEMENT_NOT_ALLOWED')

    def schema_marker(self):
        ddl = 'CREATE TABLE IF NOT EXISTS wuyu_platform_schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)'
        self.execute(ddl)
        if self.allow_schema:
            for key, value in [('schema_version', SCHEMA_VERSION), ('adapter_contract', ADAPTER_CONTRACT)]:
                self.execute('INSERT INTO wuyu_platform_schema_meta VALUES(?,?) ON CONFLICT(key) DO NOTHING', (key, value))
        found = {r['key']: r['value'] for r in self.execute('SELECT key,value FROM wuyu_platform_schema_meta').fetchall()}
        if found.get('schema_version') != SCHEMA_VERSION or found.get('adapter_contract') != ADAPTER_CONTRACT:
            raise sqlite3.OperationalError('PLATFORM_SCHEMA_VERSION_MISMATCH')
        if not self.allow_schema and found.get('schema_signature') != self.schema_signature():
            raise sqlite3.OperationalError('PLATFORM_SCHEMA_SIGNATURE_MISMATCH')

    def schema_signature(self):
        # No business values: full column/default, key/FK/check and index
        # definitions are pinned only by the explicitly privileged bootstrap.
        statements = [
            "SELECT table_name,column_name,ordinal_position,data_type,is_nullable,column_default,udt_name FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position",
            "SELECT c.relname,con.conname,con.contype,pg_get_constraintdef(con.oid) AS definition FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname,con.conname",
            "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname",
        ]
        values = [[list(row) for row in self._run(sql).fetchall()] for sql in statements]
        return hashlib.sha256(json.dumps(values, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()

    def finalize_schema(self):
        if not self.allow_schema or self.readonly:
            raise sqlite3.OperationalError('PLATFORM_SCHEMA_BOOTSTRAP_REQUIRED')
        signature = self.schema_signature()
        self.execute('INSERT INTO wuyu_platform_schema_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('schema_signature', signature))


def open_connection(url, *, readonly=False, allow_schema=False, timeout=15, lock_timeout_ms=10000, pooled=False):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('PLATFORM_DATABASE_TIMEOUT_INVALID')
    if type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 30000:
        raise ValueError('PLATFORM_DATABASE_LOCK_TIMEOUT_INVALID')
    try:
        import psycopg
    except ImportError:
        raise sqlite3.OperationalError('PLATFORM_POSTGRES_DRIVER_UNAVAILABLE') from None
    raw = None
    release = None
    try:
        if pooled and not allow_schema:
            from .pg_pool import acquire
            raw, release = acquire(url, timeout=timeout)
        else:
            raw = psycopg.connect(url, autocommit=True, connect_timeout=max(1, math.ceil(timeout)), application_name='wuyu-platform-pg111')
        if release is None:
            raw.execute('SET search_path=public')
            raw.execute("SET timezone='UTC'")
        if readonly:
            raw.execute('SET default_transaction_read_only=on')
            raw.execute('SET statement_timeout=' + str(math.ceil(timeout * 1000)))
        return Connection(raw, readonly=readonly, allow_schema=allow_schema, lock_timeout_ms=lock_timeout_ms, release=release)
    except Exception as error:
        if raw is not None:
            try:
                release() if release is not None else raw.close()
            except Exception:
                pass
        raise _error(error) from None
