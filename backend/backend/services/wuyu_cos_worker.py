"""One isolated, budgeted COS copy consumer. No Flask app, task execution or DDL."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import time
from types import SimpleNamespace

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from services.wuyu_cos_artifacts import ArtifactStore, StorageError, NEW_EXPORT_KINDS, _timestamp


def read_json(path, limit=16384):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > limit:
        raise StorageError('COS_WORKER_STATE_INVALID')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise StorageError('COS_WORKER_STATE_INVALID') from None


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(path, *, blocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


class Budget:
    def __init__(self, directory, policy):
        self.directory, self.policy = Path(directory), policy
        fields = {'enabled', 'expires_at', 'daily_attempts', 'daily_bytes', 'total_attempts', 'total_bytes'}
        if not isinstance(policy, dict) or set(policy) != fields or type(policy['enabled']) is not bool:
            raise StorageError('COS_WORKER_POLICY_INVALID')
        limits = {'daily_attempts': 20, 'daily_bytes': 64 * 1024**2,
                  'total_attempts': 80, 'total_bytes': 256 * 1024**2}
        if any(type(policy[k]) is not int or not 1 <= policy[k] <= cap for k, cap in limits.items()):
            raise StorageError('COS_WORKER_POLICY_INVALID')
        if (type(policy['expires_at']) not in (int, float) or not 0 < policy['expires_at'] <= time.time() + 8 * 86400):
            raise StorageError('COS_WORKER_POLICY_INVALID')

    def reserve(self, size):
        if (not self.policy['enabled'] or time.time() >= self.policy['expires_at']
                or type(size) is not int or not 1 <= size <= 32 * 1024**2):
            return False
        with locked(self.directory / 'budget.lock'):
            today = datetime.now(timezone.utc).date().isoformat()
            path = self.directory / 'budget.json'
            state = read_json(path) if path.exists() or path.is_symlink() else {
                'day': today, 'daily_attempts': 0, 'daily_bytes': 0, 'total_attempts': 0, 'total_bytes': 0}
            if (not isinstance(state, dict) or set(state) != {'day', 'daily_attempts', 'daily_bytes', 'total_attempts', 'total_bytes'}
                    or not isinstance(state['day'], str) or any(type(state[k]) is not int or state[k] < 0
                        for k in ['daily_attempts', 'daily_bytes', 'total_attempts', 'total_bytes'])):
                raise StorageError('COS_WORKER_STATE_INVALID')
            if state['day'] != today:
                state.update(day=today, daily_attempts=0, daily_bytes=0)
            if any(state[k] + increment > self.policy[k] for k, increment in
                    [('daily_attempts', 1), ('daily_bytes', size), ('total_attempts', 1), ('total_bytes', size)]):
                return False
            for k, increment in [('daily_attempts', 1), ('daily_bytes', size), ('total_attempts', 1), ('total_bytes', size)]:
                state[k] += increment
            atomic_json(path, state)
            return True


def _sqlite_new_exports(database, cutoff):
    """Project-owned artifact metadata only; no material or conversation bodies."""
    minimum = _timestamp(cutoff)
    if minimum is None:
        raise StorageError('COS_CONFIG_INVALID')
    database = Path(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON')
        fields = ['id', 'kind', 'relative_path', 'bytes', 'sha256', 'created_at']
        projection = ','.join("'" + f + "',json_extract(a.value,'$." + f + "')" for f in fields)
        marks = ','.join('?' for _ in NEW_EXPORT_KINDS)
        sql = ("SELECT w.project_id,json_object(" + projection + ") FROM wuyu_workflows w "
            "JOIN projects p ON p.id=w.project_id,json_each(w.body,'$.artifacts') a "
            "WHERE p.user_id IS NOT NULL AND json_extract(a.value,'$.kind') IN (" + marks + ") "
            "AND julianday(json_extract(a.value,'$.created_at'))>=julianday(?) ORDER BY "
            "json_extract(a.value,'$.created_at') LIMIT 10001")
        rows = db.execute(sql, [*NEW_EXPORT_KINDS, cutoff]).fetchall()
        if len(rows) > 10000:
            raise StorageError('COS_SCAN_CAPACITY')
        for project, raw in rows:
            item = json.loads(raw)
            created = _timestamp(item.get('created_at'))
            if created is not None and minimum <= created <= time.time() + 120:
                yield project, item


def _postgres_url(value):
    """A private deployment-only URL; no value is ever included in errors/logs."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 8192
                or value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError
        parsed = make_url(value)
        if (parsed.drivername not in {'postgresql', 'postgresql+psycopg'}
                or not parsed.username or not parsed.host or not parsed.database
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or any(str(k).lower() == 'plugin' for k in parsed.query)):
            raise ValueError
        # Use the explicitly tested driver, including legacy postgresql:// input.
        return parsed.set(drivername='postgresql+psycopg')
    except (ValueError, TypeError, AttributeError, SQLAlchemyError):
        raise StorageError('COS_DATABASE_CONFIG_INVALID') from None


