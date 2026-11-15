"""ActionCoordinator: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_state import WorkflowError
from services.wuyu_errors import PipelineError
from . import wuyu_workflow_lifecycle as lifecycle
from . import wuyu_page_retry as page_retry
from . import wuyu_action_state as action_state
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class ActionCoordinatorPorts:
    ACTIONS: Callable[..., Any]
    ASYNC_ACTIONS: Callable[..., Any]
    EXPORT_FORMATS: Callable[..., Any]
    _action_plan_dependencies: Callable[..., Any]
    _action_state_dependencies: Callable[..., Any]
    _artifact_file: Callable[..., Any]
    _cancel_old_jobs: Callable[..., Any]
    _close_inflight: Callable[..., Any]
    _default_recipe: Callable[..., Any]
    _job_dict: Callable[..., Any]
    _material_management_guard: Callable[..., Any]
    _mutate: Callable[..., Any]
    _new_job_record: Callable[..., Any]
    _new_state: Callable[..., Any]
    _normalize_recipe: Callable[..., Any]
    _read: Callable[..., Any]
    _reading_apply_action: Callable[..., Any]
    _reading_invalidate: Callable[..., Any]
    _reading_queue_action: Callable[..., Any]
    _reading_replan: Callable[..., Any]
    _reading_validate_action: Callable[..., Any]
    _relayout_validate_action: Callable[..., Any]
    _require_current_reading_replay: Callable[..., Any]
    _require_native_rebuild: Callable[..., Any]
    _require_template_choice: Callable[..., Any]
    _require_template_confirmation_ui: Callable[..., Any]
    _research_apply: Callable[..., Any]
    _research_ui: Callable[..., Any]
    _research_validate: Callable[..., Any]
    _restore_template_choice: Callable[..., Any]
    _start_armed_research: Callable[..., Any]
    _sync_page: Callable[..., Any]
    _validate_page_revision_payload: Callable[..., Any]
    app: Callable[..., Any]
    hook__failure_message: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook__now: Callable[..., Any]
    hook_apply_event: Callable[..., Any]
    hook_current_relayout_source: Callable[..., Any]
    hook_current_template_choice: Callable[..., Any]
    hook_get_quota_service: Callable[..., Any]
    hook_initiating_user: Callable[..., Any]
    hook_phase: Callable[..., Any]
    run_pending: Callable[..., Any]
    snapshot: Callable[..., Any]



class ActionCoordinator:
    def __init__(self, ports: ActionCoordinatorPorts):
        self.ports = ports

    @staticmethod
    def _validate_page_revision_payload(payload, page_count=None):
        """Reject ambiguous page edits before quota checks, state or job writes."""
        invalid = (not isinstance(payload, dict) or set(payload) - {'number', 'feedback', 'change', 'copy'}
                   or type(payload.get('number')) is not int or not 1 <= payload['number'] <= 80)
        if not invalid and page_count is not None:
            invalid = payload['number'] > page_count
        if not invalid:
            changes = [key for key in ('feedback', 'change') if key in payload]
            invalid = (len(changes) != 1 or not isinstance(payload[changes[0]], str)
                       or not payload[changes[0]].strip() or len(payload[changes[0]]) > 12000)
        if not invalid and 'copy' in payload:
            invalid = (not isinstance(payload['copy'], str) or not payload['copy'].strip()
                       or len(payload['copy']) > 30000)
        if invalid:
            raise WorkflowAPIError('Page revision requires an existing one-based number and exactly one nonempty feedback or change; copy is optional.',
                                   'PAGE_REVISION_INPUT_INVALID', 400)

    def execute_action(self, project_id, action, payload=None, expected_revision=None, idempotency_key=None):
        payload = payload or {}
        if action not in self.ports.ACTIONS():
            raise WorkflowAPIError('Unknown business action')
        if action == 'request_research' and not payload:
            self.ports._relayout_validate_action(self.ports._read(project_id)[1], action, payload)
            started = self.ports._start_armed_research(project_id, idempotency_key)
            if started is not None:
                return started
        if action == 'revise_page':
            self.ports._validate_page_revision_payload(payload)
        if action == 'confirm_template_choice':
            self.ports._require_template_confirmation_ui(project_id)
        if action in {'confirm_research', 'accept_research'}:
            self.ports._research_ui(project_id)
        if any(str(key).startswith(('__runtime', '__template', '__reading', '__design')) for key in payload):
            raise WorkflowAPIError('Runtime and template configuration are server controlled')
        if expected_revision is not None and (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)):
            raise WorkflowAPIError('expected_revision must be an integer')
        if len(self.ports.hook__json(payload)) > 250000:
            raise WorkflowAPIError('Action payload is too large', status=413)
        if any(key.lower() in {'api_key', 'token', 'password', 'secret', 'authorization'} for key in payload):
            raise WorkflowAPIError('Provider secrets must use server configuration')
        if action == 'update_brief' and 'brief' in payload:
            if set(payload) - {'brief', 'review_policy', 'preserve_outline', 'feedback'} or not isinstance(payload['brief'], dict):
                raise WorkflowAPIError('brief must be an object')
            if 'preserve_outline' in payload['brief']:
                raise WorkflowAPIError('preserve_outline must be outside brief')
            payload = dict(payload['brief']) | {key: payload[key] for key in ['review_policy', 'preserve_outline', 'feedback'] if key in payload}
        if action == 'update_brief' and 'preserve_outline' in payload and not isinstance(payload['preserve_outline'], bool):
            raise WorkflowAPIError('preserve_outline must be a boolean')
        if 'feedback' in payload and (not isinstance(payload['feedback'], str) or not payload['feedback'].strip() or len(payload['feedback']) > 12000):
            raise WorkflowAPIError('feedback must be nonempty text up to 12000 characters')
        if action == 'select_references':
            if 'reference_recipe' in payload:
                if set(payload) != {'reference_recipe'} or not isinstance(payload['reference_recipe'], dict):
                    raise WorkflowAPIError('reference_recipe must be an object')
                payload = payload['reference_recipe']
            payload = self.ports._normalize_recipe(payload)
        revision, before = self.ports._read(project_id)
        if action not in {'cancel', 'pause'}:
            self.ports._material_management_guard(before)
        if action == 'confirm_template' and before['workflow'].get('requires_final_image_review') is True:
            raise WorkflowAPIError('请在模板预览中点击确认使用，不能用旧样片确认替代模板选择。',
                                   'TEMPLATE_CONFIRMATION_UI_REQUIRED', 403)
        if idempotency_key:
            if not isinstance(idempotency_key, str) or len(idempotency_key) > 120:
                raise WorkflowAPIError('Invalid request ID')
            fingerprint = hashlib.sha256(self.ports.hook__json({'action': action, 'payload': payload}).encode()).hexdigest()
            prior = before['request_ids'].get(idempotency_key)
            if prior:
                if prior != fingerprint:
                    raise WorkflowAPIError('Request ID was reused with different input', 'IDEMPOTENCY_CONFLICT', 409)
                job = WuyuWorkflowJob.query.filter_by(project_id=project_id, idempotency_key=idempotency_key).first()
                self.ports._require_current_reading_replay(job, before)
                snapshot = self.ports.snapshot(project_id)
                if job:
                    snapshot.update(task_id=job.task_id, job=self.ports._job_dict(job))
                return snapshot
        if expected_revision is not None and revision != expected_revision:
            raise WorkflowAPIError('Workflow changed; refresh before retrying', 'REVISION_CONFLICT', 409)
        self.ports._relayout_validate_action(before, action, payload)
        if action == 'revise_page':
            self.ports._validate_page_revision_payload(payload, len(before['workflow']['pages']))
        if action in {'generate_samples', 'regenerate_samples'}:
            from .wuyu_page_lock import reject_when_locked
            reject_when_locked(before, action, self.ports._action_state_dependencies())
        if action in {'lock_pages', 'regenerate_unlocked'}:
            from .wuyu_page_lock import validate_lock_payload, validate_regenerate_payload
            deps = self.ports._action_state_dependencies()
            if action == 'lock_pages':
                validate_lock_payload(before, payload, deps)
            else:
                validate_regenerate_payload(before, payload, deps, self.ports.hook_phase)
                if WuyuWorkflowJob.query.filter_by(project_id=project_id).filter(
                        WuyuWorkflowJob.status.in_(['queued', 'running'])).first():
                    raise WorkflowAPIError('请等待当前项目任务结束后再重新生成未锁定页。', 'WORKFLOW_BUSY', 409)
        if ((action in self.ports.ASYNC_ACTIONS() | {'retry','resume','continue_from_draft','confirm_template','confirm_images','confirm_page','regenerate_unlocked'}
                or action in {'confirm_research', 'supplement_research'} and payload.get('mode') in {'web', 'deep'})
                and not(action=='export'and(payload.get('format','editable_pptx')!='editable_pptx'or payload.get('rebuild')is True))
                and self.ports.app().config.get('WUYU_QUOTA_ENABLED',True) and self.ports.hook_initiating_user()):
            self.ports.hook_get_quota_service(self.ports.app()).assert_work_allowed(self.ports.hook_initiating_user())
        stage = self.ports.hook_phase(before['workflow'])
        self.ports._reading_validate_action(before, action, payload)
        self.ports._research_validate(before, action, payload)
        if action in {'generate_samples', 'regenerate_samples', 'generate_next', 'generate_focused', 'generate_images'} and self.ports._reading_queue_action(before, action) != 'analyze_materials':
            self.ports._require_template_choice(before)
        if action == 'generate_images' and payload != {'generation_requested': True}:
            raise WorkflowAPIError('Image generation needs explicit generation_requested=true')
        focused = before['brief'].get('creation_kind', 'deck') != 'deck'
        if action == 'generate_focused' and (not focused or payload != {'generation_requested': True}):
            raise WorkflowAPIError('Focused generation requires a pages/asset brief and explicit generation_requested=true')
        if focused and action in {'generate_samples', 'regenerate_samples', 'confirm_template'}:
            raise WorkflowAPIError('Focused generation uses the requested content directly, without sample approval')
        if action == 'generate_next' and stage != 'page_generation':
            raise WorkflowAPIError('Confirm the current template/page before generating another')
        if action == 'start_tracing' and stage != 'tracing':
            raise WorkflowAPIError('All current images must be reviewed before tracing')
        export_format = payload.get('format', 'editable_pptx') if action == 'export' else None
        native_conversion = action == 'export' and export_format == 'editable_pptx' and before['brief'].get('output_preference') == 'image'
        rebuilding = action == 'export' and payload.get('rebuild') is True
        if action == 'export':
            if set(payload) - {'format', 'rebuild'}:
                raise WorkflowAPIError('Unknown export field')
            if 'rebuild' in payload and (type(payload['rebuild']) is not bool or export_format != 'editable_pptx'):
                raise WorkflowAPIError('rebuild must be a boolean for editable_pptx only')
        if action == 'export' and export_format not in self.ports.EXPORT_FORMATS():
            raise WorkflowAPIError('Unsupported export format')
        if action == 'export' and stage not in {'tracing', 'quality_check', 'ready_to_export', 'exported'}:
            raise WorkflowAPIError('The current deck is not ready for editable export')
        if rebuilding:
            self.ports._require_native_rebuild(project_id, before)
        if action == 'export' and stage == 'exported' and export_format == 'editable_pptx' and not rebuilding and not native_conversion:
            return self.ports.snapshot(project_id)
        retry_job = None
        if action == 'retry' and 'number' not in payload:
            query = WuyuWorkflowJob.query.filter_by(project_id=project_id).filter(
                WuyuWorkflowJob.status.in_(['failed', 'interrupted', 'cancelled']))
            if payload.get('job_id'):
                query = query.filter_by(id=payload['job_id'])
            elif payload.get('task_id'):
                query = query.filter_by(task_id=payload['task_id'])
            else:
                # op 176: without an ID, retry means the project's most recent job, and only
                # when that job itself failed/was interrupted (never an older, superseded failure).
                # The assistant often sends {} after the user says "重试"; explicit IDs keep working.
                query = WuyuWorkflowJob.query.filter_by(project_id=project_id)
            old = query.order_by(WuyuWorkflowJob.created_at.desc()).first()
            if not old or old.status not in {'failed', 'interrupted', 'cancelled'}:
                raise WorkflowAPIError('Only a failed or interrupted job can be retried')
            retry_job = old
        def mutate(body):
            if idempotency_key and idempotency_key in body['request_ids']:
                if body['request_ids'][idempotency_key] != fingerprint:
                    raise WorkflowAPIError('Request ID was reused with different input', 'IDEMPOTENCY_CONFLICT', 409)
                existing = WuyuWorkflowJob.query.filter_by(project_id=project_id, idempotency_key=idempotency_key).first()
                self.ports._require_current_reading_replay(existing, body)
                return existing.id if existing else None
            retry_source, retry_scope = retry_job, None
            if action == 'retry' and ('number' in payload or
                    retry_job and page_retry.PAGE_RETRY_SCOPE_KEY in json.loads(retry_job.payload)):
                try:
                    retry_source, retry_scope = page_retry.prepare_page_retry(db, project_id, body, payload)
                except PipelineError as exc:
                    raise WorkflowAPIError(self.ports.hook__failure_message(exc.code), str(exc.code).upper(),
                        400 if exc.code == 'page_retry_invalid' else 409) from None
            old_epoch = body['epoch']
            old_brief = deepcopy(body['brief'])
            if action == 'revise_page':
                self.ports._validate_page_revision_payload(payload, len(body['workflow']['pages']))
            self.ports._relayout_validate_action(body, action, payload)
            if action == 'update_brief' and payload.get('task_mode', old_brief.get('task_mode', 'create')) != old_brief.get('task_mode', 'create'):
                if body.get('relayout'):
                    body.setdefault('relayout_history', []).append(deepcopy(body.pop('relayout')))
            self.ports._reading_validate_action(body, action, payload)
            self.ports._research_validate(body, action, payload)
            if action in {'generate_samples', 'regenerate_samples', 'generate_next', 'generate_focused', 'generate_images'} and self.ports._reading_queue_action(body, action) != 'analyze_materials':
                self.ports._require_template_choice(body)
            if rebuilding:
                # Recheck within the CAS transaction: two independent requests
                # cannot both enqueue a rebuild of one project/version set.
                self.ports._require_native_rebuild(project_id, body)
            if native_conversion:
                body['workflow'] = self.ports.hook_apply_event(body['workflow'], {'event_id': self.ports.hook__id(),
                    'type': 'output_preference.changed', 'payload': {'output_preference': 'editable'}})
                body['brief']['output_preference'] = 'editable'
            if (action in {'generate_outline', 'generate_samples', 'regenerate_samples', 'generate_focused', 'continue_from_draft'}
                    and self.ports._reading_queue_action(body, action) != 'analyze_materials'
                    and not body['reference_recipe'].get('style_anchor')):
                body['reference_recipe'] = self.ports._default_recipe(body)
                body['workflow'] = self.ports._new_state(project_id, body['brief'], body['workflow']['review_policy'])
                body['epoch'] += 1
                body['history'].append({'at': self.ports.hook__now(), 'action': 'default_reference_selected', 'status': 'accepted'})
            pending_resume = None
            if action in {'pause', 'cancel'}:
                pending = WuyuWorkflowJob.query.filter(
                    WuyuWorkflowJob.project_id == project_id,
                    WuyuWorkflowJob.input_epoch == body['epoch'],
                    WuyuWorkflowJob.status.in_(['running', 'queued'])).order_by(WuyuWorkflowJob.created_at).first()
                if pending:
                    pending_resume = {'action': pending.action, 'payload': json.loads(pending.payload),
                                             'template_source_job_id': pending.id}
            locks_before = deepcopy(body['workflow'])
            action_state.apply_action_state(body, action, payload, project_id=project_id,
                pending_resume=pending_resume, deps=self.ports._action_state_dependencies())
            self.ports._reading_apply_action(body, action, payload)
            self.ports._research_apply(body, action, payload)
            if body['workflow']['template']['id'] is None and self.ports.hook_current_template_choice(body):
                self.ports._restore_template_choice(body)
            from .wuyu_page_lock import assert_locks_preserved
            assert_locks_preserved(locks_before, body['workflow'], action, self.ports._action_state_dependencies())
            changed_reading_keys = {key for key in (
                'task_mode','topic','goal','requirements','audience','scenario','subscenario','page_count','creation_kind','asset_kind','language','duration')
                if body['brief'].get(key) != old_brief.get(key)}
            if action == 'update_brief' and not self.ports.hook_current_relayout_source(body) and changed_reading_keys:
                count_kind_only = (changed_reading_keys <= {'page_count', 'language', 'creation_kind'}
                    and 'page_count' in changed_reading_keys
                    and old_brief.get('creation_kind', 'deck') in {'deck', 'pages'}
                    and body['brief'].get('creation_kind', 'deck') in {'deck', 'pages'})
                if changed_reading_keys <= {'page_count', 'language'} or count_kind_only:
                    self.ports._reading_replan(body, old_brief)
                else:
                    self.ports._reading_invalidate(body)
            lifecycle.prepare_generation_start(body, action, old_epoch)
            if rebuilding and body['paused']:
                body['resume_action'] = {'action': 'export', 'payload': deepcopy(payload)}
            if idempotency_key:
                body['request_ids'][idempotency_key] = fingerprint
            memory = self.ports.app().extensions.get('wuyu_design_memory')
            if memory:
                memory.record(body, action, payload, idempotency_key or self.ports.hook__id())
            body['history'].append({'at': self.ports.hook__now(), 'action': action, 'status': 'accepted'})
            if body['epoch'] != old_epoch:
                research = body.get('research') or {}
                if (research.get('status') in {'queued', 'running'} and research.get('mode') in {'web', 'deep'}
                        and action not in {'confirm_research', 'supplement_research', 'resume', 'retry'}):
                    # op177: the running round is cancelled below; never leave it labelled running.
                    research['status'] = 'paused'
                    research.setdefault('progress', {})['stage'] = 'paused'
                self.ports._close_inflight(body, 'Inputs changed or processing was paused; retry is available')
                # Cancellation and the new version commit atomically; an old
                # provider callback cannot attach itself to the changed inputs.
                self.ports._cancel_old_jobs(project_id)
            retry_plan_source = (action_state.JobRetrySource(retry_source.action, retry_source.payload, retry_source.id)
                if retry_source else None)
            intent = action_state.plan_action_job(body, action, payload, retry_source=retry_plan_source,
                retry_scope=retry_scope, deps=self.ports._action_plan_dependencies())
            if intent.action:
                if action == 'resume' and intent.action == 'export' and intent.payload.get('rebuild') is True:
                    existing = WuyuWorkflowJob.query.filter_by(project_id=project_id,
                        input_epoch=body['epoch'], action='export').filter(
                        WuyuWorkflowJob.status.in_(['queued', 'running'])).order_by(WuyuWorkflowJob.created_at).all()
                    matching = next((job for job in existing if {
                        key: value for key, value in json.loads(job.payload).items() if not key.startswith('__runtime')
                    } == {key: value for key, value in intent.payload.items() if not key.startswith('__runtime')}), None)
                    if matching:
                        return matching.id
                if action == 'resume' and intent.action == '_continue':
                    existing = WuyuWorkflowJob.query.filter_by(project_id=project_id,
                        input_epoch=body['epoch'], action='_continue').filter(
                        WuyuWorkflowJob.status.in_(['queued', 'running'])).first()
                    if existing:
                        # A page revision made while paused already queued the
                        # current continuation. Resume it once, rather than
                        # leaving a second job that could retry a paid failure.
                        return existing.id
                return self.ports._new_job_record(project_id, intent.action, intent.payload, body,
                                            idempotency_key, intent.retry_of, template_source_job_id=intent.template_source_job_id)
        try:
            job_id, changed_body = self.ports._mutate(project_id, mutate, expected_revision=expected_revision,
                                               replay=(idempotency_key, fingerprint) if idempotency_key else None)
        except WorkflowError as exc:
            raise WorkflowAPIError(str(exc)) from exc
        except PipelineError as exc:
            # op177: business validation raised inside the CAS/job transaction
            # (e.g. RESEARCH_REVIEW_REQUIRED) is a 409, never an HTTP 500.
            raise WorkflowAPIError(exc.message, str(exc.code).upper(), 409) from None
        if action == 'confirm_template':
            body = changed_body
            try:
                for number in [1, 2]:
                    page = body['workflow']['pages'][number - 1]
                    if not page['artifact']:
                        raise CancelledWork()
                    self.ports._sync_page(project_id, number, body['slide_specs'][number - 1],
                                    self.ports._artifact_file(body, page['artifact']['image_uri']), epoch=body['epoch'])
            except CancelledWork:
                # Another accepted edit won after this confirmation. Return its
                # latest snapshot instead of reporting a false server failure.
                pass
        if job_id:
            if self.ports.app().config.get('WUYU_WORKFLOW_INLINE'):
                self.ports.run_pending(project_id)
            result = self.ports.snapshot(project_id)
            job = db.session.get(WuyuWorkflowJob, job_id)
            result.update(task_id=job.task_id, job=self.ports._job_dict(job))
            return result
        return self.ports.snapshot(project_id)
