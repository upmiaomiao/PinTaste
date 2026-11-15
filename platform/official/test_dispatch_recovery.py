"""Dispatch faults with synthetic runner; repeat the same contracts on real PG."""
import asyncio
import json
import unittest
from unittest.mock import patch

from official import jobs
from official import store as db
from official import memory
from official import test_failure_accounting as accounting_fixture
from official.test_pg_worker_regressions import _PostgresFixtureMixin


class DispatchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        accounting_fixture.FailureAccountingTests.setUp(self)
        delay = patch.object(jobs, 'DISPATCH_RETRY_SECONDS', .005)
        delay.start();self.addCleanup(delay.stop)
        index = patch.object(memory, 'after_completion', lambda row: None)
        index.start();self.addCleanup(index.stop)
        self.calls = []

    tearDown = accounting_fixture.FailureAccountingTests.tearDown
    row = accounting_fixture.FailureAccountingTests.row

    def make_job(self):
        row = self.row();turn = json.loads(row['turn']);turn['status'] = 'queued'
        db.execute("UPDATE jobs SET status='queued',turn=? WHERE id=?", (json.dumps(turn), row['id']))
        return db.query('SELECT * FROM jobs WHERE id=?', (row['id'],), one=True)

    async def runner(self, row, output):
        self.calls.append(row['id'])
        yield {'type': 'done', 'complete': True, 'usage': {'total_tokens': 17}}

    async def wait_terminal(self):
        for _ in range(150):
            row = db.query("SELECT * FROM jobs WHERE id='job'", one=True)
            if row['status'] in jobs.TERMINAL:
                return row
            await asyncio.sleep(.01)
        self.fail('Synthetic job did not complete')

    async def drive(self, scheduler):
        await scheduler.start()
        try:
            return await self.wait_terminal()
        finally:
            await scheduler.stop()

    async def test_transient_settings_read_does_not_kill_dispatcher(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);original = db.settings;attempts = []
        def settings():
            attempts.append(1)
            if len(attempts) == 1:raise RuntimeError('secret fixture must never be logged')
            return original()
        with patch.object(db, 'settings', settings), self.assertLogs(jobs.__name__, level='WARNING') as logs:
            row = await self.drive(scheduler)
        self.assertEqual((row['status'], row['charged']), ('complete', 17))
        self.assertEqual(self.calls, ['job']);self.assertNotIn('secret fixture', '\n'.join(logs.output))

    async def test_transient_queue_read_keeps_other_execution_alive(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);original = db.query;failed = []
        blocker = asyncio.create_task(asyncio.sleep(30));scheduler.tasks['other-self-created'] = blocker
        def query(sql, *args, **kwargs):
            if "status='queued'" in sql and not failed:
                failed.append(True);raise RuntimeError('synthetic read failure')
            return original(sql, *args, **kwargs)
        with patch.object(db, 'query', query):
            await scheduler.start()
            try:
                row = await self.wait_terminal();self.assertFalse(blocker.cancelled());self.assertFalse(blocker.done())
            finally:await scheduler.stop()
        self.assertEqual(row['status'], 'complete');self.assertEqual(self.calls, ['job'])

    async def test_claim_failure_before_commit_is_retried_without_duplicate_dispatch(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);original = db.execute;failed = []
        def execute(sql, *args, **kwargs):
            if "SET status='running',started=" in sql and not failed:
                failed.append(True);raise RuntimeError('synthetic pre-commit claim failure')
            return original(sql, *args, **kwargs)
        with patch.object(db, 'execute', execute):row = await self.drive(scheduler)
        self.assertEqual(self.calls, ['job']);self.assertEqual(row['charged'], 17)

    async def test_claim_failure_after_commit_is_reconciled_once(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);original = db.execute;failed = []
        def execute(sql, *args, **kwargs):
            value = original(sql, *args, **kwargs)
            if "SET status='running',started=" in sql and not failed:
                failed.append(True);raise RuntimeError('synthetic lost successful claim receipt')
            return value
        with patch.object(db, 'execute', execute):row = await self.drive(scheduler)
        self.assertEqual(self.calls, ['job']);self.assertEqual(row['charged'], 17)
        self.assertIsNone(scheduler._pending_claim)

    async def test_cancel_after_unacknowledged_claim_does_not_dispatch_or_charge(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);original = db.execute;failed = []
        def execute(sql, *args, **kwargs):
            value = original(sql, *args, **kwargs)
            if "SET status='running',started=" in sql and not failed:
                failed.append(True)
                original("UPDATE jobs SET input=? WHERE id='job'", (json.dumps({'_cancel_requested': True}),))
                raise RuntimeError('synthetic lost claim receipt followed by cancel')
            return value
        with patch.object(db, 'execute', execute):row = await self.drive(scheduler)
        self.assertEqual((row['status'], row['charged']), ('stopped', 0));self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(row['turn'])['accounting']['basis'], 'cancelled_before_dispatch')

    async def test_running_job_without_local_claim_proof_is_never_replayed(self):
        self.make_job();db.execute("UPDATE jobs SET status='running' WHERE id='job'")
        scheduler = jobs.Scheduler(self.runner)
        scheduler._dispatch_once()
        await asyncio.sleep(0)
        self.assertEqual(self.calls, []);self.assertEqual(scheduler.tasks, {})

    async def test_terminal_claim_is_left_untouched(self):
        self.make_job();db.execute("UPDATE jobs SET status='stopped',charged=0 WHERE id='job'")
        scheduler = jobs.Scheduler(self.runner);scheduler._pending_claim = ('job', False)
        self.assertFalse(scheduler._dispatch_once());self.assertIsNone(scheduler._pending_claim)
        self.assertEqual(self.calls, [])

    async def test_registered_pending_task_is_not_started_twice(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner);scheduler._pending_claim = ('job', False)
        blocker = asyncio.create_task(asyncio.sleep(30));scheduler.tasks['job'] = blocker
        try:
            self.assertTrue(scheduler._dispatch_once());self.assertIsNone(scheduler._pending_claim)
            self.assertIs(scheduler.tasks['job'], blocker);self.assertEqual(self.calls, [])
        finally:
            blocker.cancel();await asyncio.gather(blocker, return_exceptions=True)

    async def test_failed_claim_CAS_does_not_dispatch(self):
        self.make_job();scheduler = jobs.Scheduler(self.runner)
        with patch.object(db, 'execute', return_value=0):scheduler._dispatch_once()
        self.assertEqual(self.calls, []);self.assertEqual(scheduler.tasks, {});self.assertIsNone(scheduler._pending_claim)

    async def test_stop_cancels_backoff_promptly(self):
        scheduler = jobs.Scheduler(self.runner)
        with patch.object(db, 'settings', side_effect=RuntimeError('self-created outage')):
            scheduler.loop = asyncio.create_task(scheduler.dispatch());await asyncio.sleep(.001)
            await asyncio.wait_for(scheduler.stop(), timeout=1)
        self.assertTrue(scheduler.loop.done());self.assertEqual(self.calls, [])

    async def test_repeated_poll_failure_is_logged_with_backoff(self):
        scheduler = jobs.Scheduler(self.runner)
        with patch.object(db, 'settings', side_effect=RuntimeError('do not print my private fixture')), self.assertLogs(jobs.__name__, level='WARNING') as logs:
            scheduler.loop = asyncio.create_task(scheduler.dispatch());await asyncio.sleep(.03)
            await scheduler.stop()
        self.assertEqual(len(logs.output), 1);self.assertNotIn('private fixture', logs.output[0])

    async def test_run_error_logs_coordinates_and_preserves_existing_accounting(self):
        row = self.row()
        async def fail(row, output):
            raise RuntimeError('never print material or bearer fixture')
            yield
        with self.assertLogs(jobs.__name__, level='WARNING') as logs:
            await jobs.Scheduler(fail).run(row, False)
        saved = db.query("SELECT * FROM jobs WHERE id='job'", one=True)
        self.assertEqual(saved['status'], 'error');self.assertEqual(saved['charged'], 1040000)
        text = '\n'.join(logs.output);self.assertIn('job=job', text);self.assertIn('phase=run', text)
        self.assertIn('RuntimeError', text);self.assertNotIn('bearer fixture', text)

    async def test_task_callback_retrieves_exception_without_paid_replay(self):
        scheduler = jobs.Scheduler(self.runner)
        async def fail():raise RuntimeError('private fixture error')
        task = asyncio.create_task(fail());await asyncio.sleep(0);scheduler.tasks['job'] = task
        with self.assertLogs(jobs.__name__, level='WARNING') as logs:scheduler._task_finished('job', task)
        self.assertNotIn('job', scheduler.tasks);self.assertEqual(self.calls, [])
        self.assertIn('phase=task_persistence', logs.output[0]);self.assertNotIn('private fixture', logs.output[0])

    async def test_unsafe_job_identifier_is_not_written_to_logs(self):
        with self.assertLogs(jobs.__name__, level='WARNING') as logs:
            jobs.log_safe_failure('run', RuntimeError('hidden fixture'), 'bad\nBearer private-fixture')
        self.assertIn('job=unavailable', logs.output[0]);self.assertNotIn('private-fixture', logs.output[0])

    async def test_paid_evidence_prevents_zero_cost_cancellation(self):
        row = self.row();turn = json.loads(row['turn']);turn['reasoning'] = 'existing paid fixture reasoning'
        db.execute("UPDATE jobs SET input=?,turn=? WHERE id='job'", (json.dumps({'_cancel_requested': True}), json.dumps(turn)))
        scheduler = jobs.Scheduler(self.runner)
        with self.assertRaises(AssertionError):scheduler._cancel_unstarted_claim(row)
        saved = db.query("SELECT * FROM jobs WHERE id='job'", one=True)
        self.assertEqual(saved['status'], 'running');self.assertEqual(json.loads(saved['turn'])['reasoning'], turn['reasoning'])

    async def test_failed_zero_cost_cancel_commit_keeps_claim_proof_for_reconciliation(self):
        self.make_job();db.execute("UPDATE jobs SET status='running',input=? WHERE id='job'", (json.dumps({'_cancel_requested': True}),))
        scheduler = jobs.Scheduler(self.runner);scheduler._pending_claim = ('job', False)
        with patch.object(scheduler, '_cancel_unstarted_claim', side_effect=RuntimeError('synthetic cancel write failure')):
            with self.assertRaises(RuntimeError):scheduler._dispatch_once()
        self.assertEqual(scheduler._pending_claim, ('job', False));self.assertEqual(self.calls, [])
        self.assertTrue(scheduler._dispatch_once());self.assertIsNone(scheduler._pending_claim)
        self.assertEqual(db.query("SELECT status FROM jobs WHERE id='job'", one=True)['status'], 'stopped')


class PGDispatchRecoveryTests(_PostgresFixtureMixin, DispatchRecoveryTests):
    pass


if __name__ == '__main__':unittest.main()
