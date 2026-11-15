"""Bind an explicit failed-page retry to one current failure and input basis.

Only the host creates the private payload marker. No provider, filesystem or
workflow-service import belongs here; dispatch and epoch/attempt fences stay
in the existing workflow coordinator and reducer.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from models.wuyu_workflow import WuyuWorkflowJob
from .wuyu_errors import PipelineError


PAGE_RETRY_SCOPE_KEY = '__runtime_page_retry'
PAGE_RETRY_SOURCE_KEY = '__page_retry_source'
# '_replan_unlocked' renders its changed pages through the same batch, so a page
# that fails there is retried as a single page (the retry never re-plans or re-charges).
SOURCE_ACTIONS = frozenset({'generate_images', 'generate_next', '_continue', '_replan_unlocked'})
PENDING_IMAGE_ACTIONS = SOURCE_ACTIONS | {
    'generate_focused', 'generate_samples', 'regenerate_samples',
}
TERMINAL_STATUSES = frozenset({'failed', 'interrupted', 'cancelled'})
_SCOPE_FIELDS = frozenset({
    'version', 'number', 'project_id', 'source_job_id', 'source_input_epoch',
    'failure_event_id', 'failure_attempt_id', 'basis_sha256',
})
_SOURCE_FIELDS = frozenset({'job_id', 'input_epoch', 'basis_sha256'})
_MESSAGES = {
    'page_retry_invalid': '本页重试请求无效，未发起模型调用；请刷新当前页面后重新选择失败页。',
    'page_retry_context_changed': '本页输入、失败记录或待执行任务已变化，未发起新的模型调用；请查看当前状态后再决定。',
    'page_retry_source_missing': '未找到可核对的本页失败任务，未发起模型调用；已有材料和成功页面保留。',
}


def _fail(code):
    raise PipelineError(code, _MESSAGES[code])


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


def _integer(value):
    return type(value) is int and value >= 0


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _project_id(body):
    workflow = body.get('workflow') if isinstance(body, dict) else None
    ident = workflow.get('project_id') if isinstance(workflow, dict) else None
    if not _identifier(ident):
        _fail('page_retry_invalid')
    return ident


def _page(body, number):
    pages = body.get('workflow', {}).get('pages', [])
    if type(number) is not int or not 1 <= number <= len(pages):
        _fail('page_retry_invalid')
    page = pages[number - 1]
    if (not isinstance(page, dict) or type(page.get('number')) is not int
            or page['number'] != number):
        _fail('page_retry_invalid')
    return page


def page_retry_basis(body, number):
    """Hash inputs, not runtime progress or other pages' generated outputs."""
    target = _page(body, number)
    workflow = body['workflow']
    template = workflow.get('template', {})
    brief = {key: value for key, value in body.get('brief', {}).items()
             if key != 'conversation_config_revision'}
    value = {
        'project_id': _project_id(body),
        'brief': brief,
        'slide_specs': body.get('slide_specs'),
        'pages': [{key: page.get(key) for key in ('number', 'input_revision', 'requested_change')}
                  for page in workflow.get('pages', [])],
        'target': {'number': number, 'image_version': target.get('image_version'),
                   'artifact': target.get('artifact')},
        'material_revision': body.get('material_revision', 0),
        'materials': [{key: item.get(key) for key in (
            'id', 'sha256', 'role', 'template_images', 'source_assets', 'source_structured_data')}
                      for item in body.get('materials', [])],
        'reading_exclusions': body.get('reading_exclusions', {}),
        'reading_asset_exclusions': body.get('reading_asset_exclusions', {}),
        'reference_recipe': body.get('reference_recipe'),
        'template_choice': body.get('template_choice'),
        'workflow_inputs': {key: workflow.get(key) for key in (
            'input_revision', 'mode', 'review_policy', 'creation_kind',
            'output_preference', 'requires_final_image_review')},
        'requirements': workflow.get('requirements'),
        'template': {key: template.get(key) for key in (
            'id', 'revision', 'confirmed_revision', 'samples_revision', 'confirmation_kind', 'samples')},
    }
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        _fail('page_retry_context_changed')
    return hashlib.sha256(encoded).hexdigest()


