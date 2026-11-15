"""Durable jobs; one scheduler, user round-robin, explicit token reservations."""
import asyncio
import contextlib
import json
import time
import logging
import copy
import re
import math
import sqlite3
from dataclasses import dataclass
from collections import Counter
from . import store as db
from .remote_sensing_view import enrich_turn
from .chat_accounting import collect_stop_evidence, stopped_accounting, KEY as STOP_ACCOUNTING_KEY

TERMINAL = {'complete', 'error', 'incomplete', 'stopped', 'interrupted'}
DISPATCH_RETRY_SECONDS = 1
TERMINAL_MAX_ATTEMPTS = 5
TERMINAL_RETRY_DELAYS = (1, 2, 4, 8)
TERMINAL_RETRY_WINDOW = 90
TERMINAL_STOP_WINDOW = 5


@dataclass(frozen=True)
class _TerminalEvent:
    sequence: int
    created: float
    event_json: str


@dataclass(frozen=True)
class _TerminalIntent:
    job_id: str
    user_id: str
    conversation_id: str
    started: float | None
    inflight: int
    reserved: int
    status: str
    turn_json: str
    charged: int
    finished: float
    base_sequence: int
    events: tuple[_TerminalEvent, ...]


@dataclass
class _TerminalSave:
    intent: _TerminalIntent
    attempts: int = 0
    deadline: float | None = None
    next_attempt: float = 0
    committed: bool = False
    exhausted: bool = False
    last_warning: float | None = None


class _TerminalConflict(RuntimeError):
    """A fixed persistence code; never includes user content or driver details."""


class _ProgressPersistenceFailure(RuntimeError):
    """Identify database flush failures separately from actual runner failures."""


def _save_terminal(intent):
    """Confirm or commit one frozen terminal batch; never rerun paid execution."""
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (intent.job_id,)).fetchone()
        if (row is None or row['user_id'] != intent.user_id
                or row['conversation_id'] != intent.conversation_id or row['started'] != intent.started
                or row['inflight'] != intent.inflight or row['reserved'] != intent.reserved):
            raise _TerminalConflict('CHAT_TERMINAL_IDENTITY_CONFLICT')
        conversation = connection.execute('SELECT user_id FROM conversations WHERE id=?', (intent.conversation_id,)).fetchone()
        if conversation is None or conversation['user_id'] != intent.user_id:
            raise _TerminalConflict('CHAT_TERMINAL_CONVERSATION_CONFLICT')
        if intent.base_sequence and connection.execute(
                'SELECT sequence FROM job_events WHERE job_id=? AND sequence=?',
                (intent.job_id, intent.base_sequence)).fetchone() is None:
            raise _TerminalConflict('CHAT_TERMINAL_EVENT_CONFLICT')
        saved = connection.execute('SELECT sequence,created,event FROM job_events WHERE job_id=? AND sequence>? ORDER BY sequence',
                                   (intent.job_id, intent.base_sequence)).fetchall()
        if len(saved) > len(intent.events) or any(
                actual['sequence'] != expected.sequence or actual['created'] != expected.created
                or actual['event'] != expected.event_json for actual, expected in zip(saved, intent.events)):
            raise _TerminalConflict('CHAT_TERMINAL_EVENT_CONFLICT')
        if any(item.sequence != intent.base_sequence + offset for offset, item in enumerate(intent.events, 1)):
            raise _TerminalConflict('CHAT_TERMINAL_EVENT_CONFLICT')
        end_sequence = intent.base_sequence + len(intent.events)
        if row['status'] in TERMINAL:
            if (row['status'] != intent.status or row['turn'] != intent.turn_json
                    or row['charged'] != intent.charged or row['finished'] != intent.finished
                    or len(saved) != len(intent.events)):
                raise _TerminalConflict('CHAT_TERMINAL_RESULT_CONFLICT')
            return end_sequence
        if row['status'] != 'running' or intent.status not in TERMINAL:
            raise _TerminalConflict('CHAT_TERMINAL_STATE_CONFLICT')
        for item in intent.events[len(saved):]:
            connection.execute('INSERT INTO job_events VALUES(?,?,?,?)',
                               (intent.job_id, item.sequence, item.created, item.event_json))
        changed = connection.execute(
            "UPDATE jobs SET status=?,turn=?,charged=?,finished=?,revision=revision+1 WHERE id=? AND status='running' AND revision=?",
            (intent.status, intent.turn_json, intent.charged, intent.finished, intent.job_id, row['revision']))
        if changed.rowcount != 1:
            raise _TerminalConflict('CHAT_TERMINAL_STATE_CONFLICT')
    return end_sequence


