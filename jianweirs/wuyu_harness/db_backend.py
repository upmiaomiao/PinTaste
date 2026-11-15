"""数据库后端：默认本机 SQLite，设置 WUYU_DATABASE_URL 后改用云 PostgreSQL。

为什么
------
SQLite 只能放在一台机器上，门户因此只能单进程。把账号、任务、配置、用量放进云 PostgreSQL，
数据才不依赖这台机器，也才有可能让多个副本共用同一份状态。

做法
----
业务代码（auth_portal / store / user_limits / request_replay / portal_config / conversation_state）
一直用 `with db.connect() as conn: conn.execute("... ?", params)` 这套写法。这里不改写这些调用，
而是提供一个"长得像 sqlite3 连接"的 PostgreSQL 连接，在执行前把少数 SQLite 写法翻译成 PostgreSQL：

* `?` 占位符 → `%s`；
* `INSERT OR IGNORE` / `INSERT OR REPLACE` → `ON CONFLICT ...`（主键信息见 TABLES）；
* `json_extract(body,'$.a.b')` → jsonb 取值（出现在 `AS` 之前时保留原类型，否则取文本）；
* `rowid` → `seq`（BIGSERIAL，更新同一行不改变它，对话历史顺序依赖这一点）；
* `BEGIN IMMEDIATE` → 事务级咨询锁（SQLite 的"写事务互斥"在 PostgreSQL 里的等价物）；
* `MAX(0, x)` → `GREATEST`；`PRAGMA` 忽略；`executescript` 逐条执行；REAL→DOUBLE PRECISION，BLOB→BYTEA。

SQLite 仍是默认值，出问题时去掉环境变量即可切回。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import zlib
from contextlib import contextmanager

try:                                   # 没有安装 psycopg 时，SQLite 模式照常工作
    import psycopg
except ImportError:                    # pragma: no cover
    psycopg = None

# 表 → (主键列, 其余列, 是否需要插入顺序列 seq)。INSERT OR REPLACE 与 rowid 的翻译依赖它。
TABLES = {
    'tasks': (('id',), ('user_id', 'body'), True),
    'scans': (('id',), ('user_id', 'body'), True),
    'records': (('id',), ('kind', 'body'), True),
    'config': (('id',), ('ciphertext',), False),
    'product_settings': (('user_id',), ('body',), False),
}

IntegrityError = (sqlite3.IntegrityError,) + ((psycopg.errors.IntegrityError,) if psycopg else ())


def database_url() -> str:
    return os.getenv('WUYU_DATABASE_URL', '').strip()


def using_postgres() -> bool:
    return bool(database_url())


class Row(tuple):
    """既能按序号也能按列名取值，等价于 sqlite3.Row；dict(row) 同样可用。"""
    __slots__ = ()
    _names: tuple = ()

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f'No item with that key: {key}') from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = tuple(column.name for column in (cursor.description or ()))
    row_type = type('Row', (Row,), {'__slots__': (), '_names': names})
    return lambda values: row_type(values)


_JSON_PATH = re.compile(r"json_extract\(\s*(\w+)\s*,\s*'\$\.([\w.]+)'\s*\)(\s+AS\b)?", re.I)
_INSERT_REPLACE = re.compile(r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s+VALUES\s*\((.*)\)\s*$', re.I | re.S)


def _json(match):
    column, path, alias = match.group(1), match.group(2).split('.'), match.group(3)
    operator = '#>' if alias else '#>>'
    return f"({column}::jsonb {operator} '{{{','.join(path)}}}'){alias or ''}"


def translate(sql: str) -> str | None:
    """SQLite 写法 → PostgreSQL。返回 None 表示这条语句在 PostgreSQL 里不需要执行。"""
    text = sql.strip().rstrip(';')
    upper = text.upper()
    if upper.startswith('PRAGMA'):
        return None
    if upper == 'BEGIN IMMEDIATE':
        return 'BEGIN IMMEDIATE'               # 由连接对象换成咨询锁
    text = _JSON_PATH.sub(_json, text)
    text = re.sub(r'\browid\b', 'seq', text)
    text = re.sub(r'\bMAX\(\s*0\s*,', 'GREATEST(0,', text)
    text = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', text)
    text = re.sub(r'\bBLOB\b', 'BYTEA', text)
    match = _INSERT_REPLACE.match(text)
    if match:
        table, values = match.group(1), match.group(2)
        keys, rest, _ = TABLES[table]
        assignments = ','.join(f'{column}=excluded.{column}' for column in rest)
        text = f"INSERT INTO {table} VALUES({values}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {assignments}"
    ignore = re.match(r'INSERT\s+OR\s+IGNORE\s+INTO', text, re.I)
    if ignore:
        text = 'INSERT INTO' + text[ignore.end():] + ' ON CONFLICT DO NOTHING'
    return text.replace('%', '%%').replace('?', '%s')


class PgConnection:
    """psycopg 连接的外壳，提供 sqlite3 连接常用的 execute / executescript。"""

    def __init__(self, raw, lock_key: int):
        self.raw, self.lock_key = raw, lock_key

    def execute(self, sql, params=()):
        statement = translate(sql)
        cursor = self.raw.cursor(row_factory=_row_factory)
        if statement is None:
            return cursor
        if statement == 'BEGIN IMMEDIATE':
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_key,))
            return cursor
        cursor.execute(statement, tuple(params))
        created = re.match(r'CREATE TABLE IF NOT EXISTS\s+(\w+)', statement, re.I)
        if created and TABLES.get(created.group(1), (0, 0, False))[2]:
            self.raw.execute(f'ALTER TABLE {created.group(1)} ADD COLUMN IF NOT EXISTS seq BIGSERIAL')
        return cursor

    def executescript(self, script):
        self.execute('BEGIN IMMEDIATE')            # 多副本同时建表时互斥
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)


class PgPool:
    """每线程一个长期复用的 PostgreSQL 连接，约定与 ThreadLocalSQLite 相同：
    正常退出提交，异常回滚，嵌套使用复用外层连接；连接被云端断开时自动重连一次。"""

    def __init__(self, url: str | None = None, *, schema: str | None = None, connect_timeout: float = 10.0):
        if psycopg is None:
            raise RuntimeError('已设置 WUYU_DATABASE_URL，但未安装 psycopg（pip install "psycopg[binary]"）')
        self.url = url or database_url()
        self.schema = schema or os.getenv('WUYU_DATABASE_SCHEMA', 'public')
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', self.schema):
            raise ValueError('WUYU_DATABASE_SCHEMA 只能用小写字母、数字和下划线')
        self.connect_timeout = connect_timeout
        self.lock_key = zlib.crc32(self.schema.encode()) + 7_000_000_000
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()
        self._prepare_schema()

    def _open(self):
        raw = psycopg.connect(self.url, connect_timeout=self.connect_timeout, options=f'-c search_path={self.schema}')
        raw.autocommit = False
        return raw

    def _prepare_schema(self):
        raw = psycopg.connect(self.url, connect_timeout=self.connect_timeout, autocommit=True)
        try:
            raw.execute(f'CREATE SCHEMA IF NOT EXISTS {self.schema}')
        finally:
            raw.close()

    def _connection(self) -> PgConnection:
        wrapped = getattr(self._local, 'connection', None)
        if wrapped is not None and (wrapped.raw.closed or wrapped.raw.broken):
            wrapped = None
        if wrapped is None:
            try:
                raw = self._open()
            except psycopg.OperationalError:
                raw = self._open()
            wrapped = PgConnection(raw, self.lock_key)
            self._local.connection, self._local.depth = wrapped, 0
            with self._lock:
                self._all.append(raw)
        return wrapped

    @contextmanager
    def connect(self):
        wrapped = self._connection()
        if getattr(self._local, 'depth', 0):
            self._local.depth += 1
            try:
                yield wrapped
            finally:
                self._local.depth -= 1
            return
        self._local.depth = 1
        try:
            try:
                yield wrapped
                wrapped.raw.commit()
            except BaseException:
                if not wrapped.raw.closed:
                    wrapped.raw.rollback()
                raise
        finally:
            self._local.depth = 0

    def close_all(self) -> None:
        with self._lock:
            connections, self._all = list(self._all), []
        for raw in connections:
            try:
                raw.close()
            except Exception:
                pass
        self._local = threading.local()

    def drop_schema(self):
        """只给测试用：删掉整个 schema。"""
        self.close_all()
        raw = psycopg.connect(self.url, autocommit=True)
        try:
            raw.execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')
        finally:
            raw.close()


def open_pool(path, *, timeout: float = 15.0, row_factory=None, persistent: bool | None = None):
    """按环境选择后端：设置了 WUYU_DATABASE_URL 用 PostgreSQL，否则用 path 处的 SQLite 文件。
    persistent：SQLite 是否长期复用连接；None 表示按环境默认（WUYU_SQLITE_POOL）。"""
    if using_postgres():
        return PgPool()
    from .sqlite_pool import ThreadLocalSQLite, pooling_enabled
    keep = pooling_enabled(True) if persistent is None else (persistent and pooling_enabled(True))
    return ThreadLocalSQLite(path, timeout=timeout, row_factory=row_factory, persistent=keep)
