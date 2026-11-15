"""Bounded application PG connections; bootstrap stays on direct connections.

Only psycopg_pool owns connection creation/replacement/queueing. This module
sets the application's session/reset contract and never retries business SQL.
"""
import logging
import os
import threading

MAX_SIZE = 4
MAX_WAITING = 32
_lock = threading.Lock()
_pool = None
_url = None
_pid = None


class _SafePoolLog(logging.Filter):
    def filter(self, record):
        # Third-party connection errors can include DSNs/driver messages. Keep
        # severity and a fixed event only; no arbitrary args/traceback or body.
        record.msg = 'PostgreSQL pool maintenance event'
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def _configure(raw):
    raw.execute('SET search_path=public')
    raw.execute("SET timezone='UTC'")
    raw.execute('SET default_transaction_read_only=off')
    raw.execute('SET statement_timeout=0')


def _reset(raw):
    # The library calls reset only after ending any outstanding transaction.
    # RESET ALL does not discard prepared statements tracked by psycopg.
    raw.execute('RESET ALL')
    _configure(raw)


def _get_pool(url):
    global _pool, _url, _pid
    with _lock:
        if _pool is not None:
            if _pid != os.getpid() or _url != url:
                # Do not silently reuse a forked pool or grow another URL pool
                # beyond this process's configured connection budget.
                raise RuntimeError('PLATFORM_DATABASE_POOL_RESTART_REQUIRED')
            return _pool
        from psycopg_pool import ConnectionPool
        logger = logging.getLogger('psycopg.pool')
        if not any(isinstance(value, _SafePoolLog) for value in logger.filters):
            logger.addFilter(_SafePoolLog())
        pool = ConnectionPool(url, kwargs={
            'autocommit': True, 'connect_timeout': 15,
            'application_name': 'wuyu-platform-pg111',
        }, min_size=0, max_size=MAX_SIZE, max_waiting=MAX_WAITING,
            timeout=15, max_idle=60, max_lifetime=300, reconnect_timeout=15,
            num_workers=1, configure=_configure, reset=_reset,
            check=ConnectionPool.check_connection, name='wuyu-platform', open=False)
        pool.open(wait=False)
        _pool, _url, _pid = pool, url, os.getpid()
        return pool


def acquire(url, *, timeout):
    pool = _get_pool(url)
    raw = pool.getconn(timeout=timeout)
    returned = False

    def release():
        nonlocal returned
        if returned:
            return
        returned = True
        # putconn rolls back unfinished transactions, rejects broken sessions,
        # runs reset, and only then makes the connection available to a waiter.
        pool.putconn(raw)

    return raw, release


def close():
    global _pool, _url, _pid
    with _lock:
        pool = _pool
        if pool is not None:
            # Serialize replacement with close: never create a second live pool
            # while the previous process budget is still being drained.
            pool.close(timeout=5)
        _pool = _url = _pid = None


def stats():
    with _lock:
        pool = _pool
    # Pool statistics contain only counts/timings, never connection info.
    return {'max_size': MAX_SIZE, 'max_waiting': MAX_WAITING,
            'statistics': pool.get_stats() if pool is not None else {}}
