"""Page locking and regeneration of the unlocked pages.

A locked page keeps its current image, approval-relevant versions and native
trace. ``regenerate_unlocked`` re-plans and re-renders only the other pages,
keeping the confirmed template and style. The page count stays as generated
unless the user explicitly asks for another count.
"""
from __future__ import annotations

from copy import deepcopy

from .wuyu_state import locked_numbers

LOCK_ACTIONS = {'lock_pages', 'regenerate_unlocked'}
CLIENT_LOCK_SOURCES = {'user_ui', 'user_chat'}
REPLAN_JOB = '_replan_unlocked'
_REGENERATE_FIELDS = {'feedback', 'page_count', 'locked_numbers'}


def _error(deps, message, code='PAGE_LOCK_INVALID', status=400):
    return deps.error_type(message, code, status)


def _is_page_number(value, count):
    return type(value) is int and 1 <= value <= count


def validate_lock_payload(body, payload, deps):
    count = len(body['workflow']['pages'])
    if not isinstance(payload, dict) or set(payload) - {'numbers', 'locked', 'source'} or not {'numbers', 'locked'} <= set(payload):
        raise _error(deps, 'lock_pages accepts numbers, locked and optional source only')
    numbers = payload['numbers']
    if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 80 or len(set(numbers)) != len(numbers)
            or not all(_is_page_number(n, count) for n in numbers)):
        raise _error(deps, 'numbers must be unique existing one-based page numbers')
    if type(payload['locked']) is not bool:
        raise _error(deps, 'locked must be true or false')
    if payload.get('source', 'user_chat') not in CLIENT_LOCK_SOURCES:
        raise _error(deps, 'Only the user interface or the conversation can lock pages')


def reject_when_locked(body, action, deps):
    """Whole-deck sample actions redo every page, so they cannot run while pages are locked.

    Rejecting before queueing keeps the failure at the API (no queued job, no charge)
    and gives the same message as the other whole-deck actions.
    """
    numbers = locked_numbers(body['workflow'])
    if numbers:
        raise deps.error_type(
            f'第{numbers[0]}页已锁定。重做样张会重做所有页面，请先解锁；只想重做其它页请使用“重新生成未锁定页”。',
            'PAGES_LOCKED', 409)


def validate_regenerate_payload(body, payload, deps, phase):
    """Checks independent of quota/state transitions; repeated inside CAS."""
    if not isinstance(payload, dict) or set(payload) - _REGENERATE_FIELDS:
        raise _error(deps, 'regenerate_unlocked accepts feedback, page_count and locked_numbers only')
    state = body['workflow']
    if phase(state) in {'requirements', 'samples', 'template_review', 'generation_request'}:
        raise _error(deps, 'Generate and confirm the deck before regenerating unlocked pages', 'PAGE_LOCK_NOT_READY', 409)
    if body['brief'].get('creation_kind', 'deck') == 'asset':
        raise _error(deps, 'A single asset has no pages to lock', 'PAGE_LOCK_NOT_READY', 409)
    if body.get('paused'):
        raise _error(deps, 'Resume the paused project before regenerating pages', 'WORKFLOW_PAUSED', 409)
    if any(p['generation_status'] == 'running' for p in state['pages']):
        raise _error(deps, 'Wait for the current page generation to finish', 'WORKFLOW_BUSY', 409)
    if 'feedback' in payload and (not isinstance(payload['feedback'], str) or not payload['feedback'].strip()
                                  or len(payload['feedback']) > 12000):
        raise _error(deps, 'feedback must be nonempty text up to 12000 characters')
    if (body.get('manual_edit') or {}).get('structure_changed'):
        raise _error(deps, '在线编辑中增删或调整了页面顺序，无法与 AI 逐页合并；请在编辑器中继续修改，或先放弃人工编辑版本。',
                     'MANUAL_EDIT_STRUCTURE', 409)
    current = len(state['pages'])
    count = payload.get('page_count', current)
    deck = body['brief'].get('creation_kind', 'deck') == 'deck'
    if type(count) is not int or not (2 <= count <= 80 if deck else 1 <= count <= 8):
        raise _error(deps, 'page_count does not match the creation scope')
    locked = locked_numbers(state)
    if 'locked_numbers' in payload:
        # The caller states which pages it believes are locked, so a lock
        # change made in the panel meanwhile cannot be overridden silently.
        claimed = payload['locked_numbers']
        if not isinstance(claimed, list) or sorted(claimed) != locked:
            raise _error(deps, 'Locked pages changed; read the current project before regenerating',
                         'PAGE_LOCK_CHANGED', 409)
    if any(n > count for n in locked):
        raise _error(deps, 'A locked page lies beyond the requested page count; unlock it or keep more pages',
                     'PAGE_LOCK_CONFLICT', 409)
    if len(locked) >= count:
        raise _error(deps, 'Every page is locked; unlock at least one page to regenerate', 'PAGE_LOCK_CONFLICT', 409)


