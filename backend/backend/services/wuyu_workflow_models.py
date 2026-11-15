"""ModelExecution: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from concurrent.futures import FIRST_COMPLETED
import json
import math
import threading
import time
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_state import WorkflowError
from services.wuyu_errors import PipelineError
from services.wuyu_quota import QuotaError
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from .wuyu_resource_limits import page_slot
from .wuyu_page_execution import page_executor, validate_execution_policy
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class ModelExecutionPorts:
    _catalog_path: Callable[..., Any]
    _check_cancel: Callable[..., Any]
    _pipeline: Callable[..., Any]
    _record_batch_timing: Callable[..., Any]
    app: Callable[..., Any]
    hook_ThreadPoolExecutor: Callable[..., Any]
    hook_WorkflowCharge: Callable[..., Any]
    hook__failure_message: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook__quality_report: Callable[..., Any]
    hook__safe_failure_code: Callable[..., Any]
    hook_wait: Callable[..., Any]



class ModelExecution:
    def __init__(self, ports: ModelExecutionPorts):
        self.ports = ports

    def _pipeline(self, config=None):
        config = dict(self.ports.app().config if config is None else config)
        if 'WUYU_TEMPLATE_CATALOG_BINDING' in config:
            from services.wuyu_template_library import resolve_catalog_binding
            try:config['WUYU_TEMPLATE_LIBRARY_PATH'] = str(resolve_catalog_binding(self.ports.app().config, config['WUYU_TEMPLATE_CATALOG_BINDING']))
            except (ValueError, OSError):
                raise PipelineError('TEMPLATE_BINDING_INVALID', '任务模板版本丢失或已变化，未发起模型调用。') from None
        else:
            current_catalog = self.ports._catalog_path()
            if current_catalog.is_file():
                config['WUYU_TEMPLATE_LIBRARY_PATH'] = str(current_catalog)
        factory = self.ports.app().config.get('WUYU_PIPELINE_FACTORY')
        if factory:
            return factory(config)
        from services.wuyu_pipeline import WuyuPipeline
        return WuyuPipeline(config)

    def _active_runtime_config(self):
        config = dict(self.ports.app().config)
        if config.get('TESTING') and config.get('WUYU_PIPELINE_FACTORY') and not config.get('WUYU_RUNTIME_SETTINGS_PATH'):
            return config
        from services.wuyu_runtime_settings import freeze_runtime_config, resolve_runtime_snapshot
        return resolve_runtime_snapshot(config, freeze_runtime_config(config))

    def _model_workers(self, job, role):
        policy = getattr(job, '_execution_policy', None)
        if policy is not None:
            return validate_execution_policy(policy, self.ports.app().config)['page_workers']
        config = getattr(job, '_runtime_config', self.ports.app().config)
        return max(1, min(16, int(config.get('WUYU_IMAGE_WORKERS' if role == 'image' else 'WUYU_TRACE_WORKERS') or 8)))

    def _record_batch_timing(self, job, plan, timing, status, performance=None):
        """Durable numeric-only diagnostics; never persist prompts or responses."""
        if not isinstance(plan, dict) or type(plan.get('number')) is not int:
            return
        current = db.session.get(WuyuWorkflowJob, job.id)
        progress = json.loads(current.progress or '{}')
        entry = {key: round(value, 6) for key, value in timing.items()
            if key in {'queue_seconds', 'executor_queue_seconds', 'io_seconds', 'accept_wait_seconds'}
            and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0}
        entry['status'] = status
        safe = self.ports.hook__quality_report({'trace_performance': performance}).get('trace_performance')
        if safe:entry['trace_performance'] = safe
        progress.setdefault('page_timings', {})[str(plan['number'])] = entry
        current.progress = self.ports.hook__json(progress)
        db.session.commit()

    def _model_batch(self, job, plans, prepare, invoke, accept, reject, workers,
                     *, continue_on_error=False, checkpoint=None, quota_kind='image', resource_stage='image'):
        """One durable job owner; model threads never read ORM or mutate state.

        Only the next `workers` pages are submitted. Image work stops scheduling
        after a failure; independent trace pages may continue. Cancel stops all
        new scheduling. Drain in-flight results and commit each success
        immediately. In-flight output files remain immutable after pause/edit;
        epoch checks prevent attaching them to newer versions. Cancellation is
        cooperative before dispatch, not a promise to abort a paid HTTP request.
        """
        pending, failures, page_errors, cancelled = {}, [], [], False
        quota_failure = None
        batch_started = time.monotonic()
        iterator = iter(plans)
        exhausted = False
        stop_dispatch = threading.Event()
        runtime = dict(getattr(job, '_runtime_config', self.ports.app().config))
        def note_failure(plan, error):
            failures.append(error)
            if isinstance(plan, dict) and isinstance(plan.get('number'), int):
                code = self.ports.hook__safe_failure_code(error.code) if isinstance(error, (PipelineError, QuotaError)) else (
                    'WORKFLOW_CONTRACT' if isinstance(error, (WorkflowError, WorkflowAPIError)) else 'PROVIDER_OPERATION_FAILED')
                page_errors.append({'number': plan['number'], 'code': code, 'message': self.ports.hook__failure_message(code)})
        def execute(plan, pipeline, timing):
            began = time.monotonic()
            timing.update(queue_seconds=began-batch_started, executor_queue_seconds=began-timing.pop('submitted'))
            try:
                if stop_dispatch.is_set():
                    raise CancelledWork()
                # Once paid IO begins let it save its response/output. A thread-safe
                # no-op callback avoids SQLAlchemy calls and discarded paid images.
                def pending_cancel():
                    if stop_dispatch.is_set():
                        raise CancelledWork()
                slot_options = {'stage': resource_stage} if resource_stage != 'image' else {}
                with page_slot(self.ports.app(), plan, pending_cancel, getattr(pipeline, 'config', None), **slot_options):
                    return invoke(plan, pipeline, lambda: False)
            finally:
                timing['finished'] = time.monotonic()
                timing['io_seconds'] = timing['finished'] - began
        with page_executor(self.ports.app(), self.ports.hook_ThreadPoolExecutor, workers, stop_dispatch.set) as pool:
            def may_dispatch():
                return not exhausted and (not failures or continue_on_error) and not cancelled and quota_failure is None

            def fill():
                nonlocal cancelled, quota_failure, exhausted
                while len(pending) < pool.window and may_dispatch():
                    permit = pool.try_reserve()
                    if permit is None:
                        return  # Keep accepting completed paid results; never block the owner here.
                    with permit:
                        try:
                            self.ports._check_cancel(job)
                            item = next(iterator)
                        except StopIteration:
                            exhausted = True
                            return
                        except CancelledWork:
                            cancelled = True
                            stop_dispatch.set()
                            return
                        except QuotaError as error:
                            quota_failure=error;stop_dispatch.set();note_failure(None,error);return
                        plan, charge = None, None
                        try:
                            plan = prepare(item)
                            worker_pipeline = self.ports._pipeline(runtime)
                            charge = self.ports.hook_WorkflowCharge(self.ports.app(), job, quota_kind).reserve()
                        except CancelledWork:
                            cancelled = True
                            stop_dispatch.set()
                            return
                        except Exception as error:
                            if charge:
                                charge.refund()
                            note_failure(plan, error)
                            if plan is not None:
                                reject(plan, error)
                            if isinstance(error,QuotaError):quota_failure=error;stop_dispatch.set();return
                            if not continue_on_error:
                                return
                            continue
                        timing = {'submitted': time.monotonic()}
                        try:
                            future = permit.submit(execute, plan, worker_pipeline, timing)
                        except Exception:
                            charge.refund()
                            raise
                        pending[future] = (plan, charge, timing)
            fill()
            while pending or may_dispatch():
                if not cancelled:
                    try:
                        self.ports._check_cancel(job)
                    except CancelledWork:
                        cancelled = True
                        stop_dispatch.set()
                    except QuotaError as error:
                        quota_failure=error;stop_dispatch.set();note_failure(None,error)
                if not pending:
                    pool.wait_for_capacity(.05)
                done, _ = self.ports.hook_wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    plan, charge, timing = pending.pop(future)
                    timing['accept_wait_seconds'] = max(0, time.monotonic()-timing.get('finished', time.monotonic()))
                    output, outcome, diagnostic = None, 'failed', None
                    try:
                        output = future.result()
                        diagnostic = output.get('trace_performance') if isinstance(output, dict) else None
                        if not cancelled:
                            self.ports._check_cancel(job)
                            with charge.accepting(output=output):
                                accept(plan, output)
                            charge.settle()
                            outcome = 'accepted'
                        else:
                            charge.refund('cancelled_before_acceptance')
                            outcome = 'cancelled'
                    except CancelledWork:
                        charge.refund('cancelled_before_acceptance')
                        cancelled = True
                        stop_dispatch.set()
                        outcome = 'cancelled'
                    except Exception as error:
                        diagnostic = getattr(error, 'trace_performance', None)
                        charge.refund()
                        note_failure(plan, error)
                        if not cancelled:
                            reject(plan, error)
                        if isinstance(error,QuotaError):quota_failure=error;stop_dispatch.set()
                    try:self.ports._record_batch_timing(job, plan, timing, outcome, diagnostic)
                    except Exception:
                        # Diagnostics must not turn a durably accepted result
                        # into a failed page or authorize another paid retry.
                        db.session.rollback()
                        self.ports.app().logger.warning('Page timing unavailable')
                # Do not leave a free paid-IO slot idle behind a local export.
                fill()
                if checkpoint:
                    try:
                        checkpoint(cancelled=cancelled)
                    except CancelledWork:
                        # Pause can win after the loop's earlier cancel check.
                        # Keep the pool open so its owner can drain paid IO and
                        # service explicitly requested local exports meanwhile.
                        cancelled = True
                        stop_dispatch.set()
        if cancelled:
            raise CancelledWork()
        if quota_failure:raise quota_failure
        if failures:
            current_job = db.session.get(WuyuWorkflowJob, job.id)
            progress = json.loads(current_job.progress or '{}')
            progress.update(failed_pages=sorted({item['number'] for item in page_errors}),
                            page_errors=sorted(page_errors, key=lambda item: item['number']))
            current_job.progress = self.ports.hook__json(progress)
            db.session.commit()
            raise failures[0]
