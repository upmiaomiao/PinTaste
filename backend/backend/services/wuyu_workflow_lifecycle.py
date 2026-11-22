"""Worker lifecycle, durable job claiming, queue order and pause-start policy.

Moved entry points operate on the existing service's runtime fields. The host
supplies _ensure, _reset_inflight and _run. Dependency callables are resolved
through the host factory on every use so existing main-module patch points
remain effective. This module does not execute a business generation step.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
import threading
from typing import Any, Callable

from sqlalchemy import update
from .wuyu_resource_limits import memory_admission
from workflow_execution import (worker_queue_block_reason, load_worker_admission_scope,
                                worker_admission_block_reason)
from .wuyu_page_execution import shutdown_page_executor
from .wuyu_workflow_terminal import TerminalPersistence, TerminalPorts

from models import db, Task
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob


@dataclass(frozen=True)
class LifecycleDependencies:
    execution_mode: Callable[..., str]
    owner_lock_held: Callable[..., bool]
    recover_workflow_charges: Callable[..., Any]
    pid_alive: Callable[..., bool]
    executor_factory: Callable[..., Any]
    inspect_schema: Callable[..., Any]


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        # os.kill(pid, 0) is not a portable liveness probe on Windows: non-
        # console signals can invoke TerminateProcess. Query without signaling.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def initialize_resources(self, app):
    self.app = app
    self.stop_event = threading.Event()
    if not 1 <= int(app.config.get('WUYU_WORKFLOW_WORKERS', 2)) <= 200:
        raise ValueError('Invalid durable workflow worker capacity')
    if not 0 <= int(app.config.get('WUYU_WORKFLOW_PER_ACTOR', 0)) <= 200:
        raise ValueError('Invalid per-account workflow capacity')
    self.execution_mode = self._lifecycle_dependencies().execution_mode(app.config)
    self.executor = None if self.execution_mode == 'api' else self._lifecycle_dependencies().executor_factory(
        max_workers=int(app.config.get('WUYU_WORKFLOW_WORKERS', 2)), thread_name_prefix='wuyu-ppt')
    self.scheduler = None
    self.futures = {}
    self.future_lock = threading.Lock()
    self.terminal_owner = TerminalPersistence(TerminalPorts(
        app=lambda: self.app,
        close_inflight=lambda body, message: self._close_inflight(body, message),
        recover_charges=lambda app: self._lifecycle_dependencies().recover_workflow_charges(app)))


def initialize(self):
    if self.execution_mode != 'combined':
        if self.execution_mode == 'worker' and not self._lifecycle_dependencies().owner_lock_held():
            raise RuntimeError('Standalone workflow worker must own its execution lock before initialization')
        inspector = self._lifecycle_dependencies().inspect_schema(db.engine)
        if not all(inspector.has_table(model.__tablename__) for model in (WuyuWorkflow, WuyuWorkflowJob)):
            raise RuntimeError('Workflow schema must already exist; migrations are a separate operation')
        if self.execution_mode == 'api':
            return  # No recovery, charge reconciliation, scheduler or execution pool in API mode.
    else:
        # Preserve existing combined-mode bootstrap for existing deployments/tests.
        WuyuWorkflow.__table__.create(db.engine, checkfirst=True)
        WuyuWorkflowJob.__table__.create(db.engine, checkfirst=True)
    interrupted = []
    for job in WuyuWorkflowJob.query.filter_by(status='running').all():
        alive = self._lifecycle_dependencies().pid_alive(job.worker_pid)
        # A newly locked standalone owner cannot have a surviving predecessor.
        # Container-local PIDs may equal the old PID even when that worker died.
        if self.execution_mode == 'worker' or not alive or not job.heartbeat_at or job.heartbeat_at < datetime.utcnow() - timedelta(minutes=3):
            job.status, job.error_code = 'interrupted', 'PROCESS_INTERRUPTED'
            interrupted.append((job.project_id, job.input_epoch))
            row = db.session.get(WuyuWorkflow, job.project_id)
            if row and row.active_job_id == job.id:
                row.active_job_id = None
            task = db.session.get(Task, job.task_id) if job.task_id else None
            if task:
                task.status, task.error_message = 'FAILED', 'Interrupted; retry is available'
    db.session.commit()
    for project_id, epoch in interrupted:
        self._reset_inflight(project_id, epoch, 'Processing was interrupted; retry is available')
    self._lifecycle_dependencies().recover_workflow_charges(self.app)
    if self.execution_mode == 'combined' and not self.app.config.get('WUYU_WORKFLOW_INLINE') and self.app.config.get('WUYU_WORKFLOW_AUTOSTART', True):
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name='wuyu-ppt-scheduler')
        self.scheduler.start()


def start_worker(self):
    # App factory initialization must finish before the standalone owner can dispatch.
    if self.execution_mode != 'worker' or not self._lifecycle_dependencies().owner_lock_held():
        raise RuntimeError('Only the locked standalone workflow worker can start execution')
    if self.scheduler is not None or self.stop_event.is_set():
        raise RuntimeError('Workflow execution has already started or stopped')
    if not self.app.config.get('WUYU_WORKFLOW_AUTOSTART', True):
        raise RuntimeError('Standalone workflow scheduling is disabled')
    self.scheduler = threading.Thread(target=self._schedule, daemon=True, name='wuyu-ppt-scheduler')
    self.scheduler.start()


def terminal_status(self):
    return self.terminal_owner.snapshot()


def shutdown(self):
    self.terminal_owner.stop()
    self.stop_event.set()
    if self.scheduler and self.scheduler is not threading.current_thread():
        self.scheduler.join(timeout=5)
    if self.executor is not None:
        self.executor.shutdown(wait=False, cancel_futures=True)
        for job_id, future in self.futures.items():
            if future.cancelled():
                self.terminal_owner.mark_unknown(job_id, 'TERMINAL_CANCELLED_BEFORE_EXECUTION')
    shutdown_page_executor(self.app)


def _claim(self, job_id):
    if self.execution_mode == 'api':
        return False
    db.session.expire_all()
    job = db.session.get(WuyuWorkflowJob, job_id)
    if not job or job.status != 'queued':
        return False
    if worker_admission_block_reason(job.id):
        return False
    if not memory_admission(self.app.config)['allowed']:
        return False
    occupied = self.terminal_owner.occupied()
    if any(claim is None or claim.project_id == job.project_id for claim in occupied.values()):
        return False
    all_running = WuyuWorkflowJob.query.filter_by(status='running').all()
    if len({item.id for item in all_running} | set(occupied)) >= int(self.app.config.get('WUYU_WORKFLOW_WORKERS', 2)):
        return False
    actor_limit = int(self.app.config.get('WUYU_WORKFLOW_PER_ACTOR', 0))
    actor_ids = {item.id for item in all_running if item.actor_user_id == job.actor_user_id}
    actor_ids.update(key for key, claim in occupied.items() if claim.actor_user_id == job.actor_user_id)
    if actor_limit and len(actor_ids) >= actor_limit:
        return False
    row = self._ensure(job.project_id)
    body = json.loads(row.body)
    if row.active_job_id:
        return False
    # Export reads only artifacts already on disk; a paused project must still
    # be able to produce its image-only PPTX when tracing failed.
    image_export = job.action == 'export' and json.loads(job.payload).get('format', 'editable_pptx') != 'editable_pptx'
    if body['paused'] and not image_export:
        return False
    # Re-read immediately before the claim: a scope may expire/change after queue ordering.
    if worker_admission_block_reason(job.id):
        return False
    claimed = db.session.execute(update(WuyuWorkflow).where(WuyuWorkflow.project_id == job.project_id,
                                                            WuyuWorkflow.active_job_id.is_(None)).values(active_job_id=job.id)).rowcount
    if not claimed:
        db.session.rollback()
        return False
    job.status, job.worker_pid, job.started_at, job.heartbeat_at = 'running', os.getpid(), datetime.utcnow(), datetime.utcnow()
    task = db.session.get(Task, job.task_id)
    if task:
        task.status = 'PROCESSING'
    self.terminal_owner.register_claim(job)
    try:
        db.session.commit()
    except BaseException:
        self.terminal_owner.mark_unknown(job_id, 'TERMINAL_CLAIM_ACK_UNKNOWN')
        raise
    return True


def run_pending(self, project_id=None):
    if self.execution_mode == 'api':
        raise RuntimeError('API mode cannot execute workflow jobs')
    query = WuyuWorkflowJob.query.filter_by(status='queued')
    if project_id:
        query = query.filter_by(project_id=project_id)
    for job_id in [j.id for j in query.order_by(WuyuWorkflowJob.created_at).all()]:
        if self._claim(job_id):
            self._run(job_id)


def _fair_queue(self, limit=100):
    """Queued jobs in the order they will start: fair between people, FIFO for one person.

        Plain first-come-first-served lets one user who submits several projects take every worker
        while everyone else waits. Here the next job always goes to the user with the fewest jobs
        running (or already placed ahead), ties broken by submission time. One user alone still
        runs in submission order. Shared by the scheduler and the queue position shown to users,
        so what they are told matches what actually happens."""
    running, identities = {}, {}
    for job in WuyuWorkflowJob.query.filter_by(status='running').all():
        identities[job.id] = job.actor_user_id
    occupied = self.terminal_owner.occupied()
    if any(claim is None for claim in occupied.values()):
        return []  # Unknown claim identity must not admit another unaccounted execution.
    identities.update({key: claim.actor_user_id for key, claim in occupied.items()})
    for user_id in identities.values():
        running[user_id] = running.get(user_id, 0) + 1
    scope = load_worker_admission_scope()
    if scope.status == 'hold':
        return []
    pending_query = WuyuWorkflowJob.query.filter_by(status='queued')
    if scope.status == 'restricted':
        if not scope.allowed_job_ids:
            return []
        # Filter before the bounded query so held jobs cannot hide an allowed job after row200.
        pending_query = pending_query.filter(WuyuWorkflowJob.id.in_(scope.allowed_job_ids))
    pending = pending_query.order_by(WuyuWorkflowJob.created_at).limit(200).all()
    ordered = []
    while pending and len(ordered) < limit:
        actor_limit = int(self.app.config.get('WUYU_WORKFLOW_PER_ACTOR', 0))
        occupied_projects = {claim.project_id for claim in occupied.values()}
        eligible = [job for job in pending if job.project_id not in occupied_projects
                    and (not actor_limit or running.get(job.actor_user_id, 0) < actor_limit)]
        if not eligible:
            break
        chosen = min(eligible, key=lambda job: (running.get(job.actor_user_id, 0), job.created_at))
        pending.remove(chosen)
        running[chosen.actor_user_id] = running.get(chosen.actor_user_id, 0) + 1
        ordered.append(chosen)
    return ordered


def queue_snapshot(self, job):
    """Where a queued job stands, in words a user can act on. None unless it is waiting."""
    if job.status != 'queued':
        return None
    block_reason = worker_admission_block_reason(job.id)
    if block_reason:
        pass  # Operational maintenance takes precedence over an unrelated memory observation.
    elif self.execution_mode == 'api':
        block_reason = worker_queue_block_reason()
    else:
        admission = memory_admission(self.app.config)
        block_reason = None if admission.get('allowed') is True else (
            'memory_wait' if admission.get('reason') == 'memory_wait' else 'worker_state_unknown')
    order = [item.id for item in self._fair_queue(limit=200)]
    if job.id not in order and block_reason is None:
        return None
    busy = WuyuWorkflowJob.query.filter_by(status='running').count()
    return {'position': order.index(job.id) + 1 if job.id in order else None,
            'queued': len(order), 'running': busy,
            'workers': int(self.app.config.get('WUYU_WORKFLOW_WORKERS', 2)),
            **({'block_reason': block_reason} if block_reason else {})}


def _schedule(self):
    while not self.stop_event.wait(.35):
        try:
            with self.app.app_context():
                # Saving accepted results is not new model work and must drain at high water.
                self.terminal_owner.retry_due()
                with self.future_lock:
                    for key, future in self.futures.items():
                        if future.done() and key in self.terminal_owner.occupied():
                            self.terminal_owner.mark_unknown(key, 'TERMINAL_DONE_FUTURE_UNRESOLVED')
                    occupied = self.terminal_owner.occupied()
                    self.futures = {key: future for key, future in self.futures.items()
                                    if not future.done() or key in occupied}
                    if not memory_admission(self.app.config)['allowed']:
                        continue
                    running_ids = {job.id for job in WuyuWorkflowJob.query.filter_by(status='running').all()}
                    available = self.executor._max_workers - len(set(self.futures) | set(occupied) | running_ids)
                    queued = self._fair_queue()
                    for job in queued:
                        if available <= 0:
                            break
                        if self._claim(job.id):
                            try:
                                self.futures[job.id] = self.executor.submit(self._run_context, job.id)
                            except BaseException:
                                self.terminal_owner.mark_unknown(job.id, 'TERMINAL_SUBMISSION_UNRESOLVED')
                                raise
                            available -= 1
        except Exception:
            # Log identifiers/status only; provider exception strings may contain secrets.
            self.app.logger.warning('Wuyu workflow scheduler retry')


def _run_context(self, job_id):
    with self.app.app_context():
        self._run(job_id)


def _heartbeat(self, job_id, done):
    # A slow model call must retain its durable lease independently of the
    # scheduler or provider callbacks. This never starts a second provider.
    interval = max(.05, float(self.app.config.get('WUYU_HEARTBEAT_SECONDS', 20)))
    while not done.wait(interval):
        try:
            with self.app.app_context():
                db.session.execute(update(WuyuWorkflowJob).where(
                    WuyuWorkflowJob.id == job_id, WuyuWorkflowJob.status == 'running',
                    WuyuWorkflowJob.worker_pid == os.getpid()).values(heartbeat_at=datetime.utcnow()))
                db.session.commit()
        except Exception:
            self.app.logger.warning('Wuyu workflow heartbeat retry: job=%s', job_id)


EXPLICIT_GENERATION_START_ACTIONS = frozenset({
    'generate_outline', 'generate_samples', 'regenerate_samples',
    'generate_next', 'generate_focused', 'generate_images',
})


def prepare_generation_start(body: dict, action: str, previous_epoch: int) -> bool:
    """Replace a paused continuation after the caller validates a new request.

    Only mutate the supplied state. The caller cancels older work and creates
    the requested job in its existing transaction after this policy returns.
    """
    if action not in EXPLICIT_GENERATION_START_ACTIONS or body.get('paused') is not True:
        return False
    body['paused'] = False
    body.pop('resume_action', None)
    if body['epoch'] == previous_epoch:
        body['epoch'] += 1
    return True