def _terminal_retryable(error):
    if isinstance(error, (_TerminalConflict, sqlite3.IntegrityError, sqlite3.ProgrammingError)):
        return False
    state = getattr(error, 'sqlstate', None)
    if isinstance(state, str) and state.startswith(('22', '23', '42')):
        return False
    if error.args == ('PLATFORM_DATABASE_STATEMENT_ERROR',):
        return False
    return isinstance(error, (sqlite3.OperationalError, OSError, TimeoutError))


def log_safe_failure(phase, error, job_id=None):
    """Log execution coordinates, never exception messages, arguments or locals."""
    identity = job_id if isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id) else 'unavailable'
    name = type(error).__name__
    kind = name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'Exception'
    sites = []
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame.f_code
        if frame.co_filename == __file__:
            function = frame.co_name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', frame.co_name) else 'scope'
            sites.append(function + ':' + str(trace.tb_lineno))
        trace = trace.tb_next
    logging.getLogger(__name__).warning('Chat execution deferred: phase=%s job=%s type=%s sites=%s',
                                       phase, identity, kind, ','.join(sites[-4:]) or 'unavailable')


def select_next(rows, running, cfg, last_user):
    counts = Counter(job['user_id'] for job in running)
    used = sum(job['inflight'] for job in running)
    if len(running) >= cfg['max_parallel']: return None
    users = list(dict.fromkeys(row['user_id'] for row in rows))
    if last_user in users:
        index = users.index(last_user) + 1; users = users[index:] + users[:index]
    for user in users:
        if counts[user] >= cfg['per_user_parallel']: continue
        row = next(row for row in rows if row['user_id'] == user)
        if used + row['inflight'] <= cfg['inflight_token_budget']: return row
    return None


def reduce_event(turn, event):
    kind = event['type']
    collect_stop_evidence(turn, event)
    # Keep evidence across tool-plan answer resets. A later rejected round must
    # not erase work already performed in an earlier round.
    if (kind in ('content', 'reasoning') and event.get('text')) or kind in ('tool_plan', 'tool_start', 'model_response'):
        turn['_accounting_progress'] = True
    if kind == 'model_request':
        # A previous rejected attempt says nothing about this new attempt.
        turn.pop('_accounting_upstream_rejection', None)
    if kind in ('error', 'model_response_error'):
        status = event.get('upstream_status')
        if type(status) is int and 400 <= status <= 599:
            turn['_accounting_upstream_rejection'] = True
    if kind == 'content': turn['answer'] += event['text']
    elif kind == 'reasoning':
        previous = turn['reasoning']
        turn['reasoning'] += event['text']
        rounds = turn.get('reasoningRounds')
        if (not isinstance(rounds, list) or any(not isinstance(item, dict)
                or not isinstance(item.get('text'), str) for item in rounds)
                or ''.join(item['text'] for item in rounds) != previous):
            rounds = [{'round': None, 'text': previous}] if previous else []
        number = event.get('round')
        number = number if type(number) is int and number > 0 else None
        if rounds and rounds[-1].get('round') == number:
            rounds[-1]['text'] += event['text']
        else:
            rounds.append({'round': number, 'text': event['text']})
        turn['reasoningRounds'] = rounds
    elif kind == 'model_round': turn['phase'] = '正在检索与分析' if event['tools_available'] else '正在整理回答'
    elif kind == 'tool_plan': turn['answer'] = ''
    elif kind == 'tool_start': turn['toolCalls'].append({**event, 'status': 'running'})
    elif kind == 'tool_result':
        turn['toolCalls'] = [{**call, **event, 'status': 'complete' if event.get('ok') else 'error'} if call['call_id'] == event['call_id'] else call for call in turn['toolCalls']]
    elif kind == 'citations': turn.update(citations=event['sources'], searchMeta=event)
    elif kind == 'document_sources':
        turn.update(documentSources=event['sources'], documentRag=event['info'])
        if event['info'].get('phase') == 'retrieving':
            turn['phase'] = '正在检索附件原文'
    elif kind == 'warning': turn['warning'] = (turn.get('warning', '') + ' ' + event['message']).strip()
    elif kind == 'usage': turn['_usage'] = event['usage']
    elif kind == 'error':
        turn.update(status='error', error=event['message'])
        if event.get('usage'): turn['_usage'] = event['usage']
    elif kind == 'done':
        turn.update(meta=event, status='complete' if event.get('complete') else 'incomplete')
        if event.get('usage'): turn['_usage'] = event['usage']
        if not event.get('complete'): turn['error'] = '本次生成达到长度上限或未完整结束，可继续提问。'
    return turn


