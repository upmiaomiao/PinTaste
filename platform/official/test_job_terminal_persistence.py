"""Terminal persistence against disposable SQLite and synthetic execution only."""
import asyncio
import contextlib
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from official import jobs, store as db, db_execution, chat_worker


class DisposableDatabase:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wuyu-terminal157-')
        self.patches = [patch.object(db, 'DATA', Path(self.temp.name)), patch.object(db, 'database_url', return_value='')]
        for item in self.patches: item.start()
        db.init()
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)', ('actor', 'fixture@example.invalid', 'Fixture', 'Test', '其他', 'user', 1.0))
        db.execute('INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)', ('conversation', 'actor', 'Fixture', 1.0, 1.0))
        self.original_connect = db.connect

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def row(self, identity='job', status='running', started=2.0):
        turn = {'status': status, 'answer': '', 'reasoning': '', 'toolCalls': [], 'request': {'max_tokens': 100}}
        db.execute('INSERT INTO jobs(id,user_id,conversation_id,status,input,turn,created,started,reserved,inflight,day) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identity, 'actor', 'conversation', status, '{}', json.dumps(turn), 1.0, started, 100, 50, 'fixture'))
        return db.query('SELECT * FROM jobs WHERE id=?', (identity,), one=True)

    def intent(self):
        row = self.row()
        turn = dict(json.loads(row['turn']), answer='Synthetic answer', status='complete', accounting={'tokens': 7, 'estimated': False})
        events = (jobs._TerminalEvent(1, 3.0, json.dumps({'type': 'content', 'text': 'Synthetic answer'})),
                  jobs._TerminalEvent(2, 4.0, json.dumps({'type': 'execution_end', 'status': 'complete'})))
        return jobs._TerminalIntent('job', 'actor', 'conversation', 2.0, 50, 100, 'complete', json.dumps(turn), 7, 5.0, 0, events)

    def snapshot(self):
        return db.query('SELECT * FROM jobs ORDER BY id'), db.query('SELECT * FROM job_events ORDER BY job_id,sequence')

    def fault_connection(self, mode, *, remaining=1):
        state = {'remaining': remaining, 'hits': 0}
        original = self.original_connect
        @contextlib.contextmanager
        def connect(**kwargs):
            terminal = False
            class Proxy:
                def __init__(self, connection): self.connection = connection
                def execute(self, sql, values=()):
                    nonlocal terminal
                    if sql.startswith('UPDATE jobs SET status='):
                        terminal = True
                        if mode == 'before_update' and state['remaining']:
                            state['remaining'] -= 1; state['hits'] += 1
                            raise sqlite3.OperationalError('SYNTHETIC_UNAVAILABLE')
                    return self.connection.execute(sql, values)
            with original(**kwargs) as connection:
                yield Proxy(connection)
            if mode == 'lost_commit_receipt' and terminal and state['remaining']:
                state['remaining'] -= 1; state['hits'] += 1
                raise sqlite3.OperationalError('SYNTHETIC_LOST_COMMIT_RECEIPT')
        return connect, state


class TerminalTransactionTests(DisposableDatabase, unittest.TestCase):
    def test_normal_commit_and_duplicate_receipt_are_identical(self):
        intent = self.intent()
        self.assertEqual(jobs._save_terminal(intent), 2)
        saved = self.snapshot()
        self.assertEqual(jobs._save_terminal(intent), 2)
        self.assertEqual(self.snapshot(), saved)
        self.assertEqual(saved[0][0]['charged'], 7)

    def test_commit_succeeded_but_receipt_lost_does_not_repeat_revision(self):
        intent = self.intent()
        connect, state = self.fault_connection('lost_commit_receipt')
        with patch.object(db, 'connect', connect):
            with self.assertRaises(sqlite3.OperationalError): jobs._save_terminal(intent)
        committed = self.snapshot()
        self.assertEqual(state['hits'], 1)
        jobs._save_terminal(intent)
        self.assertEqual(self.snapshot(), committed)

    def test_update_failure_rolls_back_events_before_retry(self):
        intent = self.intent()
        before = self.snapshot()
        connect, _ = self.fault_connection('before_update')
        with patch.object(db, 'connect', connect):
            with self.assertRaises(sqlite3.OperationalError): jobs._save_terminal(intent)
        self.assertEqual(self.snapshot(), before)
        jobs._save_terminal(intent)
        self.assertEqual(db.query('SELECT COUNT(*) AS n FROM job_events', one=True)['n'], 2)

    def test_equal_committed_prefix_is_completed_without_duplicate(self):
        intent = self.intent()
        first = intent.events[0]
        db.execute('INSERT INTO job_events VALUES(?,?,?,?)', ('job', first.sequence, first.created, first.event_json))
        jobs._save_terminal(intent)
        self.assertEqual(db.query('SELECT COUNT(*) AS n FROM job_events', one=True)['n'], 2)

    def test_changed_event_payload_is_never_ignored(self):
        intent = self.intent()
        db.execute('INSERT INTO job_events VALUES(?,?,?,?)', ('job', 1, 3.0, '{"different":true}'))
        before = self.snapshot()
        with self.assertRaises(jobs._TerminalConflict): jobs._save_terminal(intent)
        self.assertEqual(self.snapshot(), before)

    def test_event_hole_or_unexpected_tail_fails_without_changes(self):
        for sequence in (2, 3):
            with self.subTest(sequence=sequence):
                if sequence == 2: intent = self.intent()
                else: db.execute('DELETE FROM job_events')
                db.execute('INSERT INTO job_events VALUES(?,?,?,?)', ('job', sequence, 4.0, intent.events[1].event_json))
                before = self.snapshot()
                with self.assertRaises(jobs._TerminalConflict): jobs._save_terminal(intent)
                self.assertEqual(self.snapshot(), before)

    def test_other_terminal_is_never_overwritten(self):
        intent = self.intent()
        db.execute("UPDATE jobs SET status='stopped' WHERE id='job'")
        before = self.snapshot()
        with self.assertRaises(jobs._TerminalConflict): jobs._save_terminal(intent)
        self.assertEqual(self.snapshot(), before)

    def test_owner_started_inflight_and_reservation_are_all_bound(self):
        intent = self.intent()
        for field, value in [('user_id', 'other'), ('conversation_id', 'other'), ('started', 3.0), ('inflight', 99), ('reserved', 99)]:
            with self.subTest(field=field):
                with self.assertRaises(jobs._TerminalConflict): jobs._save_terminal(replace(intent, **{field: value}))
        self.assertEqual(db.query('SELECT status FROM jobs', one=True)['status'], 'running')

    def test_permanent_errors_are_not_classified_as_transient(self):
        for error in (jobs._TerminalConflict('CHAT_TERMINAL_EVENT_CONFLICT'), sqlite3.IntegrityError('PRIVATE'), sqlite3.ProgrammingError('PRIVATE')):
            self.assertFalse(jobs._terminal_retryable(error))
        error = sqlite3.OperationalError('PLATFORM_DATABASE_STATEMENT_ERROR'); error.sqlstate = '42601'
        self.assertFalse(jobs._terminal_retryable(error))
        self.assertTrue(jobs._terminal_retryable(sqlite3.OperationalError('PLATFORM_DATABASE_UNAVAILABLE')))

    def test_heartbeat_defaults_and_pending_responsibility_remain_visible(self):
        chat_worker.write_heartbeat('ready')
        self.assertEqual(json.loads((db.DATA/'chat-worker.json').read_text())['pending_terminal'], 0)
        chat_worker.write_heartbeat('ready', 2)
        self.assertTrue(chat_worker.heartbeat_ready())
        self.assertEqual(json.loads((db.DATA/'chat-worker.json').read_text())['pending_terminal'], 2)
        chat_worker.write_heartbeat('stopped', 1)
        self.assertEqual(json.loads((db.DATA/'chat-worker.json').read_text())['pending_terminal'], 1)
        self.assertFalse(chat_worker.heartbeat_ready())
        for bad in (-1, True, '1'):
            with self.assertRaises(ValueError): chat_worker.write_heartbeat('ready', bad)


class TerminalPersistenceTests(DisposableDatabase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        db_execution.start()
        self.delay_patch = patch.object(jobs, 'TERMINAL_RETRY_DELAYS', (0, 0, 0, 0)); self.delay_patch.start()
        self.calls = 0

    async def asyncTearDown(self):
        await db_execution.close(); db_execution.start()

    def tearDown(self):
        self.delay_patch.stop()
        super().tearDown()

    async def runner(self, row, output):
        self.calls += 1
        yield {'type': 'content', 'text': 'Synthetic answer'}
        yield {'type': 'done', 'complete': True, 'usage': {'total_tokens': 7}, 'usage_estimated': False}

    async def test_transient_terminal_failure_recovers_without_runner_replay(self):
        row = self.row(); scheduler = jobs.Scheduler(self.runner)
        connect, state = self.fault_connection('before_update')
        with patch.object(db, 'connect', connect): await scheduler.run(row, False)
        saved = db.query('SELECT * FROM jobs', one=True)
        self.assertEqual((self.calls, state['hits'], scheduler.pending_terminal_count), (1, 1, 0))
        self.assertEqual((saved['status'], saved['charged']), ('complete', 7))
        events = [json.loads(row['event']) for row in db.query('SELECT event FROM job_events ORDER BY sequence')]
        self.assertEqual(sum(event['type'] == 'execution_end' for event in events), 1)

    async def test_lost_receipt_recovers_exact_committed_terminal(self):
        row = self.row(); scheduler = jobs.Scheduler(self.runner)
        connect, state = self.fault_connection('lost_commit_receipt')
        with patch.object(db, 'connect', connect): await scheduler.run(row, False)
        saved = db.query('SELECT * FROM jobs', one=True)
        self.assertEqual((self.calls, state['hits'], scheduler.pending_terminal_count), (1, 1, 0))
        self.assertEqual((saved['status'], saved['charged']), ('complete', 7))

    async def test_done_progress_flush_database_failure_preserves_provider_result(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                identity = 'done-' + str(committed)
                row = self.row(identity); scheduler = jobs.Scheduler(self.runner)
                original = self.original_connect
                injected = False
                @contextlib.contextmanager
                def connect(**kwargs):
                    nonlocal injected
                    is_done_flush = False
                    class Proxy:
                        def execute(self, sql, values=()):
                            nonlocal injected, is_done_flush
                            if not injected and sql.startswith('UPDATE jobs SET turn=') and json.loads(values[0]).get('status') == 'complete':
                                is_done_flush = True; injected = True
                                if not committed: raise sqlite3.OperationalError('SYNTHETIC_DONE_UPDATE_FAILURE')
                            return connection.execute(sql, values)
                    with original(**kwargs) as connection:
                        yield Proxy()
                    if is_done_flush and committed: raise sqlite3.OperationalError('SYNTHETIC_DONE_RECEIPT_LOST')
                with patch.object(db, 'connect', connect): await scheduler.run(row, False)
                saved = db.query('SELECT * FROM jobs WHERE id=?', (identity,), one=True)
                self.assertTrue(injected)
                self.assertEqual((saved['status'], saved['charged']), ('complete', 7))
                self.assertEqual(json.loads(saved['turn'])['answer'], 'Synthetic answer')
                self.assertEqual(scheduler.pending_terminal_count, 0)
        self.assertEqual(self.calls, 2)

    async def test_actual_runner_error_after_done_is_not_hidden_by_terminal_status(self):
        row = self.row()
        async def runner(row, output):
            self.calls += 1
            yield {'type': 'done', 'complete': True, 'usage': {'total_tokens': 7}, 'usage_estimated': False}
            raise RuntimeError('SYNTHETIC_RUNNER_FAILURE_AFTER_DONE')
        scheduler = jobs.Scheduler(runner)
        await scheduler.run(row, False)
        saved = db.query('SELECT * FROM jobs', one=True)
        self.assertEqual((saved['status'], saved['charged'], self.calls), ('error', 100, 1))

    async def test_prepare_failure_keeps_claim_identity_and_never_calls_runner(self):
        row = self.row(status='queued', started=None)
        scheduler = jobs.Scheduler(self.runner)
        with patch.object(db, 'settings', side_effect=sqlite3.OperationalError('SYNTHETIC_SETTINGS_UNAVAILABLE')):
            # Claim by the same real dispatcher path; run receives claimed_at.
            cfg = {**db.DEFAULTS}
            with patch.object(db, 'settings', side_effect=[cfg, sqlite3.OperationalError('SYNTHETIC_SETTINGS_UNAVAILABLE')]):
                await scheduler._dispatch_once_async()
                await asyncio.gather(*scheduler.tasks.values())
        saved = db.query('SELECT * FROM jobs', one=True)
        self.assertEqual((self.calls, saved['status'], scheduler.pending_terminal_count), (0, 'error', 0))
        self.assertIsNotNone(saved['started'])

    async def test_exhausted_save_retains_owner_and_running_reservation(self):
        row = self.row(); scheduler = jobs.Scheduler(self.runner)
        with patch.object(jobs, '_save_terminal', side_effect=sqlite3.OperationalError('SYNTHETIC_OUTAGE')) as save:
            await scheduler.run(row, False)
        self.assertEqual((self.calls, save.call_count, scheduler.pending_terminal_count), (1, jobs.TERMINAL_MAX_ATTEMPTS, 1))
        self.assertTrue(scheduler._terminal_pending['job'].exhausted)
        saved = db.query('SELECT * FROM jobs', one=True)
        self.assertEqual((saved['status'], saved['charged']), ('running', 0))

    async def test_cancelled_save_drains_success_ack_without_losing_terminal(self):
        intent = self.intent(); scheduler = jobs.Scheduler(self.runner); entry = jobs._TerminalSave(intent)
        scheduler._terminal_pending['job'] = entry
        entered, release = threading.Event(), threading.Event()
        original = jobs._save_terminal
        def blocked(value):
            entered.set(); release.wait(3); return original(value)
        with patch.object(jobs, '_save_terminal', blocked):
            task = asyncio.create_task(scheduler._persist_terminal(entry))
            async with asyncio.timeout(3):
                while not entered.is_set(): await asyncio.sleep(.001)
            task.cancel(); await asyncio.sleep(.001); task.cancel(); release.set()
            self.assertTrue(await task)
        self.assertEqual(scheduler.pending_terminal_count, 0)
        self.assertEqual(db.query('SELECT status FROM jobs', one=True)['status'], 'complete')

    async def test_unconfirmed_committed_job_still_blocks_new_same_account_claim(self):
        intent = self.intent(); jobs._save_terminal(intent)
        scheduler = jobs.Scheduler(self.runner)
        scheduler._terminal_pending['job'] = jobs._TerminalSave(intent, exhausted=True)
        self.row('next', status='queued', started=None)
        self.assertFalse(await scheduler._dispatch_once_async())
        self.assertEqual(db.query("SELECT status FROM jobs WHERE id='next'", one=True)['status'], 'queued')
        self.assertEqual(self.calls, 0)

    async def test_pending_and_db_running_are_not_double_counted(self):
        intent = self.intent(); scheduler = jobs.Scheduler(self.runner)
        scheduler._terminal_pending['job'] = jobs._TerminalSave(intent, exhausted=True)
        # An independent actor can still use the second slot when capacity=2.
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)', ('other', 'other@example.invalid', 'Fixture', 'Test', '其他', 'user', 1.0))
        db.execute('INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)', ('other-conv', 'other', 'Fixture', 1.0, 1.0))
        self.row('next', status='queued', started=None)
        db.execute("UPDATE jobs SET user_id='other',conversation_id='other-conv' WHERE id='next'")
        self.assertTrue(await scheduler._dispatch_once_async())
        await asyncio.gather(*scheduler.tasks.values())
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    unittest.main()