def _latest_failure(body, number):
    events = body['workflow'].get('events', [])
    if not isinstance(events, list):
        _fail('page_retry_source_missing')
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        payload = event.get('payload')
        if (not isinstance(payload, dict) or not isinstance(event.get('type'), str)
                or type(payload.get('number')) is not int
                or payload['number'] != number
                or event.get('type') not in {
                    'page.changed', 'page.generate_started', 'page.generated',
                    'page.failed', 'page.confirmed'}):
            continue
        if event['type'] != 'page.failed':
            _fail('page_retry_context_changed')
        if not _identifier(event.get('event_id')) or not _identifier(payload.get('attempt_id')):
            _fail('page_retry_source_missing')
        return event
    _fail('page_retry_source_missing')


def _scope_schema(scope):
    if (not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS
            or type(scope.get('version')) is not int or scope['version'] != 1
            or type(scope.get('number')) is not int or scope['number'] < 1
            or not _integer(scope.get('source_input_epoch'))
            or not all(_identifier(scope.get(key)) for key in (
                'project_id', 'source_job_id', 'failure_event_id', 'failure_attempt_id'))
            or not _hash(scope.get('basis_sha256'))):
        _fail('page_retry_invalid')
    return scope


def _source_matches(source, scope):
    return (isinstance(source, dict) and set(source) == _SOURCE_FIELDS
            and _identifier(source.get('job_id')) and _integer(source.get('input_epoch'))
            and _hash(source.get('basis_sha256'))
            and source['job_id'] == scope['source_job_id']
            and source['input_epoch'] == scope['source_input_epoch']
            and source['basis_sha256'] == scope['basis_sha256'])


def _json_object(value):
    try:
        result = json.loads(value or '{}')
    except (TypeError, ValueError):
        _fail('page_retry_source_missing')
    if not isinstance(result, dict):
        _fail('page_retry_source_missing')
    return result


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            _fail('page_retry_source_missing')
    if not isinstance(value, datetime):
        _fail('page_retry_source_missing')
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def prepare_page_retry(db, project, body, payload):
    """Resolve a trusted source and a fresh scope inside the host's CAS operation."""
    project_id = project if isinstance(project, str) else getattr(project, 'id', None)
    if project_id != _project_id(body) or not isinstance(payload, dict):
        _fail('page_retry_invalid')
    if set(payload) - {'number', 'job_id', 'task_id'}:
        _fail('page_retry_invalid')
    for key in ('job_id', 'task_id'):
        if key in payload and not _identifier(payload[key]):
            _fail('page_retry_invalid')
    query = db.session.query(WuyuWorkflowJob).filter_by(project_id=project_id).filter(
        WuyuWorkflowJob.action.in_(SOURCE_ACTIONS), WuyuWorkflowJob.status.in_(TERMINAL_STATUSES))
    if 'job_id' in payload:
        query = query.filter_by(id=payload['job_id'])
    if 'task_id' in payload:
        query = query.filter_by(task_id=payload['task_id'])
    number = payload.get('number')
    if 'number' not in payload:
        if not {'job_id', 'task_id'} & set(payload):
            _fail('page_retry_invalid')
        selected = query.order_by(WuyuWorkflowJob.created_at.desc()).first()
        if selected is None:
            _fail('page_retry_source_missing')
        marker = _scope_schema(_json_object(selected.payload).get(PAGE_RETRY_SCOPE_KEY))
        number = marker['number']
    page = _page(body, number)
    if page.get('generation_status') != 'failed':
        _fail('page_retry_context_changed')
    epoch = body.get('epoch')
    if not _integer(epoch):
        _fail('page_retry_invalid')
    if db.session.query(WuyuWorkflowJob).filter_by(project_id=project_id).filter(
            WuyuWorkflowJob.status.in_(['queued', 'running']),
            WuyuWorkflowJob.action.in_(PENDING_IMAGE_ACTIONS)).first() is not None:
        _fail('page_retry_context_changed')
    failure = _latest_failure(body, number)
    basis = page_retry_basis(body, number)
    provenance = failure['payload'].get(PAGE_RETRY_SOURCE_KEY)
    if PAGE_RETRY_SOURCE_KEY in failure['payload']:
        if not isinstance(provenance, dict) or not _identifier(provenance.get('job_id')):
            _fail('page_retry_context_changed')
        query = query.filter_by(id=provenance['job_id'])
    else:
        # Legacy evidence is accepted only within the exact unchanged epoch
        # and the original execution's closed timestamp window.
        instant = _utc(failure.get('recorded_at')).replace(tzinfo=None)
        query = query.filter(WuyuWorkflowJob.input_epoch == epoch,
                             WuyuWorkflowJob.started_at <= instant,
                             WuyuWorkflowJob.completed_at >= instant)
    if provenance is not None:
        source = query.order_by(WuyuWorkflowJob.created_at.desc()).first()
        if source is None:
            _fail('page_retry_source_missing')
    else:
        sources = query.order_by(WuyuWorkflowJob.created_at.desc()).limit(2).all()
        if len(sources) != 1:
            _fail('page_retry_source_missing')
        source = sources[0]
    scope = {
        'version': 1, 'number': number, 'project_id': project_id,
        'source_job_id': source.id, 'source_input_epoch': source.input_epoch,
        'failure_event_id': failure['event_id'],
        'failure_attempt_id': failure['payload']['attempt_id'], 'basis_sha256': basis,
    }
    _scope_schema(scope)
    if provenance is not None:
        if not _source_matches(provenance, scope) or source.input_epoch > epoch:
            _fail('page_retry_context_changed')
    else:
        if (source.input_epoch != epoch
                or not _utc(source.started_at) <= _utc(failure.get('recorded_at')) <= _utc(source.completed_at)):
            _fail('page_retry_context_changed')
        if source.action == 'generate_images':
            failed = _json_object(source.progress).get('failed_pages')
            if not isinstance(failed, list) or not any(type(item) is int and item == number for item in failed):
                _fail('page_retry_source_missing')
    return source, scope