def database_source():
    """A dedicated read-only role file, independent of API secrets/COS budget."""
    expected = os.environ.get('WUYU_COS_DATABASE_BACKEND')
    if expected not in (None, 'sqlite', 'postgresql'):
        raise StorageError('COS_DATABASE_CONFIG_INVALID')
    path = Path('/run/wuyu-cos/database.json')
    if not path.exists() and not path.is_symlink():
        if expected == 'postgresql':
            raise StorageError('COS_DATABASE_CONFIG_INVALID')
        return '/cos-db/database.db'
    if expected == 'sqlite':
        raise StorageError('COS_DATABASE_CONFIG_INVALID')
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != {'url'}:
        raise StorageError('COS_DATABASE_CONFIG_INVALID')
    _postgres_url(value['url'])
    return value['url']


def _postgres_new_exports(database, cutoff):
    minimum = _timestamp(cutoff)
    if minimum is None:
        raise StorageError('COS_CONFIG_INVALID')
    url = _postgres_url(database)
    engine = None
    try:
        engine = create_engine(url, connect_args={'connect_timeout': 3},
            pool_pre_ping=True, pool_size=1, max_overflow=0, pool_timeout=2,
            isolation_level='READ COMMITTED', hide_parameters=True)
        fields = ['id', 'kind', 'relative_path', 'bytes', 'sha256', 'created_at']
        projection = ','.join("'" + f + "',a.value->'" + f + "'" for f in fields)
        query = text("SELECT w.project_id,jsonb_build_object(" + projection + ") AS metadata "
            "FROM wuyu_workflows w JOIN projects p ON p.id=w.project_id "
            "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(CAST(w.body AS jsonb)->'artifacts')='array' THEN "
            "CAST(w.body AS jsonb)->'artifacts' ELSE '[]'::jsonb END) a(value) "
            "WHERE p.user_id IS NOT NULL AND a.value->>'kind' IN :kinds AND "
            "CASE WHEN pg_input_is_valid(a.value->>'created_at','timestamp with time zone') "
            "THEN CAST(a.value->>'created_at' AS timestamp with time zone) ELSE NULL END "
            ">=CAST(:cutoff AS timestamp with time zone) "
            "ORDER BY a.value->>'created_at' LIMIT 10001").bindparams(bindparam('kinds', expanding=True))
        with engine.connect() as connection:
            with connection.begin():
                if not connection.dialect.server_version_info or connection.dialect.server_version_info < (16,):
                    raise StorageError('COS_DATABASE_VERSION_UNSUPPORTED')
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5000ms'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
                rows = list(connection.execute(query, {'kinds': sorted(NEW_EXPORT_KINDS), 'cutoff': cutoff}).mappings())
        if len(rows) > 10000:
            raise StorageError('COS_SCAN_CAPACITY')
        for row in rows:
            item = row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])
            created = _timestamp(item.get('created_at'))
            if created is not None and minimum <= created <= time.time() + 120:
                yield row['project_id'], item
    except StorageError:
        raise
    except (SQLAlchemyError, ImportError, TypeError, ValueError):
        raise StorageError('COS_DATABASE_UNAVAILABLE') from None
    finally:
        if engine is not None:
            engine.dispose()


def new_exports(database, cutoff):
    """Project-owned metadata from SQLite or an explicitly configured PG16+ RO role."""
    if isinstance(database, str) and database.startswith(('postgresql://', 'postgresql+psycopg://')):
        yield from _postgres_new_exports(database, cutoff)
    else:
        if not isinstance(database, (str, Path)) or '://' in str(database):
            raise StorageError('COS_DATABASE_CONFIG_INVALID')
        yield from _sqlite_new_exports(database, cutoff)


def sweep(store, database, budget):
    if store is None or store.settings.not_before is None:
        return {'state': 'paused', 'copied': 0}
    copied, rejected = 0, 0
    for project, item in new_exports(database, store.settings.not_before):
        if not store.eligible(project, item):
            rejected += 1
            continue
        try:
            lifecycle = store.lifecycle()
            lifecycle.enqueue(project, item)
            before = lifecycle.inspect(project, item)['attempts']
            if lifecycle.process_one(project, item, allow_cloud=True, before_attempt=budget.reserve):
                copied += 1
            # At most one cloud attempt per sweep, even if it failed.
            if lifecycle.inspect(project, item)['attempts'] > before or copied:
                break
        except StorageError:
            rejected += 1
    return {'state': 'ready', 'copied': copied, 'rejected': rejected}


def main():
    os.umask(0o077)
    root = Path('/app/uploads')
    control = root / '.cos-worker'
    if not root.is_dir() or root.is_symlink() or control.is_symlink():
        raise StorageError('COS_PATH_INVALID')
    control.mkdir(mode=0o700, exist_ok=True)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with locked(control / 'worker.lock', blocking=False):
        while running:
            try:
                policy = read_json('/run/wuyu-cos/worker.json')
                budget = Budget(control, policy)
                if not policy['enabled'] or time.time() >= policy['expires_at']:
                    status = {'state': 'paused', 'copied': 0}
                else:
                    store = ArtifactStore.from_app(SimpleNamespace(config={'UPLOAD_FOLDER': str(root)}))
                    status = sweep(store, database_source(), budget)
            except (StorageError, OSError, sqlite3.Error, ValueError):
                status = {'state': 'error', 'copied': 0}
            atomic_json(control / 'status.json', dict(status, heartbeat=time.time()))
            for _ in range(10):
                if not running:
                    break
                time.sleep(1)


if __name__ == '__main__':
    main()