def settle_accounting(turn, reserved):
    """Release a rejected first request; retain conservative unknown in-flight use."""
    stop_evidence = turn.pop(STOP_ACCOUNTING_KEY, None)
    usage = turn.pop('_usage', None) or turn.get('meta', {}).get('usage')
    progress = turn.pop('_accounting_progress', False)
    rejected = turn.pop('_accounting_upstream_rejection', False)
    exact = bool(usage and turn.get('meta', {}).get('usage')
                 and not turn['meta'].get('usage_estimated')
                 and turn['status'] in ('complete', 'incomplete')
                 and usage.get('total_tokens') is not None)
    if exact:
        return {'tokens': int(usage['total_tokens']), 'estimated': False}
    if turn.get('status') == 'stopped':
        observed = stopped_accounting(stop_evidence, reserved)
        if observed is not None:
            return observed
    if (turn['status'] == 'error' and rejected and not usage and not progress
            and not turn.get('answer') and not turn.get('reasoning') and not turn.get('toolCalls')
            and not turn.get('meta')):
        # An explicit non-200 response, before any model/tool output or usage,
        # is not a completed model turn. This is platform quota, not a vendor bill.
        return {'tokens': 0, 'estimated': True, 'basis': 'upstream_rejected_before_output'}
    return {'tokens': reserved, 'estimated': True}


def xml_display_answer(answer):
    """Keep leading model XML visible to Markdown clients without enabling HTML.

    Presentation only: the database turn and raw model-response archive stay
    untouched. A longer delimiter preserves even embedded Markdown fences.
    """
    if not isinstance(answer, str) or not re.match(r'^<(?:thinking|answer)(?:\s|>)', answer.lstrip(), re.I):
        return answer
    longest = max((len(match.group(0)) for match in re.finditer(r'`+', answer)), default=0)
    fence = '`' * max(3, longest + 1)
    return fence + 'xml\n' + answer + '\n' + fence


def execution_timing(row, now=None):
    """Derive wall-clock job phases from existing timestamps, never agent/model latency."""
    row = dict(row)
    def timestamp(value):
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    created, started, finished = (timestamp(row.get(key)) for key in ('created', 'started', 'finished'))
    observed = timestamp(time.time() if now is None else now)
    if created is None or observed is None:
        return None
    status = row.get('status')
    active = status in {'queued', 'running'}
    end = observed if active else finished
    queue_end = started if started is not None else end if status != 'running' else None
    duration = lambda first, last: round(max(0, last - first), 3) if first is not None and last is not None else None
    return {'sampled_at': observed, 'phase': status,
            'queue_seconds': duration(created, queue_end),
            'execution_seconds': duration(started, end),
            'total_seconds': duration(created, end)}