def validate_page_retry(job, body):
    """Validate a private queued scope without querying or changing the database."""
    if job.action not in SOURCE_ACTIONS:
        _fail('page_retry_invalid')
    scope = _scope_schema(_json_object(job.payload).get(PAGE_RETRY_SCOPE_KEY))
    if (scope['project_id'] != job.project_id or job.project_id != _project_id(body)
            or job.retry_of != scope['source_job_id']
            or not _integer(job.input_epoch) or not _integer(body.get('epoch'))
            or job.input_epoch != body['epoch']):
        _fail('page_retry_context_changed')
    number = scope['number']
    if _page(body, number).get('generation_status') != 'failed':
        _fail('page_retry_context_changed')
    if page_retry_basis(body, number) != scope['basis_sha256']:
        _fail('page_retry_context_changed')
    failure = _latest_failure(body, number)
    if (failure['event_id'] != scope['failure_event_id']
            or failure['payload']['attempt_id'] != scope['failure_attempt_id']):
        _fail('page_retry_context_changed')
    if PAGE_RETRY_SOURCE_KEY in failure['payload']:
        if (not _source_matches(failure['payload'][PAGE_RETRY_SOURCE_KEY], scope)
                or scope['source_input_epoch'] > job.input_epoch):
            _fail('page_retry_context_changed')
    elif scope['source_input_epoch'] != job.input_epoch:
        _fail('page_retry_context_changed')
    return number


def annotate_page_failure(job, body, payload):
    """Attach server provenance before applying one eligible page.failed event."""
    result = deepcopy(payload)
    result.pop(PAGE_RETRY_SOURCE_KEY, None)
    if job is None or job.action not in SOURCE_ACTIONS:
        return result
    number = result.get('number')
    _page(body, number)
    if (job.project_id != _project_id(body) or not _identifier(job.id)
            or not _integer(job.input_epoch) or job.input_epoch != body.get('epoch')):
        _fail('page_retry_context_changed')
    result[PAGE_RETRY_SOURCE_KEY] = {
        'job_id': job.id, 'input_epoch': job.input_epoch,
        'basis_sha256': page_retry_basis(body, number),
    }
    return result
