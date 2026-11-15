"""Business action state changes and job selection, called inside the host's existing CAS.

No ORM, queue, provider or quota object enters this module. Existing rule hooks
are explicit callables; their validation behavior and order remain unchanged.
"""
from copy import deepcopy
import json
from dataclasses import dataclass
from typing import AbstractSet, Callable, Optional


@dataclass(frozen=True)
class ActionStateDependencies:
    new_state: Callable
    validate_recipe: Callable
    confirm_template_choice: Callable
    patch_specs: Callable
    validate_specs: Callable
    normalize_specs: Callable
    bind_source_assets: Callable
    validate_sample_promotion: Callable
    public_source: Callable
    event_id: Callable
    error_type: type[Exception]
    phase: Callable
    current_relayout_source: Callable
    apply_event: Callable


def _update_brief(body, payload, project_id, deps):
    allowed = {'conversation_config_revision', 'task_mode', 'scenario', 'subscenario', 'audience', 'page_count', 'style', 'topic', 'goal',
               'requirements', 'review_policy', 'duration', 'language', 'aspect_ratio', 'template_id', 'preserve_outline',
               'creation_kind', 'output_preference', 'image_profile', 'asset_kind', 'feedback', 'design_notes', 'diagram_preset'}
    if set(payload) - allowed:
        raise deps.error_type('Unknown brief field')
    if 'conversation_config_revision' in payload and (type(payload['conversation_config_revision']) is not int or payload['conversation_config_revision'] < 0):
        raise deps.error_type('conversation_config_revision must be a nonnegative integer')
    brief = body['brief'] | {k: v for k, v in payload.items() if k not in {'review_policy', 'preserve_outline', 'feedback'}}
    from services.wuyu_generation import creation_options
    try:
        generation = creation_options(brief)
    except ValueError as error:
        raise deps.error_type(str(error)) from None
    if any(not isinstance(brief.get(k), str) or not brief[k].strip() for k in ['scenario', 'audience', 'style']):
        raise deps.error_type('Scenario, audience and style are required')
    policy = payload.get('review_policy', body['workflow']['review_policy'])
    if generation['creation_kind'] != 'deck':
        if 'review_policy' in payload and payload['review_policy'] != 'template_only':
            raise deps.error_type('Focused jobs use explicit generation authorization, not deck sample review')
        policy = 'template_only'
    if policy not in {'guided', 'template_only', 'template_and_final'}:
        raise deps.error_type('Invalid review policy')
    changed = {key for key in brief if brief[key] != body['brief'].get(key)} - {'conversation_config_revision'}
    if not changed:
        body['brief'] = brief
        if policy != body['workflow']['review_policy']:
            body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(),
                'type': 'review_policy.changed', 'payload': {'review_policy': policy}})
            body['epoch'] += 1
    elif changed == {'output_preference'} and policy == body['workflow']['review_policy']:
        body['brief'] = brief
        body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(),
            'type': 'output_preference.changed', 'payload': {'output_preference': generation['output_preference']}})
        body['epoch'] += 1
    elif brief != body['brief']:
        visual = {'style', 'aspect_ratio', 'template_id', 'image_profile', 'design_notes', 'diagram_preset'}
        preserve = bool(body['slide_specs']) and (
            deps.current_relayout_source(body) is not None or
            payload.get('preserve_outline') is not False and changed <= visual or
            payload.get('preserve_outline') is True and changed <= visual | {'requirements'})
        previous = body['workflow']
        body['brief'], body['workflow'] = brief, deps.new_state(project_id, brief, policy)
        if preserve:
            # Visual feedback changes image inputs, not the accepted
            # prose, evidence bindings, source records or crops.
            # New samples require a fresh, non-reusable approval.
            body['workflow']['requirements']['revision'] = max(
                body['workflow']['requirements']['revision'], previous['requirements']['revision'] + 1)
            body['workflow']['template']['revision'] = previous['template']['revision'] + 1
        else:
            body['outline'], body['slide_specs'] = [], []
            body['planning_report'] = {}
        body['epoch'] += 1

