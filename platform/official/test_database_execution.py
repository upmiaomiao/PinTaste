"""No paid/network calls: bounded work, cancellation and actual worker ownership."""
import asyncio
import contextvars
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from . import db_execution
from . import jobs
from . import store as db
from . import test_failure_accounting as accounting_cases


async def signalled(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(.001)


class DatabaseExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db_execution.start()

    async def asyncTearDown(self):
        await db_execution.close()
        db_execution.start()

    async def test_context_and_completion_stay_on_their_threads(self):
        owner = threading.get_ident()
        variable = contextvars.ContextVar('synthetic_scope', default=None)
        variable.set('account-A')
        seen = []
        result = await db_execution.run_sync_complete(
            lambda: (threading.get_ident(), variable.get()),
            lambda value: seen.append((threading.get_ident(), value)))
        self.assertNotEqual(result[0], owner)
        self.assertEqual(result[1], 'account-A')
        self.assertEqual(seen, [(owner, result)])

    async def test_cancellation_drains_commit_and_records_completion_first(self):
        entered, release = threading.Event(), threading.Event()
        receipt = []
        def committed():
            entered.set(); release.wait(3)
            return 17
        task = asyncio.create_task(db_execution.run_sync_complete(committed, receipt.append))
        await signalled(entered)
        task.cancel(); await asyncio.sleep(.005); task.cancel()
        self.assertFalse(task.done()); self.assertEqual(receipt, [])
        self.assertEqual(db_execution.stats()['pending'], 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(receipt, [17]); self.assertEqual(db_execution.stats()['pending'], 0)

    async def test_cancelled_failure_never_runs_success_callback(self):
        entered, release = threading.Event(), threading.Event()
        receipt = []
        def failed():
            entered.set(); release.wait(3)
            raise RuntimeError('synthetic private message')
        task = asyncio.create_task(db_execution.run_sync_complete(failed, receipt.append))
        await signalled(entered); task.cancel(); release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(receipt, [])

    async def test_cancelled_callback_failure_preserves_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def committed(): entered.set(); release.wait(3)
        def complete(_): raise RuntimeError('synthetic callback failure')
        task = asyncio.create_task(db_execution.run_sync_complete(committed, complete))
        await signalled(entered); task.cancel(); release.set()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_running_and_waiting_work_is_bounded_without_blocking_loop(self):
        release = threading.Event(); lock = threading.Lock(); running = 0; peak = 0
        def blocked():
            nonlocal running, peak
            with lock:
                running += 1; peak = max(peak, running)
            release.wait(3)
            with lock: running -= 1
        tasks = [asyncio.create_task(db_execution.run_sync(blocked)) for _ in range(db_execution.MAX_PENDING)]
        try:
            await asyncio.sleep(.03)
            self.assertEqual(db_execution.stats()['pending'], db_execution.MAX_PENDING)
            with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_DATABASE_UNAVAILABLE$'):
                await db_execution.run_sync(lambda: None)
            self.assertEqual(peak, db_execution.MAX_WORKERS)
        finally:
            release.set(); await asyncio.gather(*tasks)

    async def test_shutdown_drains_and_rejects_new_work_then_explicit_start(self):
        entered, release = threading.Event(), threading.Event()
        def work(): entered.set(); release.wait(3); return 9
        task = asyncio.create_task(db_execution.run_sync(work)); await signalled(entered)
        closing = asyncio.create_task(db_execution.close()); await asyncio.sleep(.005)
        with self.assertRaisesRegex(sqlite3.OperationalError, '^PLATFORM_DATABASE_UNAVAILABLE$'):
            await db_execution.run_sync(lambda: None)
        self.assertFalse(closing.done()); release.set()
        self.assertEqual(await task, 9); await closing
        db_execution.start(); self.assertEqual(await db_execution.run_sync(lambda: 3), 3)


class WorkerThreadBoundaryTests(accounting_cases.FailureAccountingTests):
    # Use the real transaction/worker implementation and a disposable local DB.
    async def asyncSetUp(self):
        db_execution.start()

    async def asyncTearDown(self):
        await db_execution.close()
        db_execution.start()

    def queued(self):
        row = self.row()
        db.execute("UPDATE jobs SET status='queued' WHERE id=?", (row['id'],))
        return db.query('SELECT * FROM jobs WHERE id=?', (row['id'],), one=True)

    async def test_cancel_during_read_never_claims(self):
        self.queued(); entered, release = threading.Event(), threading.Event()
        original = db.settings
        def settings(): entered.set(); release.wait(3); return original()
        scheduler = jobs.Scheduler(None)
        with patch.object(db, 'settings', settings):
            task = asyncio.create_task(scheduler._dispatch_once_async())
            await signalled(entered); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(db.query("SELECT status FROM jobs WHERE id='job'", one=True)['status'], 'queued')
        self.assertEqual(scheduler.tasks, {})

    async def test_stop_drains_committed_claim_registers_then_stops_owner(self):
        self.queued(); entered, release = threading.Event(), threading.Event(); calls = []
        original = db.execute
        def execute(sql, *args, **kwargs):
            value = original(sql, *args, **kwargs)
            if "SET status='running',started=" in sql:
                entered.set(); release.wait(3)
            return value
        async def runner(row, output):
            calls.append(row['id']); yield {'type': 'done', 'complete': True, 'usage': {'total_tokens': 17}}
        scheduler = jobs.Scheduler(runner)
        with patch.object(db, 'execute', execute):
            scheduler.loop = asyncio.create_task(scheduler.dispatch())
            await signalled(entered)
            stopping = asyncio.create_task(scheduler.stop())
            await asyncio.sleep(.005); self.assertFalse(stopping.done())
            release.set(); await stopping
        row = db.query("SELECT * FROM jobs WHERE id='job'", one=True)
        self.assertEqual(calls, [])
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['charged'], row['reserved'])
        self.assertEqual(scheduler.tasks, {}); self.assertIsNone(scheduler._pending_claim)

    async def test_lost_claim_receipt_and_cancel_keeps_proof_never_replays_unknown(self):
        self.queued(); entered, release = threading.Event(), threading.Event()
        original = db.execute; launches = []
        def execute(sql, *args, **kwargs):
            value = original(sql, *args, **kwargs)
            if "SET status='running',started=" in sql:
                entered.set(); release.wait(3); raise RuntimeError('synthetic lost commit receipt')
            return value
        scheduler = jobs.Scheduler(None)
        with patch.object(db, 'execute', execute), patch.object(scheduler, '_launch', side_effect=lambda *args: launches.append(args)):
            task = asyncio.create_task(scheduler._dispatch_once_async())
            await signalled(entered); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(scheduler._pending_claim, ('job', False)); self.assertEqual(launches, [])
        stranger = jobs.Scheduler(None)
        with patch.object(stranger, '_launch', side_effect=lambda *args: launches.append(args)):
            self.assertFalse(await stranger._dispatch_once_async())
        self.assertEqual(launches, [])

    async def test_cancel_during_stream_flush_drains_before_terminal_flush(self):
        row = self.row(); entered, release = threading.Event(), threading.Event()
        original = db.connect; count = 0
        import contextlib
        @contextlib.contextmanager
        def connect(*args, **kwargs):
            nonlocal count
            with original(*args, **kwargs) as connection:
                yield connection
            # First connection is sequence lookup, second settings, third flush.
            count += 1
            if count == 3:
                entered.set(); release.wait(3)
        async def runner(row, output):
            yield {'type': 'content', 'text': 'synthetic partial'}
            await asyncio.sleep(30)
        scheduler = jobs.Scheduler(runner)
        with patch.object(db, 'connect', connect):
            task = asyncio.create_task(scheduler.run(row, False))
            await signalled(entered); task.cancel(); await asyncio.sleep(.005)
            self.assertFalse(task.done()); release.set(); await task
        saved = db.query("SELECT * FROM jobs WHERE id='job'", one=True)
        self.assertEqual(saved['status'], 'stopped'); self.assertEqual(saved['charged'], row['reserved'])
        self.assertEqual(json.loads(saved['turn'])['answer'], 'synthetic partial')
        events = db.query("SELECT sequence,event FROM job_events WHERE job_id='job' ORDER BY sequence")
        self.assertEqual([r['sequence'] for r in events], list(range(1, len(events)+1)))
        self.assertEqual([json.loads(r['event'])['type'] for r in events], ['execution_start', 'content', 'execution_end'])


if __name__ == '__main__': unittest.main()
