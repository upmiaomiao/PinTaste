"""JobExecution: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
import json
import threading
import time
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from models import db, Project, Task
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
from services.wuyu_state import WorkflowError
from services.wuyu_errors import PipelineError, USER_RETRYABLE_TRACE_CODES
from services.wuyu_workflow_quota import current_workflow_actor
from services.wuyu_quota import QuotaError
from . import wuyu_page_retry as page_retry
from .wuyu_page_execution import POLICY_KEY, bind_execution_policy, validate_execution_policy
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError, _UNCAPTURED_CATALOG
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class JobExecutionPorts:
    _check_cancel: Callable[..., Any]
    _continue: Callable[..., Any]
    _export_additional: Callable[..., Any]
    _generate_batch: Callable[..., Any]
    _generate_focused: Callable[..., Any]
    _generate_next: Callable[..., Any]
    _generate_review_images: Callable[..., Any]
    _heartbeat: Callable[..., Any]
    _job_catalog_binding: Callable[..., Any]
    _material_management_guard: Callable[..., Any]
    _mutate: Callable[..., Any]
    _new_job_record: Callable[..., Any]
    _outline: Callable[..., Any]
    _parse_materials: Callable[..., Any]
    _pipeline: Callable[..., Any]
    _prepare_relayout: Callable[..., Any]
    _replan_unlocked: Callable[..., Any]
    _read: Callable[..., Any]
    _reading_failed: Callable[..., Any]
    _reading_run: Callable[..., Any]
    _reading_validate_job: Callable[..., Any]
    _relayout_validate_action: Callable[..., Any]
    _research_validate: Callable[..., Any]
    _reset_inflight: Callable[..., Any]
    _run_research: Callable[..., Any]
    _samples: Callable[..., Any]
    _trace_and_export: Callable[..., Any]
    _terminal_owner: Callable[..., Any]
    _versions: Callable[..., Any]
    app: Callable[..., Any]
    get_job: Callable[..., Any]
    hook__failure_diagnostics: Callable[..., Any]
    hook__failure_message: Callable[..., Any]
    hook__failure_site: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook__safe_failure_code: Callable[..., Any]
    hook_get_quota_service: Callable[..., Any]
    hook_initiating_user: Callable[..., Any]
    hook_phase: Callable[..., Any]
    hook_public_template_choice: Callable[..., Any]
    hook_recover_workflow_charges: Callable[..., Any]
    run_pending: Callable[..., Any]
    snapshot: Callable[..., Any]
    stop_event: Callable[..., Any]



class JobExecution:
    def __init__(self, ports: JobExecutionPorts):
        self.ports = ports

    def _require_native_rebuild(self, project_id, body):
        workflow = body['workflow']
        versions = self.ports._versions(body)
        exported, qa = workflow.get('export') or {}, workflow.get('qa') or {}
        # phase() checks current template approval, page input/template/brief
        # bindings, current image review, traces and the passing QA versions.
        if (self.ports.hook_phase(workflow) != 'exported' or len(workflow['pages']) != body['brief']['page_count']
                or any(exported.get(key) != value for key, value in versions.items())
                or exported.get('qa_run_id') != qa.get('run_id')):
            raise WorkflowAPIError('Rebuild requires reviewed current images, traces and passing current QA')
        if WuyuWorkflowJob.query.filter_by(project_id=project_id).filter(
                WuyuWorkflowJob.status.in_(['queued', 'running'])).first():
            raise WorkflowAPIError('Wait for the current project task before rebuilding', 'WORKFLOW_BUSY', 409)

    def _job_catalog_binding(self, previous=_UNCAPTURED_CATALOG):
        """Capture/resolve only a digest in the server's private template store."""
        from services.wuyu_template_library import capture_catalog_binding, resolve_catalog_binding
        try:
            binding = capture_catalog_binding(self.ports.app().config) if previous is _UNCAPTURED_CATALOG else deepcopy(previous)
            resolve_catalog_binding(self.ports.app().config, binding)
            return binding
        except (ValueError, OSError):
            raise PipelineError('TEMPLATE_BINDING_INVALID', '任务模板版本丢失或校验失败，请由管理员恢复该版本；已有页图与导出保留。') from None

    def _new_job_record(self, project_id, action, payload, body, idempotency_key=None, retry_of=None,
                        *, template_source_job_id=None):
        payload = deepcopy(payload)
        payload.pop(POLICY_KEY, None)  # Never trust client/copied execution policy.
        payload['__runtime_requirements_snapshot'] = {
            'conversation_config_revision': body['brief'].get('conversation_config_revision'),
            'brief': deepcopy(body['brief']), 'input_epoch': body['epoch'],
            'material_revision': body.get('material_revision', 0),
            'materials': [{key: item.get(key) for key in ('id','sha256','role')} for item in body.get('materials', [])
                          if item.get('id') not in body.get('reading_exclusions', {})],
            'template_selection': deepcopy(self.ports.hook_public_template_choice(body)),
        }
        self.ports._material_management_guard(body)
        self.ports._reading_validate_job(body, action, payload)
        self.ports._research_validate(body, action, payload, running=True)
        if action in {'analyze_materials', 'revise_draft'}:
            previous_reading = None
            predecessor_id = retry_of or template_source_job_id
            if predecessor_id:
                predecessor = db.session.get(WuyuWorkflowJob, predecessor_id)
                if not predecessor or predecessor.project_id != project_id or predecessor.action != action:
                    raise WorkflowAPIError('Reading predecessor is not this project action')
                previous_reading = json.loads(predecessor.payload)
            from services.wuyu_runtime_settings import get_research_policy
            payload['__reading_policy'] = deepcopy(previous_reading['__reading_policy']) if previous_reading and '__reading_policy' in previous_reading else get_research_policy(dict(self.ports.app().config))
            payload['__reading_scope_id'] = previous_reading.get('__reading_scope_id', self.ports.hook__id()) if previous_reading else self.ports.hook__id()
        # Neither a client path nor a copied action payload can choose a store
        # version. Retry/resume inheritance must originate in this project's DB.
        payload.pop('__template_catalog_binding', None)
        pure_export = action == 'export' and (payload.get('format', 'editable_pptx') != 'editable_pptx' or payload.get('rebuild') is True)
        if not pure_export and action not in {'_prepare_relayout', '_parse_materials', 'analyze_materials', 'revise_draft', 'run_research'}:
            bind_execution_policy(payload, self.ports.app().config)
        if not pure_export and action not in {'_prepare_relayout', '_parse_materials', 'analyze_materials', 'revise_draft', 'run_research'}:
            predecessor_id = retry_of or template_source_job_id
            previous_binding = _UNCAPTURED_CATALOG
            if predecessor_id:
                predecessor = db.session.get(WuyuWorkflowJob, predecessor_id)
                if not predecessor or predecessor.project_id != project_id or predecessor.action != action:
                    raise WorkflowAPIError('Template predecessor is not this project action')
                previous_payload = json.loads(predecessor.payload)
                if '__template_catalog_binding' in previous_payload:
                    previous_binding = previous_payload['__template_catalog_binding']
            try:payload['__template_catalog_binding'] = self.ports._job_catalog_binding(previous_binding)
            except PipelineError:
                raise WorkflowAPIError('The server template version requires administrator repair', 'TEMPLATE_BINDING_INVALID', 503) from None
        if action != '_prepare_relayout' and not pure_export and '__runtime_snapshot' not in payload and not (self.ports.app().config.get('TESTING') and self.ports.app().config.get('WUYU_PIPELINE_FACTORY') and not self.ports.app().config.get('WUYU_RUNTIME_SETTINGS_PATH')):
            from services.wuyu_runtime_settings import freeze_runtime_config
            try:
                payload['__runtime_snapshot'] = freeze_runtime_config(dict(self.ports.app().config), profile_id=body['brief'].get('image_profile', 'default'))
            except (ValueError, OSError):
                raise WorkflowAPIError('Runtime model settings require administrator repair', 'RUNTIME_SETTINGS_INVALID', 503) from None
        memory = self.ports.app().extensions.get('wuyu_design_memory')
        if memory and '__design_context' not in payload:
            owner_id = db.session.get(Project, project_id).user_id
            payload['__design_context'] = memory.retrieve(owner_id, self.ports.hook__json(body['brief']))
            habits = self.ports.app().extensions.get('wuyu_design_habits')
            if habits:
                try:
                    # Same transaction as the job; a log that cannot be written must not block job creation.
                    used = [rule.get('id') for rule in payload['__design_context'].get('rules', []) if isinstance(rule, dict) and isinstance(rule.get('id'), str)]
                    habits.log_injection(owner_id, project_id,
                        [value[len('habit:'):] for value in used if value.startswith('habit:')],
                        [value for value in used if not value.startswith('habit:')])
                except SQLAlchemyError:
                    raise  # The transaction is no longer usable; let the caller's handling see the real error.
                except Exception as exc:
                    self.ports.app().logger.warning('Wuyu design injection log skipped: project=%s type=%s', project_id, type(exc).__name__)
        task = Task(project_id=project_id, task_type='WUYU_WORKFLOW', status='PENDING')
        task.set_progress({'total': body['brief']['page_count'], 'completed': 0, 'failed': 0})
        db.session.add(task)
        db.session.flush()
        job = WuyuWorkflowJob(project_id=project_id, task_id=task.id, action=action, payload=self.ports.hook__json(payload),
                              input_epoch=body['epoch'], idempotency_key=idempotency_key, retry_of=retry_of,
                              actor_user_id=self.ports.hook_initiating_user())
        db.session.add(job)
        db.session.flush()
        return job.id

    def _enqueue_response(self, project_id, action, payload, idempotency_key=None, retry_of=None):
        job_id, _ = self.ports._mutate(project_id, lambda body: self.ports._new_job_record(
            project_id, action, payload, body, idempotency_key, retry_of))
        if self.ports.app().config.get('WUYU_WORKFLOW_INLINE'):
            self.ports.run_pending(project_id)
        result = self.ports.snapshot(project_id)
        job = db.session.get(WuyuWorkflowJob, job_id)
        result.update(task_id=job.task_id, job=self.ports.get_job(project_id, job.id))
        return result

    def _check_cancel(self, job):
        db.session.expire_all()
        current = db.session.get(WuyuWorkflowJob, job.id)
        _, body = self.ports._read(job.project_id)
        image_export = job.action == 'export' and json.loads(job.payload).get('format', 'editable_pptx') != 'editable_pptx'
        paused_blocks = body['paused'] and not image_export
        if self.ports.stop_event().is_set() or not current or current.cancel_requested or paused_blocks or body['epoch'] != job.input_epoch:
            raise CancelledWork()
        local_export = job.action=='export'and(image_export or json.loads(job.payload).get('rebuild')is True)
        now=time.monotonic()
        if (not local_export and self.ports.app().config.get('WUYU_QUOTA_ENABLED',True)and job.actor_user_id
                and now-getattr(job,'_quota_checked_at',0) >= .5):
            self.ports.hook_get_quota_service(self.ports.app()).assert_work_allowed(job.actor_user_id)
            job._quota_checked_at=now
        return False

    def _run(self, job_id):
        owner = self.ports._terminal_owner()
        if not owner.begin_execution(job_id):
            return  # A local claim enters business execution at most once.
        try:
            self._run_execution(job_id)
        except BaseException:
            owner.mark_unknown(job_id, 'TERMINAL_EXECUTION_CONTEXT_UNRESOLVED')
            self.ports.app().logger.warning('Wuyu terminal execution context unresolved: job=%s', job_id)
            raise

    def _run_execution(self, job_id):
        job = db.session.get(WuyuWorkflowJob, job_id)
        if job is None:
            raise RuntimeError('TERMINAL_JOB_MISSING')
        owner = self.ports._terminal_owner()
        if not owner.bind_execution(job):
            return
        actor_context = current_workflow_actor.set(job.actor_user_id)
        from services.wuyu_design_memory import current_design_context
        design_context = current_design_context.set(json.loads(job.payload).get('__design_context'))
        status, error, error_details = None, None, None
        heartbeat_done = threading.Event()
        heartbeat = threading.Thread(target=self.ports._heartbeat, args=(job_id, heartbeat_done), daemon=True,
                                     name='wuyu-job-heartbeat')
        heartbeat.start()
        try:
            self.ports._check_cancel(job)
            payload = json.loads(job.payload)
            job._execution_policy = (validate_execution_policy(payload[POLICY_KEY], self.ports.app().config)
                                     if POLICY_KEY in payload else None)
            _, reading_body = self.ports._read(job.project_id)
            self.ports._relayout_validate_action(reading_body, job.action, payload)
            self.ports._reading_validate_job(reading_body, job.action, payload, running=True)
            self.ports._research_validate(reading_body, job.action, payload, running=True)
            retry_number = (page_retry.validate_page_retry(job, reading_body)
                if page_retry.PAGE_RETRY_SCOPE_KEY in payload else None)
            pure_export = job.action == 'export' and (payload.get('format', 'editable_pptx') != 'editable_pptx' or payload.get('rebuild') is True)
            if job.action != '_prepare_relayout' and not pure_export and not payload.get('__runtime_snapshot') and not (self.ports.app().config.get('TESTING') and self.ports.app().config.get('WUYU_PIPELINE_FACTORY')):
                from services.wuyu_runtime_settings import freeze_runtime_config
                _, frozen_body = self.ports._read(job.project_id)
                payload['__runtime_snapshot'] = freeze_runtime_config(dict(self.ports.app().config), profile_id=frozen_body['brief'].get('image_profile', 'default'))
                job.payload = self.ports.hook__json(payload)
                owner.allow_own_payload(job_id, job.payload)
                db.session.commit()
            if not pure_export and payload.get('__runtime_snapshot'):
                from services.wuyu_runtime_settings import resolve_runtime_snapshot
                try:
                    job._runtime_config = resolve_runtime_snapshot(dict(self.ports.app().config), payload['__runtime_snapshot'])
                except (ValueError, OSError):
                    raise PipelineError('runtime_settings_invalid', '任务模型配置版本需要管理员恢复。') from None
            else:
                job._runtime_config = dict(self.ports.app().config)
            if not pure_export and job.action not in {'_prepare_relayout', '_parse_materials', 'analyze_materials', 'revise_draft', 'run_research'}:
                if '__template_catalog_binding' not in payload:
                    # Legacy jobs can freeze once when first run. This is not a
                    # claim that their pre-release enqueue version was captured.
                    payload['__template_catalog_binding'] = self.ports._job_catalog_binding()
                    job.payload = self.ports.hook__json(payload)
                    owner.allow_own_payload(job_id, job.payload)
                    db.session.commit()
                job._runtime_config['WUYU_TEMPLATE_CATALOG_BINDING'] = self.ports._job_catalog_binding(payload['__template_catalog_binding'])
            pipeline = None if job.action == '_prepare_relayout' else self.ports._pipeline(job._runtime_config)
            if retry_number is not None:
                # A page retry never enters bulk continuation, planning, tracing or export.
                self.ports._generate_batch(job, numbers=[retry_number])
            elif job.action == '_prepare_relayout':
                self.ports._prepare_relayout(job)
            elif job.action == '_parse_materials':
                self.ports._parse_materials(job, pipeline, payload)
            elif job.action == 'run_research':
                self.ports._run_research(job, pipeline)
            elif job.action in {'analyze_materials', 'revise_draft'}:
                self.ports._reading_run(job, pipeline)
            elif job.action == 'generate_outline':
                self.ports._outline(job, pipeline)
            elif job.action == 'generate_focused':
                self.ports._generate_focused(job, pipeline)
            elif job.action == 'generate_images':
                self.ports._generate_review_images(job, pipeline)
            elif job.action in {'generate_samples', 'regenerate_samples'}:
                self.ports._samples(job, pipeline)
            elif job.action == 'generate_next':
                _, current = self.ports._read(job.project_id)
                if current['workflow']['review_policy'] == 'guided':
                    self.ports._generate_next(job, pipeline)
                else:
                    self.ports._continue(job, pipeline)
            elif job.action == 'start_tracing':
                self.ports._trace_and_export(job, pipeline)
            elif job.action == 'export':
                if payload.get('format', 'editable_pptx') == 'editable_pptx':
                    self.ports._trace_and_export(job, pipeline, rebuild=payload.get('rebuild') is True)
                else:
                    self.ports._export_additional(job, pipeline, payload['format'])
            elif job.action == '_continue':
                self.ports._continue(job, pipeline)
            elif job.action == '_replan_unlocked':
                self.ports._replan_unlocked(job, pipeline)
            else:
                raise WorkflowAPIError('Unknown queued action')
            status = 'completed'
        except CancelledWork:
            status, error = 'cancelled', 'CANCELLED_OR_INPUT_CHANGED'
        except Exception as exc:
            status, error = 'failed', (self.ports.hook__safe_failure_code(exc.code) if isinstance(exc, (PipelineError, QuotaError)) else
                'WORKFLOW_CONTRACT' if isinstance(exc, (WorkflowError, WorkflowAPIError)) else 'PROVIDER_OPERATION_FAILED')
            error_details = {'code': error, 'message': self.ports.hook__failure_message(error),
                             'retryable': getattr(exc, 'retryable', False) is True or error in USER_RETRYABLE_TRACE_CODES,
                             'diagnostics': self.ports.hook__failure_diagnostics(getattr(exc, 'diagnostics', {}))}
            self.ports.app().logger.warning('Wuyu workflow job failed: job=%s code=%s type=%s', job_id, error, type(exc).__name__)
            if error == 'PROVIDER_OPERATION_FAILED' and not isinstance(exc, (PipelineError, QuotaError)):
                # An exception nobody planned for. Only its type used to be logged, so a
                # KeyError in production could not be located at all. Log WHERE it happened
                # (file, line, function) and never WHAT it said: an exception message can
                # hold user material or a credential.
                self.ports.app().logger.warning('Wuyu workflow job unexpected failure site: job=%s %s',
                                        job_id, self.ports.hook__failure_site(exc))
            if error_details['diagnostics']:
                self.ports.app().logger.warning('Wuyu workflow safe diagnostics: job=%s metadata=%s', job_id, self.ports.hook__json(error_details['diagnostics']))
        finally:
            # Freeze before context/reset/rollback/join: none of those failures may erase a result.
            if status is not None:
                owner.freeze_outcome(job_id, status, error, error_details)
            else:
                owner.mark_unknown(job_id, 'TERMINAL_EXECUTION_OUTCOME_UNKNOWN')
            cleanup_error = None
            for cleanup in (lambda: current_workflow_actor.reset(actor_context),
                            lambda: current_design_context.reset(design_context),
                            heartbeat_done.set, db.session.rollback,
                            lambda: heartbeat.join(timeout=6)):
                try:
                    cleanup()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    self.ports.app().logger.warning('Wuyu terminal local cleanup pending: job=%s', job_id)
            if status is not None:
                owner.attempt(job_id)
            if cleanup_error is not None:
                raise cleanup_error

    def _progress(self, job, stage, body):
        progress = {'stage': stage, 'total': len(body['workflow']['pages']),
                    'completed': sum(p['generation_status'] == 'ready' for p in body['workflow']['pages']),
                    'failed': sum(p['generation_status'] == 'failed' for p in body['workflow']['pages']),
                    'traced': sum(bool(p['trace']) and p['trace']['source_image_version'] == p['image_version'] for p in body['workflow']['pages'])}
        current = db.session.get(WuyuWorkflowJob, job.id)
        previous = json.loads(current.progress or '{}')
        for key in ('quota_receipts', 'page_timings', 'export_timings'):
            if previous.get(key):progress[key] = previous[key]
        current.progress = self.ports.hook__json(progress)
        task = db.session.get(Task, current.task_id)
        if task:
            task.set_progress(progress)
        db.session.commit()