def apply_lock(body, payload, deps):
    body['workflow'] = deps.apply_event(body['workflow'], {'event_id': deps.event_id(), 'type': 'pages.locked',
        'payload': {'numbers': list(payload['numbers']), 'locked': payload['locked'],
                    'source': payload.get('source', 'user_chat')}})


def apply_regenerate_request(body, payload):
    """Record the request only. Pages change when the new plan is accepted."""
    state = body['workflow']
    body['lock_regeneration'] = {
        'page_count': payload.get('page_count', len(state['pages'])),
        'previous_page_count': len(state['pages']),
        'locked_numbers': locked_numbers(state),
        'feedback': payload.get('feedback', ''),
    }


def locked_page_snapshot(state):
    return {p['number']: ((p.get('artifact') or {}).get('image_uri'), p['image_version'], p.get('trace_version'))
            for p in state['pages'] if p.get('locked') is True}


def assert_locks_preserved(before_state, after_state, action, deps):
    """Generic invariant: no business action except unlocking may alter a locked page."""
    if action == 'lock_pages':
        return
    before = locked_page_snapshot(before_state)
    if not before:
        return
    after = {p['number']: p for p in after_state.get('pages', [])}
    for number, value in before.items():
        page = after.get(number)
        if (page is None or page.get('locked') is not True
                or ((page.get('artifact') or {}).get('image_uri'), page['image_version'], page.get('trace_version')) != value):
            raise deps.error_type(
                f'第{number}页已锁定。整体改需求/换模板会重做所有页面，请先解锁；只想重做其它页请使用“重新生成未锁定页”。',
                'PAGES_LOCKED', 409)


def planning_brief(body, plan):
    """Brief copy for the planner. The stored brief is not modified."""
    brief = deepcopy(body['brief'])
    brief['page_count'] = plan['page_count']
    locked = {n: body['slide_specs'][n - 1] for n in plan['locked_numbers'] if n - 1 < len(body['slide_specs'])}
    lines = []
    if locked:
        lines.append('【页面锁定】以下页面已由用户锁定，其页号、标题和内容保持不变；请照常返回完整页数，'
                     '但锁定页只需重复下列标题，其余页面重新规划，与锁定页衔接且不要重复它们的内容：')
        for number, spec in sorted(locked.items()):
            lines.append(f'第{number}页（锁定）：{spec.get("title", "")}｜{str(spec.get("copy", ""))[:300]}')
    if plan.get('feedback'):
        lines.append('【用户对未锁定页的修改意见】' + plan['feedback'])
    if plan['page_count'] != plan['previous_page_count']:
        lines.append(f'【页数】用户明确要求总页数改为{plan["page_count"]}页。')
    brief['requirements'] = (str(brief.get('requirements') or '') + '\n' + '\n'.join(lines)).strip()
    return brief