def _update_outline(body, payload, project_id, deps):
    patching = 'page_patches' in payload
    if patching:
        if set(payload) - {'page_patches', 'feedback'}:
            raise deps.error_type('page_patches cannot be mixed with a full outline')
        specs = deps.patch_specs(body['slide_specs'], payload['page_patches'])
        outline = [{'number': spec['number'], 'title': spec['title']} for spec in specs]
    else:
        outline = payload.get('outline')
        specs = payload.get('slide_specs', outline)
    deps.validate_specs(specs, body['brief']['page_count'], body['brief'])
    if patching:
        changed = [new['number'] for old, new in zip(body['slide_specs'], specs) if old != new]
        if changed:
            # A reviewed style stays approved when individual page
            # prose changes. Preserve other images and native traces.
            initial_focused_draft = (deps.phase(body['workflow']) == 'generation_request'
                and not body['workflow'].get('used_attempt_ids')
                and all(not page.get('artifact') and not page.get('image_version') and not page.get('attempt')
                        for page in body['workflow']['pages']))
            if (deps.phase(body['workflow']) not in {'requirements', 'samples', 'template_review'}
                    and not initial_focused_draft):
                for number in changed:
                    body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(),
                        'type': 'page.changed', 'payload': {'number': number,
                            'change': 'Follow this page\'s corrected title, copy, points, body and notes; retain the approved overall style.'}})
            else:
                body['workflow'] = deps.new_state(project_id, body['brief'], body['workflow']['review_policy'])
            body['outline'], body['slide_specs'] = outline, specs
            body.pop('resume_action', None)
            body['epoch'] += 1
    else:
        normalized = deps.normalize_specs(specs, brief=body['brief'])
        records = {ident: record for material in body['materials'] if material.get('role') == 'material'
                   and material['id']not in body.get('reading_exclusions',{})
                   for ident, record in material.get('source_structured_data', {}).items()}
        for spec in normalized:
            data_ids = spec.get('data_ids', [])
            if not isinstance(data_ids, list) or len(data_ids) > 4 or any(not isinstance(ident, str) or ident not in records for ident in data_ids):
                raise deps.error_type('Slide data IDs must refer to this project material records')
            spec['source_structured_data'] = {ident: deepcopy(records[ident]) for ident in data_ids}
        body['outline'], body['slide_specs'] = deps.public_source(outline), deps.bind_source_assets(normalized, body)
        body['workflow'] = deps.new_state(project_id, body['brief'], body['workflow']['review_policy'])
        body['epoch'] += 1


def apply_action_state(body, action, payload, *, project_id, pending_resume, deps):
    """Mutate only accepted business state; commit and task creation stay outside."""
    if action == 'update_brief':
        _update_brief(body, payload, project_id, deps)
    elif action == 'select_references':
        deps.validate_recipe(payload, body)
        body['reference_recipe'] = deepcopy(payload)
        body['workflow'] = deps.new_state(project_id, body['brief'], body['workflow']['review_policy'])
        body['epoch'] += 1
    elif action == 'confirm_template_choice':
        deps.confirm_template_choice(body, payload)
    elif action == 'update_outline':
        _update_outline(body, payload, project_id, deps)
    elif action == 'lock_pages':
        from .wuyu_page_lock import validate_lock_payload, apply_lock
        validate_lock_payload(body, payload, deps)
        apply_lock(body, payload, deps)
    elif action == 'regenerate_unlocked':
        from .wuyu_page_lock import validate_regenerate_payload, apply_regenerate_request
        validate_regenerate_payload(body, payload, deps, deps.phase)
        apply_regenerate_request(body, payload)
    elif action == 'generate_outline' and body['slide_specs']:
        body['workflow'] = deps.new_state(project_id, body['brief'], body['workflow']['review_policy'])
        body['outline'], body['slide_specs'] = [], []
        body['planning_report'] = {}
        body['epoch'] += 1
    elif action == 'confirm_template':
        t = body['workflow']['template']
        deps.validate_sample_promotion(body)
        body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(), 'type': 'template.confirmed',
            'payload': {'template_revision': payload.get('template_revision', t['revision']),
                        'samples_revision': payload.get('samples_revision', t['samples_revision']), 'reuse_samples': True}})
    elif action == 'confirm_page':
        number = payload.get('number')
        body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(), 'type': 'page.confirmed', 'payload': {
            'number': number, 'image_version': payload.get('image_version')}})
    elif action == 'revise_page':
        number = payload.get('number')
        body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(), 'type': 'page.changed',
            'payload': {'number': number, 'change': payload.get('feedback') or payload.get('change')}})
        if 'copy' in payload:
            if not isinstance(payload['copy'], str) or not payload['copy'].strip() or len(payload['copy']) > 30000:
                raise deps.error_type('Replacement page copy must be nonempty text up to 30000 characters')
            body['slide_specs'][number - 1]['copy'] = payload['copy']
        body['epoch'] += 1
    elif action == 'confirm_images':
        body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(), 'type': 'images.confirmed',
            'payload': {'image_versions': payload.get('image_versions')}})
    elif action in {'pause', 'cancel'}:
        if pending_resume is not None:
            body['resume_action'] = deepcopy(pending_resume)
        body['paused'] = True
        body['epoch'] += 1
    elif action == 'resume':
        body['paused'] = False
    elif action == 'retry':
        body['paused'] = False
        body.pop('resume_action', None)