def public_turn(row):
    turn = json.loads(row['turn'])
    turn.pop('_usage', None)
    turn.pop(STOP_ACCOUNTING_KEY, None)
    turn.pop('_accounting_progress', None)
    turn.pop('_accounting_upstream_rejection', None)
    if 'answer' in turn:
        turn['answer'] = xml_display_answer(turn['answer'])
    turn.update(status=row['status'], revision=row['revision'])
    timing = execution_timing(row)
    if timing is not None:
        turn['executionTiming'] = timing
    else:
        turn.pop('executionTiming', None)
    return enrich_turn(turn, row)


def request_cancel(row):
    """Durable cancellation through existing task JSON, shared by API and worker."""
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
        if current is None:
            return
        if current['status'] == 'queued':
            turn = json.loads(current['turn'])
            turn.update(status='stopped', error='已取消排队。')
            connection.execute("UPDATE jobs SET status='stopped',turn=?,charged=0,finished=?,revision=revision+1 WHERE id=? AND status='queued'",
                (json.dumps(turn, ensure_ascii=False), time.time(), current['id']))
        elif current['status'] == 'running':
            saved = json.loads(current['input'])
            saved['_cancel_requested'] = True
            connection.execute("UPDATE jobs SET input=? WHERE id=? AND status='running'",
                (json.dumps(saved, ensure_ascii=False), current['id']))