def merge_specs(old_specs, new_specs, locked):
    """Locked positions keep the accepted spec byte-for-byte."""
    merged = []
    for index, spec in enumerate(new_specs):
        number = index + 1
        merged.append(deepcopy(old_specs[index]) if number in locked and index < len(old_specs) else spec)
    return merged


class PageLockMixin:
    """Job owner for ``_replan_unlocked``; uses the facade's existing ports."""

    def _replan_unlocked(self, job, pipeline):
        from models import db
        from models.wuyu_workflow import WuyuWorkflowJob
        from .wuyu_workflow_contracts import WorkflowAPIError
        from services.wuyu_workflow_quota import WorkflowCharge
        _, body = self._read(job.project_id)
        plan = body.get('lock_regeneration')
        if not plan:
            raise WorkflowAPIError('No regeneration request is pending')
        self._progress(job, 'planning', body)
        charge = WorkflowCharge(self.app, job, 'text').reserve()
        try:
            planning_body = dict(body, brief=planning_brief(body, plan))
            materials = [m for m in body['materials'] if m['id'] not in body.get('reading_exclusions', {})]
            output = self._plan_outline(pipeline, planning_body, materials, job)
            self._check_cancel(job)
            specs = output.get('slide_specs') or output.get('outline') if isinstance(output, dict) else output
            self._validate_specs(specs, plan['page_count'], planning_body['brief'])
            locked = set(plan['locked_numbers'])

            def accept(current):
                if current.get('lock_regeneration') != plan or locked_numbers(current['workflow']) != sorted(locked):
                    raise WorkflowAPIError('Locked pages changed during planning; nothing was replaced',
                                           'PAGE_LOCK_CHANGED', 409)
                fresh = self._bind_source_assets(self._normalize_specs(specs, trusted=True, brief=planning_body['brief']), current)
                merged = merge_specs(current['slide_specs'], fresh, locked)
                state = current['workflow']
                if plan['page_count'] != len(state['pages']):
                    state = self._apply(state, 'pages.resized', {'count': plan['page_count']})
                    current['brief']['page_count'] = plan['page_count']
                change = plan['feedback'] or '按重新规划后的本页内容重新生成，保持已确认模板与整体风格。'
                for page in state['pages']:
                    if page['number'] in locked:
                        continue
                    if page.get('artifact') or page['generation_status'] != 'idle':
                        state = self._apply(state, 'page.changed', {'number': page['number'], 'change': change, 'fresh': True})
                current['workflow'] = state
                current['slide_specs'] = merged
                current['outline'] = [{'number': s['number'], 'title': s['title']} for s in merged]
                current['planning_report'] = dict(current.get('planning_report') or {},
                    lock_regeneration={'locked_numbers': sorted(locked), 'page_count': plan['page_count'],
                                       'replanned_numbers': [s['number'] for s in merged if s['number'] not in locked]})
                current.pop('lock_regeneration', None)
                current['history'].append({'at': self._now(), 'action': 'unlocked_pages_replanned', 'status': 'accepted'})
                charge.mark_accepted(db.session.get(WuyuWorkflowJob, job.id))

            with charge.accepting(pipeline=pipeline):
                self._mutate(job.project_id, accept, job=job)
            charge.settle(pipeline=pipeline)
        except BaseException:
            charge.refund()
            raise
        # Image generation for the changed pages uses the ordinary continuation,
        # which skips locked (still current) pages.
        self._continue(job, pipeline)

    def _apply(self, state, kind, payload):
        from .wuyu_state import apply_event
        from .wuyu_workflow_contracts import _id
        return apply_event(state, {'event_id': _id(), 'type': kind, 'payload': payload})

    @staticmethod
    def _now():
        from .wuyu_workflow_contracts import _now
        return _now()


def public_lock_view(state):
    """Compact fields for the model/panel views."""
    return {'locked_numbers': locked_numbers(state),
            'lockable_numbers': [p['number'] for p in state.get('pages', []) if p.get('generation_status') == 'ready']}