@dataclass(frozen=True)
class JobRetrySource:
    """Only persisted retry values cross the boundary, never an ORM job."""
    action: str
    payload_json: str
    id: str


@dataclass(frozen=True)
class JobIntent:
    """A shallow value result; payload aliases intentionally retain old behavior."""
    action: Optional[str]
    payload: dict
    retry_of: Optional[str]
    template_source_job_id: Optional[str]


@dataclass(frozen=True)
class JobPlanDependencies:
    reading_queue_action: Callable
    phase: Callable
    async_actions: AbstractSet[str]
    read_actions: AbstractSet[str]
    page_retry_scope_key: str


def plan_action_job(body, action, payload, *, retry_source, retry_scope, deps):
    """Choose work inside CAS, consuming resume memo at its original point.

    This does not deduplicate, enqueue, charge, or read an ORM object. Reading
    and research retain their ordered overrides, including lineage fields.
    """
    queued_action, queued_payload, retry_of, template_source_job_id = None, payload, None, None
    if retry_source:
        queued_action, queued_payload, retry_of = retry_source.action, json.loads(retry_source.payload_json), retry_source.id
        if retry_scope is not None:
            queued_payload[deps.page_retry_scope_key] = retry_scope
    elif action == 'resume' and body.get('resume_action'):
        pending = body.pop('resume_action')
        queued_action, queued_payload = pending['action'], pending['payload']
        template_source_job_id = pending.get('template_source_job_id')
        if deps.page_retry_scope_key in queued_payload:
            retry_of = queued_payload[deps.page_retry_scope_key].get('source_job_id')
    elif action == 'regenerate_unlocked':
        queued_action, queued_payload = '_replan_unlocked', {}
    elif action in deps.async_actions:
        queued_action = action
    elif action in {'confirm_template', 'confirm_page', 'confirm_images', 'resume', 'revise_page'}:
        if body['workflow']['review_policy'] == 'guided' and deps.phase(body['workflow']) == 'page_generation':
            queued_action, queued_payload = 'generate_next', {}
        elif action == 'revise_page' or body['workflow']['review_policy'] != 'guided' or deps.phase(body['workflow']) in {'tracing', 'quality_check', 'ready_to_export'}:
            queued_action, queued_payload = '_continue', {}
    reading_action = deps.reading_queue_action(body, action)
    if reading_action:
        queued_action = reading_action
        queued_payload = {'generation_requested': True} if reading_action == 'generate_focused' else payload if action in deps.read_actions else {}
    if action in {'confirm_research', 'supplement_research'} and payload['mode'] != 'materials':
        queued_action, queued_payload = 'run_research', {}
    return JobIntent(queued_action, queued_payload, retry_of, template_source_job_id)
