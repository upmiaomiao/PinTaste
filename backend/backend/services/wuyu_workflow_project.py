"""ProjectState: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from datetime import datetime
import json
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from models import db, Project, Task
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_workflow_quota import current_workflow_charge
from . import wuyu_page_retry as page_retry
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class ProjectStatePorts:
    _close_inflight: Callable[..., Any]
    _default: Callable[..., Any]
    _ensure: Callable[..., Any]
    _mutate: Callable[..., Any]
    _new_state: Callable[..., Any]
    _read: Callable[..., Any]
    _reading_protocol_enabled: Callable[..., Any]
    _require_current_reading_replay: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook_apply_event: Callable[..., Any]
    hook_new_state: Callable[..., Any]



class ProjectState:
    def __init__(self, ports: ProjectStatePorts):
        self.ports = ports

    def _default(self, project):
        from .wuyu_delivery_names import INTAKE_TITLE
        idea = (project.idea_prompt or '').strip()
        # The Work host opens a project with a fixed intake instruction before
        # the user has said anything. It is a UI placeholder, never a topic.
        topic = '' if INTAKE_TITLE.search(idea) else idea
        brief = {'task_mode': 'create', 'scenario': 'commercial', 'subscenario': 'technical_proposal', 'audience': '企业客户', 'page_count': 12,
                 'style': '蓝白专业方案风：白底留白、清晰标题层级、少字、细线图标与内容驱动图示，避免论文平铺和密集段落', 'topic': topic or ('' if INTAKE_TITLE.search((project.project_title or '').strip()) else project.project_title or ''),
                 'goal': '', 'requirements': project.extra_requirements or ''}
        state = self.ports._new_state(project.id, brief, 'template_and_final')
        return {'workflow': state, 'brief': brief, 'reference_recipe': {'style_anchor': None, 'references': []},
                'planning_report': {},
                'outline': [], 'slide_specs': [], 'artifacts': [], 'materials': [], 'history': [],
                'epoch': 0, 'paused': False, 'request_ids': {},
                **({'reading_protocol': 1, 'material_revision': 0, 'reading': None}
                   if self.ports._reading_protocol_enabled() else {})}

    def _new_state(self, project_id, brief, policy):
        state = self.ports.hook_new_state(project_id, 'guided' if policy == 'guided' else 'batch', policy,
                          creation_kind=brief.get('creation_kind', 'deck'),
                          output_preference=brief.get('output_preference', 'editable'))
        for key in state['requirements']['steps']:
            state = self.ports.hook_apply_event(state, {'event_id': self.ports.hook__id(), 'type': 'requirement.selected',
                                       'payload': {'key': key, 'value': brief[key]}})
        return state

    def _ensure(self, project_id):
        row = db.session.get(WuyuWorkflow, project_id)
        if row:
            return row
        project = db.session.get(Project, project_id)
        if not project:
            raise WorkflowAPIError('Project not found', 'PROJECT_NOT_FOUND', 404)
        row = WuyuWorkflow(project_id=project_id, revision=0, body=self.ports.hook__json(self.ports._default(project)))
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            row = db.session.get(WuyuWorkflow, project_id)
        return row

    def _read(self, project_id):
        db.session.expire_all()
        row = self.ports._ensure(project_id)
        return row.revision, json.loads(row.body)

    def _require_current_reading_replay(self, existing, body):
        if (existing is not None and existing.action in {'analyze_materials', 'revise_draft'}
                and existing.input_epoch != body['epoch']):
            raise WorkflowAPIError('旧阅读请求属于先前材料或需求版本；如需读取当前版本，请使用新的 request_id。',
                                   'IDEMPOTENCY_CONTEXT_CHANGED', 409)

    def _mutate(self, project_id, operation, expected_revision=None, job=None, replay=None):
        for _ in range(8):
            revision, body = self.ports._read(project_id)
            if replay and replay[0] in body['request_ids']:
                if body['request_ids'][replay[0]] != replay[1]:
                    raise WorkflowAPIError('Request ID was reused with different input', 'IDEMPOTENCY_CONFLICT', 409)
                existing = WuyuWorkflowJob.query.filter_by(project_id=project_id, idempotency_key=replay[0]).first()
                self.ports._require_current_reading_replay(existing, body)
                return existing.id if existing else None, body
            if expected_revision is not None and revision != expected_revision:
                raise WorkflowAPIError('Workflow changed; refresh before retrying', 'REVISION_CONFLICT', 409)
            if job and body['epoch'] != job.input_epoch:
                raise CancelledWork()
            try:
                result = operation(body)
            except IntegrityError:
                # A simultaneous request may have committed the same durable
                # idempotency key while this transaction waited for SQLite.
                db.session.rollback()
                if replay:
                    continue
                raise
            changed = db.session.execute(update(WuyuWorkflow).where(
                WuyuWorkflow.project_id == project_id, WuyuWorkflow.revision == revision
            ).values(body=self.ports.hook__json(body), revision=revision + 1, updated_at=datetime.utcnow())).rowcount
            if changed:
                db.session.commit()
                return result, body
            db.session.rollback()
            if expected_revision is not None:
                raise WorkflowAPIError('Workflow changed; refresh before retrying', 'REVISION_CONFLICT', 409)
        raise WorkflowAPIError('Workflow is busy; retry shortly', 'REVISION_CONFLICT', 409)

    def _event(self, project_id, kind, payload, job=None):
        def op(body):
            event_payload = payload
            if job and page_retry.PAGE_RETRY_SCOPE_KEY in json.loads(job.payload):
                number = json.loads(job.payload)[page_retry.PAGE_RETRY_SCOPE_KEY].get('number')
                if kind in {'page.generate_started', 'page.generated', 'page.failed'}:
                    if payload.get('number') != number:
                        raise PipelineError('page_retry_invalid', 'Page retry target does not match')
                    if kind == 'page.generate_started':
                        page_retry.validate_page_retry(job, body)
            if job and kind == 'page.failed':
                event_payload = page_retry.annotate_page_failure(job, body, payload)
            body['workflow'] = self.ports.hook_apply_event(body['workflow'], {'event_id': self.ports.hook__id(), 'type': kind, 'payload': event_payload})
            charge = current_workflow_charge.get()
            if charge and job and kind in {'sample.generated', 'page.generated', 'trace.page_completed'}:
                charge.mark_accepted(db.session.get(WuyuWorkflowJob, job.id))
        return self.ports._mutate(project_id, op, job=job)[1]

    def _cancel_old_jobs(self, project_id):
        db.session.execute(update(WuyuWorkflowJob).where(
            WuyuWorkflowJob.project_id == project_id, WuyuWorkflowJob.status == 'queued'
        ).values(status='cancelled', cancel_requested=True, completed_at=datetime.utcnow()))
        db.session.execute(update(WuyuWorkflowJob).where(
            WuyuWorkflowJob.project_id == project_id, WuyuWorkflowJob.status == 'running'
        ).values(cancel_requested=True))
        cancelled_tasks = db.select(WuyuWorkflowJob.task_id).where(
            WuyuWorkflowJob.project_id == project_id, WuyuWorkflowJob.status == 'cancelled')
        db.session.execute(update(Task).where(Task.id.in_(cancelled_tasks)).values(
            status='FAILED', error_message='CANCELLED_OR_INPUT_CHANGED', completed_at=datetime.utcnow()))

    def _reset_inflight(self, project_id, epoch, message):
        def op(body):
            if body['epoch'] != epoch:
                return
            self.ports._close_inflight(body, message)
        self.ports._mutate(project_id, op)

    def _close_inflight(self, body, message):
        state = body['workflow']
        def event(kind, payload):
            nonlocal state
            state = self.ports.hook_apply_event(state, {'event_id': self.ports.hook__id(), 'type': kind, 'payload': payload})
        for role, attempt in list(state['template']['sample_attempts'].items()):
            event('sample.failed', {'role': role, 'attempt_id': attempt['id'],
                'template_revision': attempt['template_revision'], 'requirements_revision': attempt['requirements_revision'], 'error': message})
        for page in state['pages']:
            if page['generation_status'] == 'running' and page['attempt']:
                event('page.failed', {'number': page['number'], 'attempt_id': page['attempt']['id'], 'error': message})
        if state['trace_batch']:
            event('trace.failed', {'batch_id': state['trace_batch']['id'], 'error': message})
        versions = {'image_versions': {str(p['number']): p['image_version'] for p in state['pages']},
                    'trace_versions': {str(p['number']): p['trace_version'] for p in state['pages']}}
        if state['qa_run']:
            event('qa.failed', {'run_id': state['qa_run']['id'], **versions, 'error': message})
        if state['export_run']:
            event('export.failed', {'run_id': state['export_run']['id'], **versions, 'error': message})
        body['workflow'] = state
