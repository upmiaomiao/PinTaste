"""Live-worker ownership of one frozen PPT terminal result; never dispatches business work.

No database schema, provider retry, disk spool or historical-job recovery lives here.
Only the SQL adapter imports ORM modules, keeping identity/reconciliation testable.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import threading
import time
from typing import Any, Callable

MAX_ATTEMPTS = 5
RETRY_DELAYS = (1, 2, 4, 8)
RETRY_WINDOW = 90
STOP_WINDOW = 5
LOCK_TIMEOUT_MS = 2000
STATEMENT_TIMEOUT_MS = 10000
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})


class TerminalConflict(RuntimeError):
    """Fixed safe reason only; no user material or exception payload."""
    def __init__(self, code):
        if (not isinstance(code, str) or not code.startswith('TERMINAL_')
                or not code.replace('_', '').isalnum() or code != code.upper()):
            code = 'TERMINAL_CONFLICT'
        super().__init__(code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def payload_digest(value):
    return hashlib.sha256((value or '{}').encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Claim:
    job_id: str
    project_id: str
    task_id: str | None
    actor_user_id: str | None
    action: str
    input_epoch: int
    worker_pid: int | None
    started_at: datetime | None
    idempotency_key: str | None

    @classmethod
    def from_job(cls, job):
        return cls(job.id, job.project_id, job.task_id, job.actor_user_id, job.action,
                   job.input_epoch, job.worker_pid, job.started_at, job.idempotency_key)


@dataclass(frozen=True)
class Outcome:
    status: str
    error_code: str | None
    completed_at: datetime
    error_details_json: str | None


@dataclass(frozen=True)
class Target:
    job_before: dict
    job_after: dict
    task_before: dict | None
    task_after: dict | None
    workflow_before: dict
    workflow_after: dict


@dataclass
class Responsibility:
    job_id: str
    claim: Claim | None = None
    payload_digests: set[str] = field(default_factory=set)
    stage: str = 'unknown'
    execution_entered: bool = False
    outcome: Outcome | None = None
    target: Target | None = None
    terminal_ack: bool = False
    attempts: int = 0
    first_pending: float | None = None
    next_attempt: float = 0
    blocked: bool = False
    reason: str | None = None
    attempt_lock: Any = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class TerminalPorts:
    app: Callable[..., Any]
    close_inflight: Callable[..., Any]
    recover_charges: Callable[..., Any]
    dumps: Callable[..., str] = encoded


def apply_reading_failure(body, job_id, input_epoch, code):
    """Existing reader label contract, pure and shared with its legacy entry point."""
    reading = body.get('reading') or {}
    if (reading.get('_owner_job_id') != job_id or reading.get('status') != 'reading'
            or reading.get('source_revision') != body.get('material_revision', 0)):
        return False
    reading.update(status='draft_incomplete', coverage_complete=reading.get('source_reading_complete') is True,
                   failed_segments=max(0, reading.get('total_segments', 0) - reading.get('completed_segments', 0)),
                   issues=reading.get('issues', []) + [{'code': str(code or 'reading_job_failed')}])
    if code == 'CANCELLED_OR_INPUT_CHANGED':
        reading['interruption'] = {'job_status': 'cancelled', 'error_code': 'reading_cancelled',
            'reason': 'inputs_changed' if body['epoch'] != input_epoch else 'processing_stopped'}
    return True


def job_fields(job):
    return {key: getattr(job, key) for key in ('status', 'error_code', 'progress', 'completed_at')}


def task_fields(task):
    if task is None:
        return None
    return {key: getattr(task, key) for key in ('status', 'error_message', 'progress', 'completed_at')}


def workflow_fields(workflow):
    return {key: getattr(workflow, key) for key in ('body', 'revision', 'active_job_id')}


def check_identity(record, job, task, workflow):
    claim = record.claim
    if claim is None or job is None or Claim.from_job(job) != claim:
        raise TerminalConflict('TERMINAL_CLAIM_CHANGED')
    if payload_digest(job.payload) not in record.payload_digests:
        raise TerminalConflict('TERMINAL_PAYLOAD_CHANGED')
    if workflow is None or workflow.project_id != claim.project_id:
        raise TerminalConflict('TERMINAL_PROJECT_MISSING')
    if claim.task_id is not None and (task is None or task.id != claim.task_id
                                     or task.project_id != claim.project_id):
        raise TerminalConflict('TERMINAL_TASK_CHANGED')


def prepare_target(record, job, task, workflow, ports):
    """Freeze all cleanup IDs/times/body bytes once, before the first terminal write."""
    check_identity(record, job, task, workflow)
    outcome, claim = record.outcome, record.claim
    if outcome is None or job.status != 'running' or job.completed_at is not None:
        raise TerminalConflict('TERMINAL_BASE_JOB_CHANGED')
    if task is not None and (task.status not in {'PENDING', 'PROCESSING'} or task.completed_at is not None):
        raise TerminalConflict('TERMINAL_BASE_TASK_CHANGED')
    if workflow.active_job_id not in {None, claim.job_id}:
        raise TerminalConflict('TERMINAL_NEW_PROJECT_OWNER')
    before_job, before_task, before_workflow = job_fields(job), task_fields(task), workflow_fields(workflow)
    progress = before_job['progress']
    if outcome.error_details_json is not None:
        parsed = json.loads(progress or '{}')
        parsed['error'] = json.loads(outcome.error_details_json)
        progress = ports.dumps(parsed)
    after_job = {'status': outcome.status, 'error_code': outcome.error_code,
                 'progress': progress, 'completed_at': outcome.completed_at}
    after_task = None if task is None else {**before_task,
        'status': 'COMPLETED' if outcome.status == 'completed' else 'FAILED',
        'error_message': outcome.error_code, 'completed_at': outcome.completed_at}
    after_workflow = dict(before_workflow)
    if workflow.active_job_id == claim.job_id:
        after_workflow['active_job_id'] = None
    if outcome.status != 'completed':
        body = json.loads(workflow.body)
        original = deepcopy(body)
        if claim.action in {'analyze_materials', 'revise_draft'}:
            apply_reading_failure(body, claim.job_id, claim.input_epoch, outcome.error_code)
        if body['epoch'] == claim.input_epoch:
            if claim.action == '_prepare_relayout':
                body['relayout'].update(status='failed', error_code=outcome.error_code)
            if claim.action == 'run_research':
                body['research'].update(status='failed', error_code=outcome.error_code)
            ports.close_inflight(body, 'Processing did not complete; retry is available')
        if body != original:
            after_workflow.update(body=ports.dumps(body), revision=workflow.revision + 1)
    return Target(deepcopy(before_job), after_job, deepcopy(before_task), after_task,
                  deepcopy(before_workflow), after_workflow)


def reconcile_target(record, job, task, workflow):
    """Exact ACK or finite before->after patches; reject every unrelated state."""
    check_identity(record, job, task, workflow)
    target = record.target
    if target is None:
        raise TerminalConflict('TERMINAL_TARGET_MISSING')
    current_job, current_task, current_workflow = job_fields(job), task_fields(task), workflow_fields(workflow)
    if (current_job == target.job_after and current_task == target.task_after
            and workflow.active_job_id != record.job_id):
        # Our terminal txn is proven. A later human change/new owner is not ours to undo.
        return {}
    result = {}
    for name, current, before, after in (
            ('job', current_job, target.job_before, target.job_after),
            ('task', current_task, target.task_before, target.task_after),
            ('workflow', current_workflow, target.workflow_before, target.workflow_after)):
        if current == after:
            continue
        if current != before:
            raise TerminalConflict('TERMINAL_' + name.upper() + '_CONFLICT')
        if after is not None:
            result[name] = deepcopy(after)
    return result


def configure_terminal_transaction(db):
    """Bound these new row locks/statements only; preserve any stricter existing policy."""
    if db.engine.dialect.name != 'postgresql':
        return
    from sqlalchemy import text
    rows = db.session.execute(text("SELECT name, setting, unit FROM pg_settings "
        "WHERE name IN ('lock_timeout', 'statement_timeout')")).mappings().all()
    settings = {row['name']: row for row in rows}
    for name, cap in (('lock_timeout', LOCK_TIMEOUT_MS), ('statement_timeout', STATEMENT_TIMEOUT_MS)):
        row = settings.get(name)
        if row is None or row['unit'] != 'ms' or not str(row['setting']).isdigit():
            raise TerminalConflict('TERMINAL_TIMEOUT_POLICY_UNAVAILABLE')
        existing = int(row['setting'])
        selected = min(existing, cap) if existing else cap
        db.session.execute(text('SELECT set_config(:name, :value, true)'),
                           {'name': name, 'value': str(selected) + 'ms'})


def persist_terminal(record, ports):
    """Existing Job/Task/workflow tables, one short atomic transaction, no business callback."""
    from models import db, Task
    from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
    db.session.rollback()
    claim = record.claim
    if claim is None:
        raise TerminalConflict('TERMINAL_CLAIM_UNKNOWN')
    configure_terminal_transaction(db)
    # Lock only the three identity-bound rows. Other work/HTTP transactions stay outside.
    workflow = db.session.get(WuyuWorkflow, claim.project_id, with_for_update=True)
    job = db.session.get(WuyuWorkflowJob, claim.job_id, with_for_update=True)
    task = db.session.get(Task, claim.task_id, with_for_update=True) if claim.task_id else None
    if record.target is None:
        record.target = prepare_target(record, job, task, workflow, ports)
    changes = reconcile_target(record, job, task, workflow)
    if changes:
        for name, values in changes.items():
            row = {'job': job, 'task': task, 'workflow': workflow}[name]
            for key, value in values.items():
                setattr(row, key, value)
            if name == 'workflow' and values['body'] != record.target.workflow_before['body']:
                row.updated_at = record.outcome.completed_at
        db.session.commit()
        # COMMIT returning is distinct from a complete same-intent readback ACK.
        db.session.expire_all()
        configure_terminal_transaction(db)
        workflow = db.session.get(WuyuWorkflow, claim.project_id, with_for_update=True)
        job = db.session.get(WuyuWorkflowJob, claim.job_id, with_for_update=True)
        task = db.session.get(Task, claim.task_id, with_for_update=True) if claim.task_id else None
        if reconcile_target(record, job, task, workflow):
            raise TerminalConflict('TERMINAL_READBACK_INCOMPLETE')
    db.session.rollback()  # Release read locks before the existing fee reconciler opens its txn.


class TerminalPersistence:
    """Independent local ownership survives completed Futures; bounded persistence only."""
    def __init__(self, ports, *, clock=time.monotonic):
        self.ports, self.clock = ports, clock
        self._records = {}
        self._lock = threading.RLock()
        self._stopped_at = None

    def register_claim(self, job):
        claim = Claim.from_job(job)
        with self._lock:
            if job.id in self._records:
                raise TerminalConflict('TERMINAL_DUPLICATE_CLAIM')
            self._records[job.id] = Responsibility(job.id, claim,
                {payload_digest(job.payload)}, stage='claimed')

    def begin_execution(self, job_id):
        with self._lock:
            record = self._records.setdefault(job_id, Responsibility(job_id))
            if record.execution_entered or record.outcome is not None:
                return False
            record.execution_entered = True
            record.stage = 'executing' if record.claim else 'unknown'
            return True

    def bind_execution(self, job):
        with self._lock:
            record = self._records[job.id]
            if record.claim is None:
                if job.status != 'running':
                    self._records.pop(job.id)
                    return False
                record.claim = Claim.from_job(job)
                record.payload_digests.add(payload_digest(job.payload))
            if Claim.from_job(job) != record.claim or payload_digest(job.payload) not in record.payload_digests:
                raise TerminalConflict('TERMINAL_EXECUTION_IDENTITY_CHANGED')
            record.stage = 'executing'
            return True

    def allow_own_payload(self, job_id, value):
        with self._lock:
            record = self._records[job_id]
            if record.outcome is not None:
                raise TerminalConflict('TERMINAL_PAYLOAD_AFTER_OUTCOME')
            record.payload_digests.add(payload_digest(value))

    def mark_unknown(self, job_id, reason):
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return  # A confirmed result must not be resurrected by later context cleanup.
            if record.outcome is None:
                record.stage, record.blocked, record.reason = 'unknown', True, reason

    def freeze_outcome(self, job_id, status, error, details=None, *, completed_at=None):
        if status not in {'completed', 'failed', 'cancelled'}:
            raise TerminalConflict('TERMINAL_OUTCOME_INVALID')
        outcome = Outcome(status, error, completed_at or datetime.utcnow(),
                          self.ports.dumps(deepcopy(details)) if details is not None else None)
        with self._lock:
            record = self._records[job_id]
            if record.claim is None:
                raise TerminalConflict('TERMINAL_OUTCOME_WITHOUT_CLAIM')
            if record.outcome is not None:
                raise TerminalConflict('TERMINAL_OUTCOME_ALREADY_FROZEN')
            record.outcome, record.stage, record.blocked = outcome, 'outcome_pending', False
            record.first_pending = self.clock()
            record.next_attempt = record.first_pending

    def attempt(self, job_id):
        with self._lock:
            record = self._records.get(job_id)
        if record is None:
            return True
        if not record.attempt_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self._records.get(job_id) is not record:
                    return True
                now = self.clock()
                if record.outcome is None or record.blocked or now < record.next_attempt:
                    return False
                if (record.attempts >= MAX_ATTEMPTS or now - record.first_pending > RETRY_WINDOW
                        or self._stopped_at is not None and now - self._stopped_at > STOP_WINDOW):
                    record.blocked, record.reason = True, 'TERMINAL_RETRY_BUDGET_EXHAUSTED'
                    return False
                record.attempts += 1
            try:
                if not record.terminal_ack:
                    persist_terminal(record, self.ports)
                    with self._lock:
                        record.terminal_ack, record.stage = True, 'charge_pending'
                self.ports.recover_charges(self.ports.app())
            except Exception as exc:
                try:
                    from models import db
                    db.session.rollback()
                except Exception:
                    pass
                with self._lock:
                    record.reason = (str(exc) if isinstance(exc, TerminalConflict)
                                     else 'TERMINAL_CHARGE_ACK_PENDING' if record.terminal_ack
                                     else 'TERMINAL_STORAGE_ACK_PENDING')
                    if isinstance(exc, TerminalConflict):
                        record.blocked = True
                    record.next_attempt = self.clock() + RETRY_DELAYS[min(record.attempts - 1, 3)]
                self.ports.app().logger.warning('Wuyu terminal persistence pending: job=%s stage=%s attempt=%s code=%s',
                    job_id, record.stage, record.attempts, record.reason)
                return False
            with self._lock:
                if self._records.get(job_id) is record:
                    self._records.pop(job_id)
            return True
        finally:
            record.attempt_lock.release()

    def retry_due(self):
        with self._lock:
            ids = list(self._records)
        for job_id in ids:
            self.attempt(job_id)

    def stop(self):
        with self._lock:
            if self._stopped_at is None:
                self._stopped_at = self.clock()

    def occupied(self):
        with self._lock:
            return {key: record.claim for key, record in self._records.items()}

    def snapshot(self):
        with self._lock:
            records = list(self._records.values())
            pending = [record for record in records if record.outcome is not None or record.stage == 'unknown']
            return {'pending_terminal': len(pending),
                    'pending_terminal_unknown': sum(record.stage == 'unknown' for record in pending),
                    'pending_terminal_blocked': sum(record.blocked for record in pending),
                    'owned_jobs': len(records),
                    'active_terminal_attempts': sum(record.attempt_lock.locked() for record in records)}
