"""Bounded synchronous database work without blocking an asyncio event loop."""
import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import sqlite3
import threading

MAX_WORKERS = 4
MAX_PENDING = 32
_lock = threading.Lock()
_executor = None
_pending = 0
_accepting = True


async def _run(function, args, kwargs, complete=None):
    global _executor, _pending
    with _lock:
        if not _accepting or _pending >= MAX_PENDING:
            raise sqlite3.OperationalError('PLATFORM_DATABASE_UNAVAILABLE')
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='wuyu-db')
        executor = _executor
        _pending += 1
    try:
        context = contextvars.copy_context()
        future = asyncio.get_running_loop().run_in_executor(executor, context.run, partial(function, *args, **kwargs))
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                if future.done():
                    try:
                        result = future.result()
                    except BaseException:
                        raise cancelled
                    break
            except BaseException:
                if cancelled is not None:
                    raise cancelled
                raise
        # This synchronous callback runs on the caller's event loop, even when
        # cancellation arrived after the DB operation committed. It never awaits.
        if complete is not None:
            try:
                complete(result)
            except BaseException:
                if cancelled is not None:
                    raise cancelled
                raise
        if cancelled is not None:
            raise cancelled
        return result
    finally:
        with _lock:
            _pending -= 1


async def run_sync(function, *args, **kwargs):
    return await _run(function, args, kwargs)


async def run_sync_complete(function, complete, *args, **kwargs):
    return await _run(function, args, kwargs, complete)


async def close():
    global _executor, _accepting
    with _lock:
        _accepting = False
        executor = _executor
    cancelled = None
    if executor is not None:
        closing = asyncio.create_task(asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=False))
        while True:
            try:
                await asyncio.shield(closing)
                break
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                if closing.done():
                    closing.result()
                    break
    with _lock:
        _executor = None
    if cancelled is not None:
        raise cancelled


def start():
    global _accepting
    with _lock:
        if _pending:
            raise sqlite3.OperationalError('PLATFORM_DATABASE_UNAVAILABLE')
        _accepting = True


def stats():
    with _lock:
        return {'max_workers': MAX_WORKERS, 'max_pending': MAX_PENDING, 'pending': _pending}