class Scheduler:
    def __init__(self, runner):
        self.runner = runner; self.tasks = {}; self.stopping = False; self.last_user = None; self.loop = None
        self._pending_claim = None
        self._dispatch_phase = 'read'
        self._last_dispatch_warning = None
        self._terminal_pending = {}

    @property
    def pending_terminal_count(self):
        return len(self._terminal_pending)

    def _terminal_warning(self, entry, error, *, final=False):
        now = time.monotonic()
        if final or entry.last_warning is None or now - entry.last_warning >= 30:
            log_safe_failure('terminal_exhausted' if final else 'terminal_save', error, entry.intent.job_id)
            identity = entry.intent.job_id if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', entry.intent.job_id) else 'unavailable'
            state = getattr(error, 'sqlstate', None)
            state = state if isinstance(state, str) and re.fullmatch('[A-Z0-9]{5}', state) else 'unavailable'
            logging.getLogger(__name__).warning('Chat terminal persistence: job=%s attempts=%s retained=%s sqlstate=%s',
                                               identity, entry.attempts, self.pending_terminal_count, state)
            entry.last_warning = now

    async def _persist_terminal(self, entry):
        """Keep this run's save responsibility even if the execution task ends."""
        if entry.deadline is None:
            entry.deadline = time.monotonic() + TERMINAL_RETRY_WINDOW
        def acknowledge(_):
            entry.committed = True
            if self._terminal_pending.get(entry.intent.job_id) is entry:
                self._terminal_pending.pop(entry.intent.job_id)
        while not entry.committed:
            now = time.monotonic()
            if self.stopping:
                entry.deadline = min(entry.deadline, now + TERMINAL_STOP_WINDOW)
            if entry.attempts >= TERMINAL_MAX_ATTEMPTS or now >= entry.deadline:
                entry.exhausted = True
                self._terminal_warning(entry, _TerminalConflict('CHAT_TERMINAL_SAVE_BUDGET_EXHAUSTED'), final=True)
                return False
            try:
                if now < entry.next_attempt:
                    await asyncio.sleep(min(entry.next_attempt, entry.deadline) - now)
                    continue
                entry.attempts += 1
                await db.run_sync_complete(_save_terminal, acknowledge, entry.intent)
            except asyncio.CancelledError:
                # db_execution drains the submitted DB operation before this
                # exception, and invokes acknowledge first on returned success.
                # A lost receipt remains pending; never infer rollback here.
                if entry.committed:
                    return True
                entry.next_attempt = time.monotonic()
                continue
            except Exception as error:
                if not _terminal_retryable(error):
                    entry.exhausted = True
                    self._terminal_warning(entry, error, final=True)
                    return False
                self._terminal_warning(entry, error)
                entry.next_attempt = time.monotonic() + TERMINAL_RETRY_DELAYS[min(entry.attempts - 1, len(TERMINAL_RETRY_DELAYS) - 1)]
        return True

    async def start(self):
        self.event_loop = asyncio.get_running_loop()
        def recover_running():
            for row in db.query("SELECT * FROM jobs WHERE status='running'"):
                cancelled = json.loads(row['input']).get('_cancel_requested') is True
                status = 'stopped' if cancelled else 'interrupted'
                turn = json.loads(row['turn']); turn.update(status=status, error='已停止生成，已保留收到的内容。' if cancelled else '执行服务重启使本次生成中断，已保留收到的内容。请重新提问。')
                db.execute("UPDATE jobs SET status=?,turn=?,charged=reserved,finished=?,revision=revision+1 WHERE id=? AND status='running'", (status, json.dumps(turn, ensure_ascii=False), time.time(), row['id']))
        await db.run_sync(recover_running)
        self.loop = asyncio.create_task(self.dispatch())

    async def stop(self):
        self.stopping = True
        if self.loop:
            self.loop.cancel()
            # A committed claim is registered on this loop before cancellation
            # propagates. Drain dispatch first so stop cannot miss that owner.
            await asyncio.gather(self.loop, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def dispatch(self):
        while True:
            try:
                dispatched = await self._dispatch_once_async()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                now = time.monotonic()
                if self._last_dispatch_warning is None or now - self._last_dispatch_warning >= 30:
                    log_safe_failure(self._dispatch_phase, error,
                                     self._pending_claim[0] if self._pending_claim else None)
                    self._last_dispatch_warning = now
                # Existing execution tasks remain alive during a polling outage.
                await asyncio.sleep(DISPATCH_RETRY_SECONDS)
                continue
            self._last_dispatch_warning = None
            if not dispatched:
                await asyncio.sleep(.2)

    def _dispatch_once(self):
        # Compatibility entry for synchronous operator/tests; same decision body.
        steps = self._dispatch_steps()
        value = None
        try:
            while True:
                function, args, kwargs = steps.send(value)
                value = function(*args, **kwargs)
        except StopIteration as done:
            return done.value
        finally:
            steps.close()

    async def _dispatch_once_async(self):
        steps = self._dispatch_steps()
        state = {}
        def advance(value):
            try:
                state['operation'] = steps.send(value)
            except StopIteration as done:
                state['done'] = True
                state['result'] = done.value
        try:
            advance(None)
            while not state.get('done'):
                function, args, kwargs = state['operation']
                await db.run_sync_complete(function, advance, *args, **kwargs)
            return state['result']
        finally:
            steps.close()

    def _dispatch_steps(self):
        self._dispatch_phase = 'read'
        cfg = (yield (db.settings, (), {}))
        rows = (yield (db.query, ("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id",), {}))
        running = (yield (db.query, ("SELECT * FROM jobs WHERE status='running'",), {}))
        self._dispatch_phase = 'cancel'
        for row in running:
            task = self.tasks.get(row['id'])
            if task is not None and not task.done() and not task.cancelling() and json.loads(row['input']).get('_cancel_requested') is True:
                task.cancel()
        if self._pending_claim is not None:
            self._dispatch_phase = 'claim_reconcile'
            identity, busy = self._pending_claim
            if identity in self.tasks:
                self._pending_claim = None
                return True
            pending = next((r for r in running if r['id'] == identity), None)
            if pending is not None:
                # A failed claim receipt cannot have reached _launch. Only this
                # in-process proof permits recovery; arbitrary running jobs do not.
                if json.loads(pending['input']).get('_cancel_requested') is True:
                    (yield (self._cancel_unstarted_claim, (pending,), {}))
                else:
                    self._launch(pending, busy)
                self._pending_claim = None
                return True
            # Queued means the claim did not commit; terminal/missing is left alone.
            self._pending_claim = None
        # A COMMIT can succeed while its receipt is lost. Such an unconfirmed
        # local owner remains occupied even if its DB row is already terminal.
        occupied = list(running)
        running_ids = {row['id'] for row in running}
        for identity, entry in self._terminal_pending.items():
            if identity not in running_ids:
                occupied.append({'id': identity, 'user_id': entry.intent.user_id, 'inflight': entry.intent.inflight})
        selected = select_next(rows, occupied, cfg, self.last_user)
        if selected is None:
            return False
        busy = len({r['user_id'] for r in rows}) > 1
        self._dispatch_phase = 'claim'
        self._pending_claim = (selected['id'], busy)
        # The async driver drains each DB result and resumes this generator on
        # its owning loop before propagating cancellation after a claim.
        claimed_at = time.time()
        claimed = (yield (db.execute, ("UPDATE jobs SET status='running',started=?,revision=revision+1 WHERE id=? AND status='queued'", (claimed_at, selected['id'])), {}))
        if claimed:
            self._launch(dict(selected, started=claimed_at), busy)
        self._pending_claim = None
        return True

    def _launch(self, row, busy):
        self.last_user = row['user_id']
        task = asyncio.create_task(self.run(row, busy))
        self.tasks[row['id']] = task
        task.add_done_callback(lambda finished, job_id=row['id']: self._task_finished(job_id, finished))

    def _task_finished(self, job_id, task):
        if self.tasks.get(job_id) is task:
            self.tasks.pop(job_id, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                # Retrieve task exceptions so asyncio does not print unsanitized
                # exception strings. Do not replay potentially paid execution.
                log_safe_failure('task_persistence', error, job_id)

    def _cancel_unstarted_claim(self, row):
        with db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if current is None or current['status'] != 'running':
                return
            assert json.loads(current['input']).get('_cancel_requested') is True
            turn = json.loads(current['turn'])
            # The local claim proof is necessary; reject old paid evidence too.
            assert not (current['charged'] or turn.get('answer') or turn.get('reasoning') or turn.get('toolCalls')
                        or turn.get('_usage') or turn.get('_accounting_progress') or turn.get('meta'))
            turn.update(status='stopped', error='已取消排队。',
                        accounting={'tokens': 0, 'estimated': True, 'basis': 'cancelled_before_dispatch'})
            connection.execute("UPDATE jobs SET status='stopped',turn=?,charged=0,finished=?,revision=revision+1 WHERE id=? AND status='running'",
                               (json.dumps(turn, ensure_ascii=False), time.time(), row['id']))

    async def run(self, row, busy):
        turn = json.loads(row['turn']); turn['status'] = 'running'; last_save = 0
        pending = []
        sequence = 0
        cfg = None
        def record(event):
            item = copy.deepcopy(event)
            # Coalesce only adjacent deltas in the same round. The full text and
            # ordering survive without one SQLite row per streamed token.
            if pending and item['type'] in ('content', 'reasoning'):
                previous = pending[-1]['event']
                if {k: v for k, v in previous.items() if k != 'text'} == {k: v for k, v in item.items() if k != 'text'}:
                    previous['text'] += item['text']; return
            pending.append({'created': time.time(), 'event': item})
        record({'type': 'execution_start', 'trace_version': 1, 'job_id': row['id']})
        def prepare_execution():
            sequence = db.query('SELECT COALESCE(MAX(sequence),0) AS n FROM job_events WHERE job_id=?', (row['id'],), one=True)['n']
            return sequence, db.settings()
        def prepared(value):
            nonlocal sequence, cfg, output
            sequence, cfg = value
            output = min(turn['request']['max_tokens'], cfg['busy_output_tokens']) if busy else turn['request']['max_tokens']
            if output < turn['request']['max_tokens']:
                turn['warning'] = f'当前多人排队，本轮输出预算为 {output:,} tokens，可在回答后继续追问。'
        output = turn['request']['max_tokens']
        def flush():
            nonlocal sequence
            with db.connect() as conn:
                conn.execute('UPDATE jobs SET turn=?,revision=revision+1 WHERE id=?', (json.dumps(turn, ensure_ascii=False), row['id']))
                for offset, item in enumerate(pending, 1):
                    conn.execute('INSERT INTO job_events VALUES(?,?,?,?)',
                        (row['id'], sequence + offset, item['created'], json.dumps(item['event'], ensure_ascii=False)))
            sequence += len(pending); pending.clear()
        try:
            await db.run_sync_complete(prepare_execution, prepared)
            async with asyncio.timeout(cfg['job_timeout']):
                # Carry the same execution-time setting to the native agent so
                # an older fixed inner deadline cannot cut off a long answer.
                execution_row = dict(row, _timeout_seconds=cfg['job_timeout'])
                async with contextlib.aclosing(self.runner(execution_row, output)) as stream:
                    async for event in stream:
                        record(event)
                        reduce_event(turn, event)
                        # Avoid a disk transaction for every individual token.
                        if event['type'] not in ('content', 'reasoning') or time.monotonic() - last_save >= .25:
                            try:
                                await db.run_sync(flush)
                            except (sqlite3.Error, OSError, TimeoutError) as error:
                                raise _ProgressPersistenceFailure() from error
                            last_save = time.monotonic()
            if turn['status'] == 'running': turn.update(status='incomplete', error='模型连接结束但未完成回答，请重试。')
        except asyncio.CancelledError:
            turn.update(status='interrupted' if self.stopping else 'stopped', error='服务重启，已保留生成内容。' if self.stopping else '已停止生成，已保留收到的内容。')
        except TimeoutError: turn.update(status='error', error='任务超时，已保留收到的内容。')
        except _ProgressPersistenceFailure as error:
            log_safe_failure('progress_save', error.__cause__ or error, row['id'])
            # A received done/error result belongs to the provider. Its failed
            # DB flush is not authority to replace status or usage. Actual
            # runner exceptions still take the separate general branch below.
            if turn['status'] not in TERMINAL:
                turn.update(status='error', error='生成服务暂不可用，请稍后再试或联系管理员。')
        except Exception as error:
            log_safe_failure('run', error, row['id'])
            turn.update(status='error', error='生成服务暂不可用，请稍后再试或联系管理员。')
        turn['accounting'] = settle_accounting(turn, row['reserved'])
        charged = turn['accounting']['tokens']
        record({'type': 'execution_end', 'status': turn['status'], 'accounting': turn['accounting'],
                'error': turn.get('error'), 'final_answer': turn['answer']})
        intent = _TerminalIntent(row['id'], row['user_id'], row['conversation_id'], dict(row).get('started'), row['inflight'], row['reserved'],
            turn['status'], json.dumps(turn, ensure_ascii=False), charged, time.time(), sequence,
            tuple(_TerminalEvent(sequence + offset, item['created'], json.dumps(item['event'], ensure_ascii=False))
                  for offset, item in enumerate(pending, 1)))
        entry = _TerminalSave(intent)
        if row['id'] in self._terminal_pending:
            raise _TerminalConflict('CHAT_TERMINAL_OWNER_CONFLICT')
        self._terminal_pending[row['id']] = entry
        if not await self._persist_terminal(entry):
            return
        if self.stopping or asyncio.current_task().cancelling():
            return
        try:
            await db.run_sync(db.execute, 'UPDATE conversations SET updated=? WHERE id=?', (time.time(), row['conversation_id']))
        except Exception as error:
            # The answer and fees are committed. Follow-up sorting/indexing is
            # not authority to replay or fail the terminal result.
            log_safe_failure('conversation_touch', error, row['id'])
        if turn['status'] == 'complete':
            try:
                from . import memory
                await asyncio.to_thread(memory.after_completion, row)
            except Exception:
                # The lossless answer is already committed. Retry indexing from
                # the archive on the next question, without failing the answer.
                logging.getLogger(__name__).warning('Memory indexing deferred for job %s', row['id'])

    def cancel(self, row):
        request_cancel(row)
