"""Additive reading/MD review protocol for the existing durable workflow owner."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import uuid

from models import db, User
from models.wuyu_workflow import WuyuWorkflowJob
from .wuyu_errors import PipelineError
from .wuyu_quota import text_points, get_quota_service, QuotaError
from .wuyu_points_policy import tariffs
from .wuyu_reading import (PROTOCOL, encoded, sha, prepare_reading,
                           finish_reading, markdown_draft)
# Preserve existing callers while execution and cache ownership live separately.
from .wuyu_reading_executor import _write, _cached_response, _cached, run_reader
from .wuyu_workflow_quota import WorkflowCharge
from .wuyu_reading_partial import (apply_omission_audit, verified_subset,
    continuation_options, accepted_reading, reading_failure_code, READER_RESPONSE_CODES)

READ_ACTIONS = {'analyze_materials', 'revise_draft', 'continue_from_draft', 'exclude_materials', 'exclude_source_assets'}
GENERATING = {'generate_outline', 'generate_samples', 'regenerate_samples', 'generate_focused',
              'generate_next', 'generate_images', 'confirm_template', 'confirm_page', 'confirm_images', 'revise_page', 'start_tracing'}


# Only response validation failures are recoverable by changing the plan request.
# Cache identity/path/hash, source integrity, cancellation and budget failures stay strict.
CACHED_PLAN_VALIDATION_CODES = frozenset({
    'invalid_model_json', 'invalid_slide_spec', 'reading_synthesis_invalid',
    'reading_synthesis_source_mismatch', 'reading_synthesis_incomplete_statement', 'output_truncated'})


def template_reading_guard(body):
    """Serialize visual-template mutations against this epoch's actual reader.

    A stale public reading label is not a lock. The durable queued/running job
    is the owner; neither a template click nor configuration synchronization may
    silently discard its in-flight paid result.
    """
    pending = WuyuWorkflowJob.query.filter_by(
        project_id=body['workflow']['project_id'], input_epoch=body['epoch']).filter(
        WuyuWorkflowJob.action.in_(['analyze_materials', 'revise_draft']),
        WuyuWorkflowJob.status.in_(['queued', 'running'])).first()
    if pending:
        from .wuyu_workflow_contracts import WorkflowAPIError
        raise WorkflowAPIError('资料正在阅读，请等待本次阅读完成后再上传或更换模板；本次操作未中断阅读。',
                               'TEMPLATE_READING_IN_PROGRESS', 409)


def template_parse_guard(body, *, running=False):
    """A newly appended template must finish its existing local intake first.

    Checked both before an action and inside its CAS/job transaction. This
    closes the interval between template append and automatic selection without
    adding a second upload state or starting another parser/reader.
    """
    pending_ids = {item['id'] for item in body.get('materials', [])
                   if item.get('role') == 'template' and item.get('status') in {'pending', 'processing'}}
    if pending_ids:
        covered = set()
        for job in WuyuWorkflowJob.query.filter_by(
                project_id=body['workflow']['project_id'], input_epoch=body['epoch'], action='_parse_materials').filter(
                WuyuWorkflowJob.status.in_(['queued', 'running'])):
            try:
                ids = json.loads(job.payload).get('material_ids', [])
            except (ValueError, AttributeError):
                continue
            if isinstance(ids, list):
                covered.update(ident for ident in ids if isinstance(ident, str))
        active = pending_ids <= covered
        code = 'TEMPLATE_PARSE_PENDING' if active else 'TEMPLATE_PARSE_INCOMPLETE'
        message = ('模板正在本地解析，请等待现有解析完成后再开始阅读；本次尚未启动新的阅读任务。' if active else
                   '模板解析尚未完成，且未发现覆盖该模板的排队或运行中解析任务。请先刷新模板状态核对；若状态仍未恢复，请检查并恢复当前项目的模板解析。原文件保留，本次未启动阅读。')
        if running:
            raise PipelineError(code, message)
        from .wuyu_workflow_contracts import WorkflowAPIError
        raise WorkflowAPIError(message, code, 409)


def terminal_reading_view(body):
    """Read-only recovery for old terminal owners left labelled as reading."""
    value = body.get('reading') or {}
    if (value.get('status') != 'reading' or not value.get('_owner_job_id')
            or value.get('source_revision') != body.get('material_revision', 0)):
        return value
    owner = WuyuWorkflowJob.query.filter_by(
        id=value['_owner_job_id'], project_id=body['workflow']['project_id']).first()
    if owner is None or owner.status not in {'cancelled', 'failed', 'interrupted', 'completed'}:
        return value
    code = ('reading_cancelled' if owner.status == 'cancelled' else
            'reading_interrupted' if owner.status == 'interrupted' else 'reading_job_incomplete')
    result = deepcopy(value)
    reason = ('inputs_changed' if owner.input_epoch != body['epoch'] else
              'processing_stopped' if owner.status == 'cancelled' else
              'process_interrupted' if owner.status == 'interrupted' else 'result_not_published')
    result.update(status='draft_incomplete', coverage_complete=value.get('source_reading_complete') is True,
                  interruption={'job_status': owner.status, 'error_code': code, 'reason': reason})
    result['issues'] = list(result.get('issues', []))
    if not any(item.get('code') == code for item in result['issues']):
        result['issues'].append({'code': code})
    return result


def _cached_plan_invalid(cause):
    error = PipelineError('reading_cached_plan_invalid',
        '已读材料保留；旧逐页计划缓存未通过检查。请给出具体调整意见后重新整理当前草稿，直接重试不会改变旧计划。')
    error.cause_code = cause
    error.diagnostics = {'validation_code': cause, 'response_cached': True, 'provider_called': False}
    return error


def _reader_goal(brief):
    """Planning feedback/count/language/visual choices cannot change the reading goal."""
    result = {key: brief.get(key) for key in (
        'task_mode', 'topic', 'goal', 'requirements', 'audience', 'scenario', 'subscenario', 'asset_kind', 'duration')}
    kind = brief.get('creation_kind', 'deck')
    result['creation_kind'] = 'presentation' if kind in {'deck', 'pages'} else kind
    return result


def _reader_scope(body):
    """An optional compatibility guard; existing reader task/cache identities are unchanged."""
    sources = [{key: material.get(key) for key in ('id', 'sha256', 'role', 'filename', 'relative_path', 'origin')}
               for material in body['materials'] if material.get('role') in (None, '', 'material')]
    return sha({'sources': sources, 'goal': _reader_goal(body['brief']),
                'excluded_materials': body.get('reading_exclusions', {}),
                'excluded_assets': body.get('reading_asset_exclusions', {})})


def _reader_context(body):
    previous = body.get('reading') or {}
    saved = previous.get('_reader_brief')
    if (isinstance(saved, dict) and previous.get('source_revision') == body.get('material_revision', 0)
            and _reader_goal(saved) == _reader_goal(body['brief'])
            and (previous.get('_reader_scope') is None or previous['_reader_scope'] == _reader_scope(body))):
        # Legacy records have no optional scope guard. prepare_reading still
        # verifies originals and builds exact source/role/policy-bound tasks.
        return deepcopy(saved)
    return deepcopy(body['brief'])




class ReadingWorkflowMixin:
    def _reading_protocol_enabled(self):
        # Legacy regression fixtures may opt out explicitly. Production cannot
        # turn a new material workflow into the old unreviewed generation path.
        return not (self.app.config.get('TESTING') is True and self.app.config.get('WUYU_READING_ENABLED') is False)

    def _reading_failed(self, job, code):
        from .wuyu_workflow_contracts import CancelledWork
        from .wuyu_workflow_terminal import apply_reading_failure
        def update(body):
            if not apply_reading_failure(body, job.id, job.input_epoch, code):
                raise CancelledWork()
        # Only close the same owner's label; do not attach old results or rely
        # on its superseded epoch, and never overwrite a replacement reader.
        try:self._mutate(job.project_id, update)
        except Exception:db.session.rollback()

    def _reading_required(self, body):
        from .wuyu_relayout import current_source
        if current_source(body):
            return False
        return body.get('reading_protocol') == PROTOCOL and any(
            m.get('role') in (None, '', 'material') and m['id'] not in body.get('reading_exclusions', {})
            for m in body['materials'])

    def _reading_public(self, body, replace):
        if not self._reading_required(body):return None
        value = terminal_reading_view(body)
        status = value.get('status', 'idle')
        if value.get('source_revision') != body.get('material_revision', 0):status = 'idle'
        result = {k: deepcopy(value[k]) for k in ('draft_revision','mode','total_materials','total_segments',
            'submitted_materials','selected_materials','excluded_materials','read_materials','failed_materials','all_uploaded_materials_read',
            'completed_segments','failed_segments','coverage_complete','normalization_count','issues','artifacts','policy','interruption') if k in value}
        result.update({key: value[key] for key in ('reader_contract','acknowledgement_unit','total_reader_tasks','completed_reader_tasks','coverage_basis','planning_issues','content_plan_complete','visual_coverage','synthesis','evidence_validation',
            'omitted_source_assets','omitted_material_ids','selected_scope_complete','accepted_with_omissions','partial_acceptance','evidence_context_preserved') if key in value})
        if isinstance(result.get('issues'),list):
            result['issue_count']=len(result['issues'])
            result['issues']=[{**{k:v for k,v in issue.items()if k not in {'segment_ids','source_asset_ids'}},
                              **({'affected_source_ranges':len(issue['segment_ids'])}if isinstance(issue.get('segment_ids'),list)else{}),
                              **({'affected_images':len(issue['source_asset_ids'])}if isinstance(issue.get('source_asset_ids'),list)else{})}
                             for issue in result['issues'][:20]]
        result.update(protocol_version=PROTOCOL, status=status, draft_revision=value.get('draft_revision', 0),
            can_continue=status == 'draft_ready' and value.get('coverage_complete') is True)
        result['continuation_options'] = continuation_options(dict(value, status=status), body)
        # An untouched/replanned idle draft has no attempted plan to fail.
        # This is a public status projection only; internal continuation gates
        # and any recorded error/security/partial-plan evidence remain strict.
        if (status == 'idle' and value.get('status', 'idle') == 'idle'
                and not any(value.get(key) for key in ('issues', 'planning_issues', 'error', 'error_code'))):
            result['continuation_options'].update(failure_category='not_started',
                content_plan_valid=False, can_continue_with_omissions=False, can_exclude_source_assets=False)
        # Readers must be able to inspect the complete current page materials
        # before paying for image generation; private _slide_specs stay private.
        if status in {'draft_ready','draft_incomplete','accepted'}:
            page_plan=deepcopy(value.get('_slide_specs',[]))
            assets={a['id']:self._public_asset(body['workflow']['project_id'],a)
                    for m in body.get('materials',[])for a in m.get('source_assets',[])}
            for spec in page_plan:
                for image in spec.get('reading_plan',{}).get('image_materials',[]):
                    public=assets.get(image.get('source_asset_id'),{})
                    image['image_url']=public.get('image_url')or public.get('url')or public.get('preview_url')
            result['page_plan']=page_plan
        return replace(result)

    def _reading_invalidate(self, body):
        if not self._reading_protocol_enabled():return
        body['reading_protocol'] = PROTOCOL
        body['material_revision'] = body.get('material_revision', 0) + 1
        previous = body.get('reading') or {}
        body['reading'] = {'status': 'idle', 'draft_revision': previous.get('draft_revision', 0),
                           'coverage_complete': False, 'source_revision': body['material_revision']}

    def _reading_replan(self, body, reader_brief):
        """Retain exact reader inputs for a count/language-only replan; revoke draft approval."""
        if not self._reading_protocol_enabled():return
        previous = body.get('reading') or {}
        body['reading_protocol'] = PROTOCOL
        context = previous.get('_reader_brief')
        if not isinstance(context, dict):context = reader_brief
        body['reading'] = {'status': 'idle', 'draft_revision': previous.get('draft_revision', 0),
                           'coverage_complete': False, 'source_revision': body.get('material_revision', 0),
                           '_reader_brief': deepcopy(context), 'evidence_context_preserved': True}
        for key in ('_reader_scope', '_planning_feedback'):
            if key in previous:body['reading'][key] = deepcopy(previous[key])

    def _reading_validate_job(self, body, action, payload, *, running=False):
        # Retry/resume resolve an effective action after the public action has
        # been checked. Persisted pre-upgrade jobs must pass this same gate at
        # execution; a new material revision cannot reuse an old MD approval.
        if action in {'analyze_materials', 'revise_draft'}:
            template_parse_guard(body, running=running)
        local_export = action == 'export' and (payload.get('format', 'editable_pptx') != 'editable_pptx'
                                               or payload.get('rebuild') is True)
        paid_generation = action in GENERATING | {'_continue'} or action == 'export' and not local_export
        reading = body.get('reading') or {}
        accepted = accepted_reading(reading, body.get('material_revision', 0))
        if self._reading_required(body) and paid_generation and not accepted:
            message = 'Review the material MD draft and explicitly continue first'
            if running:raise PipelineError('DRAFT_REVIEW_REQUIRED', message)
            from .wuyu_workflow_service import WorkflowAPIError
            raise WorkflowAPIError(message, 'DRAFT_REVIEW_REQUIRED', 409)

    def _reading_validate_action(self, body, action, payload):
        from .wuyu_workflow_service import WorkflowAPIError
        if action in {'confirm_template_choice', 'select_references'}:
            template_reading_guard(body)
        if action == 'update_brief':
            changed = {key for key, value in payload.items()
                       if key not in {'conversation_config_revision', 'preserve_outline', 'feedback'}
                       and value != (body['workflow']['review_policy'] if key == 'review_policy'
                                     else body['brief'].get(key))}
            if changed and changed <= {'style', 'aspect_ratio', 'template_id', 'image_profile',
                                      'design_notes', 'diagram_preset', 'review_policy'}:
                template_reading_guard(body)
        if self._reading_queue_action(body, action) in {'analyze_materials', 'revise_draft'}:
            template_parse_guard(body)
        if action in READ_ACTIONS:
            allowed = {'analyze_materials': set(), 'revise_draft': {'draft_revision', 'feedback'},
                       'continue_from_draft': {'draft_revision', 'continue_requested'},
                       'exclude_materials':{'material_ids','reason'},
                       'exclude_source_assets':{'source_asset_ids','reason'}}[action]
            optional = {'allow_incomplete'} if action == 'continue_from_draft' else {'cache_only'} if action == 'analyze_materials' else set()
            if not allowed <= set(payload) or set(payload) - allowed - optional:
                raise WorkflowAPIError('Reading actions require only their documented fields')
            if 'cache_only' in payload and type(payload['cache_only']) is not bool:
                raise WorkflowAPIError('cache_only must be an explicit boolean')
            if 'allow_incomplete' in payload and type(payload['allow_incomplete']) is not bool:
                raise WorkflowAPIError('allow_incomplete must be an explicit boolean')
            if action == 'revise_draft' and (not isinstance(payload['feedback'], str) or not payload['feedback'].strip() or len(payload['feedback']) > 12000):
                raise WorkflowAPIError('Draft feedback must be nonempty text up to12000 characters')
            if action=='exclude_materials':
                ids=payload['material_ids'];owned={m['id']for m in body['materials']if m.get('role')in(None,'','material')}
                if (not isinstance(ids,list)or not 1<=len(ids)<=100 or not all(isinstance(i,str)for i in ids)
                        or len(ids)!=len(set(ids))or not set(ids)<=owned or not isinstance(payload['reason'],str)
                        or not 1<=len(payload['reason'].strip())<=500):
                    raise WorkflowAPIError('Explicit exclusion needs unique owned material IDs and a reason of1–500 characters')
            if action == 'exclude_source_assets':
                ids=payload['source_asset_ids']
                owned={a['id'] for m in body['materials'] if m.get('role') in (None,'','material')
                       for a in m.get('source_assets',[])}
                if (not isinstance(ids,list) or not 1 <= len(ids) <= 600 or not all(isinstance(i,str) for i in ids)
                        or len(ids) != len(set(ids)) or not set(ids) <= owned or not isinstance(payload['reason'],str)
                        or not 1 <= len(payload['reason'].strip()) <= 500):
                    raise WorkflowAPIError('Explicit image exclusion needs unique owned source_asset_ids and a reason of1–500 characters')
            if action == 'analyze_materials' and not any(
                    m.get('role') in (None, '', 'material') and m['id'] not in body.get('reading_exclusions', {})
                    for m in body.get('materials', [])):
                raise WorkflowAPIError('当前没有选用任何内容材料，阅读不会产生页面内容。请上传或保存文字材料（例如把对话里的文案保存为材料），或恢复已排除的材料后再阅读。',
                                       'READING_NO_MATERIALS', 409)
            if action not in {'analyze_materials','exclude_materials','exclude_source_assets'}:
                reading = body.get('reading') or {}
                if type(payload['draft_revision']) is not int or payload['draft_revision'] != reading.get('draft_revision') or reading.get('source_revision') != body.get('material_revision', 0):
                    raise WorkflowAPIError('Draft changed; inspect the current revision', 'DRAFT_REVISION_CONFLICT', 409)
                if action == 'continue_from_draft':
                    full = reading.get('status') == 'draft_ready' and reading.get('coverage_complete') is True
                    partial = payload.get('allow_incomplete') is True and continuation_options(reading, body)['can_continue_with_omissions']
                    if payload['continue_requested'] is not True or not (full or partial):
                        raise WorkflowAPIError('Review the current valid draft; partial sources require explicit allow_incomplete=true and verified evidence', 'DRAFT_NOT_READY', 409)
        if self._reading_required(body) and action in GENERATING:
            reading = body.get('reading') or {}
            ready = accepted_reading(reading, body.get('material_revision', 0))
            if not ready and (reading.get('status') in {'draft_ready', 'draft_incomplete', 'reading'} or action not in {'generate_outline','generate_samples','generate_focused'}):
                raise WorkflowAPIError('Review the material MD draft and explicitly continue first', 'DRAFT_REVIEW_REQUIRED', 409)

    def _reading_apply_action(self, body, action, payload):
        if action in READ_ACTIONS:
            # An explicit current reading request replaces an obsolete paused
            # image continuation. It must not remain unclaimable behind pause.
            body['paused'] = False
            body.pop('resume_action', None)
        if action == 'analyze_materials':
            body['reading_protocol'] = PROTOCOL
        if action=='exclude_materials':
            body.setdefault('reading_exclusions',{}).update({ident:payload['reason'].strip()for ident in payload['material_ids']})
            self._reading_invalidate(body)
            body['outline'],body['slide_specs']=[],[]
            body['workflow']=self._new_state(body['workflow']['project_id'],body['brief'],body['workflow']['review_policy'])
            body['epoch']+=1
            if not self._reading_required(body):
                # Explicitly excluded originals remain stored and auditable;
                # an empty selection is not an unread upload or a successful
                # reading. Current user-supplied brief text can now follow the
                # ordinary no-material planning path without a phantom MD gate.
                body['reading'].update(status='not_required', coverage_complete=False,
                    submitted_materials=len([m for m in body['materials'] if m.get('role') in (None, '', 'material')]),
                    selected_materials=0, read_materials=0, failed_materials=0,
                    excluded_materials=len(body.get('reading_exclusions', {})),
                    all_uploaded_materials_read=False, total_segments=0, completed_segments=0, failed_segments=0,
                    issues=[], artifacts={}, _slide_specs=[])
        if action == 'exclude_source_assets':
            materials = {a['id']: m['id'] for m in body['materials'] if m.get('role') in (None,'','material')
                         for a in m.get('source_assets', [])}
            body.setdefault('reading_asset_exclusions', {}).update({ident: {
                'material_id': materials[ident], 'reason': payload['reason'].strip(),
                'source_revision': body.get('material_revision', 0)} for ident in payload['source_asset_ids']})
            self._reading_invalidate(body)
            body['outline'], body['slide_specs'] = [], []
            body['workflow'] = self._new_state(body['workflow']['project_id'], body['brief'], body['workflow']['review_policy'])
            body['epoch'] += 1
        if action == 'continue_from_draft':
            reading = body['reading']
            specs = deepcopy(reading['_slide_specs'])
            records = {ident: record for material in body['materials'] if material.get('role') == 'material' and material['id']not in body.get('reading_exclusions',{})
                       for ident, record in material.get('source_structured_data', {}).items()}
            for spec in specs:
                spec['data_ids'] = spec.get('data_ids', [])[:4]
                spec['source_structured_data'] = {ident: records[ident] for ident in spec['data_ids'] if ident in records}
                spec['source_asset_ids'] = spec.get('source_asset_ids', [])[:2]
                if payload.get('allow_incomplete') is True:
                    allowed_images = set(reading.get('_acknowledged_source_asset_ids', []))
                    spec['source_asset_ids'] = [i for i in spec['source_asset_ids'] if i in allowed_images]
            body['slide_specs'] = self._bind_source_assets(self._normalize_specs(specs, trusted=True, brief=body['brief']), body)
            body['outline'] = [{'number': spec['number'], 'title': spec['title']} for spec in body['slide_specs']]
            body['workflow'] = self._new_state(body['workflow']['project_id'], body['brief'], body['workflow']['review_policy'])
            reading['status'] = 'accepted';reading['accepted_draft_revision'] = reading['draft_revision']
            reading['accepted_with_omissions'] = reading.get('coverage_complete') is not True or bool(body.get('reading_exclusions'))
            if reading['accepted_with_omissions']:
                options = continuation_options(dict(reading, status='draft_incomplete'), body)
                reading['partial_acceptance'] = {'draft_revision': reading['draft_revision'],
                    'source_revision': body.get('material_revision', 0), 'continue_requested': True,
                    'allow_incomplete': payload.get('allow_incomplete') is True,
                    'omitted_material_ids': options['omitted_material_ids'],
                    'source_asset_ids': options['source_asset_ids'], 'omission_reasons': options['omission_reasons']}
            body['epoch'] += 1
        if action == 'revise_draft':
            body['reading']['status'] = 'idle'
            body['reading']['_planning_feedback'] = payload['feedback']

    def _reading_queue_action(self, body, action):
        if action == 'continue_from_draft':
            from .wuyu_template_review import current_template_choice
            return 'generate_images' if current_template_choice(body) else None
        if action in {'analyze_materials', 'revise_draft'}:return action
        # Changing the selected source scope revokes approval without dispatching model work.
        if action in {'exclude_materials','exclude_source_assets'}:return None
        if self._reading_required(body)and action=='generate_outline':return 'analyze_materials'
        if self._reading_required(body) and action in {'generate_outline','generate_samples','generate_focused'} and (body.get('reading') or {}).get('status') != 'accepted':
            return 'analyze_materials'
        return None

    def _reading_run(self, job, pipeline):
        from .wuyu_workflow_service import CancelledWork
        from .wuyu_identity import authorize_paid, IdentityFailure
        if self.app.config.get('WUYU_QUOTA_ENABLED', True) and (not job.actor_user_id or db.session.get(User,job.actor_user_id)is None):
            raise PipelineError('quota_actor_missing','阅读任务缺少有效的发起账户；不会发布新的草稿。')
        try:authorize_paid(job.actor_user_id,'text',job._runtime_config.get('WUYU_IMAGE_MODEL'))
        except IdentityFailure as error:
            raise PipelineError(error.code,'当前账户的工作权限已变更；不会发布新的草稿。')from None
        _, body = self._read(job.project_id)
        payload = json.loads(job.payload)
        policy = payload['__reading_policy']
        # A failed legacy batch may have left later files pending. Complete
        # those local parses individually; old failed files need user action,
        # never an implicit retry. Preserve original records and cancellation.
        for material in body['materials']:
            if (material.get('role') not in (None, '', 'material') or material['id'] in body.get('reading_exclusions', {})
                    or material.get('status') not in {'pending', 'processing'}):
                continue
            self._check_cancel(job)
            try:self._parse_materials(job, pipeline, {'material_ids': [material['id']]})
            except CancelledWork:raise
            except Exception as error:
                if isinstance(error, PipelineError) and error.code == 'cancelled':raise
                # _parse_materials records this file's failure. Continue with
                # later selected sources rather than silently leaving them out.
                self._check_cancel(job)
        _, body = self._read(job.project_id)
        config = dict(job._runtime_config)
        config['WUYU_READING_CACHE_ONLY'] = payload.get('cache_only') is True
        config['WUYU_READING_EXCLUSIONS']=deepcopy(body.get('reading_exclusions',{}))
        config['WUYU_READING_ASSET_EXCLUSIONS']=deepcopy(body.get('reading_asset_exclusions',{}))
        config.update({target: policy[source] for target, source in (
            ('WUYU_READING_WORKERS','reading_workers'),('WUYU_READING_MAX_CALLS','reading_max_calls'),
            ('WUYU_READING_MAX_POINTS','reading_max_points'),('WUYU_READING_INPUT_TOKENS','reading_input_tokens'))})
        brief = deepcopy(body['brief'])
        previous_reading = body.get('reading') or {}
        feedback = payload['feedback'] if job.action == 'revise_draft' else previous_reading.get('_planning_feedback')
        if feedback is None:
            # Preserve pre-upgrade planning feedback without changing the exact
            # old reader brief or its task/cache keys.
            feedback = (previous_reading.get('_reader_brief') or {}).get('draft_revision_feedback')
        if isinstance(feedback, str):brief['draft_revision_feedback'] = feedback
        reader_brief = _reader_context(body)
        plan = prepare_reading(body['materials'], reader_brief, config, lambda: self._check_cancel(job))
        source_revision = body.get('material_revision', 0)
        draft_revision = (body.get('reading') or {}).get('draft_revision', 0) + 1
        for update in plan.get('_material_refreshes',[]):
            previous=next(m for m in body['materials']if m['id']==update['id'])
            old_assets={a['id']:a for a in previous.get('source_assets',[])}
            for asset in update.get('source_assets',[]):
                known=old_assets.get(asset['id'],{})
                asset.update(material_id=update['id'],project_id=job.project_id,evidence_kind='project_material')
                if known.get('sha256')==asset['sha256']and known.get('artifact_id'):
                    asset['artifact_id']=known['artifact_id']
                else:
                    uri=self._record_artifact(job.project_id,Path(asset['path']),'source_figure',job,
                        {'material_id':update['id'],'source_asset_id':asset['id'],'figure_label':asset.get('figure_label')})
                    asset['artifact_id']=uri.removeprefix('artifact:')
        def update_reading(current, values):
            if current.get('material_revision', 0) != source_revision:raise CancelledWork()
            for update in plan.get('_material_refreshes',[]):
                material=next((m for m in current['materials']if m['id']==update['id']and m.get('sha256')==update['sha256']),None)
                if material is None:raise CancelledWork()
                material.update(deepcopy(update))
            if '_reader_brief' in values:
                values = dict(values, _reader_scope=_reader_scope(current))
            current.setdefault('reading', {}).update(values)
        self._mutate(job.project_id, lambda current: update_reading(current, {
            'status': 'reading', 'draft_revision': draft_revision, 'source_revision': source_revision, '_owner_job_id':job.id,
            '_reader_brief': reader_brief,
            **({'_planning_feedback': feedback} if isinstance(feedback, str) else {}),
            'mode': plan['mode'], 'total_materials': len(plan['materials']), 'total_segments': len(plan['segments']),
            'reader_contract': plan['policy']['reader_contract'], 'acknowledgement_unit': 'material_scope',
            'total_reader_tasks': len(plan['tasks']), 'completed_reader_tasks': 0,
            'submitted_materials': len(plan['materials']),
            'selected_materials': sum(m['status'] != 'excluded_by_user' for m in plan['materials']),
            'excluded_materials': sum(m['status'] == 'excluded_by_user' for m in plan['materials']),
            'read_materials': 0, 'failed_materials': 0, 'all_uploaded_materials_read': False,
            'completed_segments': 0, 'failed_segments': 0, 'coverage_complete': False, 'issues': plan['issues'],
            'artifacts':{},'_slide_specs':[],
            'source_reading_complete':False,'visual_coverage':{},'evidence_validation':{},'synthesis':{},'content_plan_complete':False,
            'policy': {k: policy[k] for k in ('reading_workers','reading_max_calls','reading_max_points','reading_input_tokens')}}), job=job)
        cache_root = self._path(job.project_id, 'reading', 'tasks')
        cache_root.mkdir(exist_ok=True)
        results, used_points, used_calls = [], 0, 0
        pending = {};stop = threading.Event();iterator = iter(plan['tasks']);cancelled = False;quota_failure = None
        # The full task SHA remains verified inside every response. A bounded
        # directory label avoids Windows MAX_PATH failures on real uploads.
        def root_for(task):return cache_root / task['id'].split(':',1)[1][:32]
        def accept(task, output, charge=None):
            record = {'task_id': task['id'], 'status': 'completed', 'result': output['result']}
            def op(current):
                acknowledged = {ident for r in results if r['status']=='completed' for ident in r['result']['read_segment_ids']}
                acknowledged.update(output['result']['read_segment_ids'])
                read_materials = sum(m['status'] != 'excluded_by_user' and bool(m['segment_ids'])
                    and set(m['segment_ids']) <= acknowledged
                    and not any(issue.get('material_id') == m['material_id'] for issue in plan['issues'])
                    for m in plan['materials'])
                update_reading(current, {'completed_segments': len(acknowledged), 'read_materials': read_materials})
                update_reading(current, {'completed_reader_tasks': sum(r['status']=='completed' for r in results) + 1})
                if charge:charge.mark_accepted(db.session.get(WuyuWorkflowJob, job.id))
            if charge:
                with charge.accepting(output=output):self._mutate(job.project_id, op, job=job)
                charge.settle()
            else:self._mutate(job.project_id, op, job=job)
            results.append(record)
        runtime = dict(config, OPENAI_MAX_RETRIES=0, WUYU_PLANNER_MAX_RETRIES=0)
        with ThreadPoolExecutor(max_workers=plan['policy']['workers'], thread_name_prefix='wuyu-reader') as pool:
            def fill():
                nonlocal used_points, used_calls, cancelled, quota_failure
                while not cancelled and quota_failure is None and len(pending) < plan['policy']['workers']:
                    try:self._check_cancel(job);task = next(iterator)
                    except StopIteration:return
                    except CancelledWork:cancelled = True;stop.set();return
                    except QuotaError as error:quota_failure=error;stop.set();return
                    try:cached = _cached(task, root_for(task), config, lambda:self._check_cancel(job))
                    except PipelineError as error:
                        if error.code == 'cancelled':cancelled=True;stop.set();return
                        results.append({'task_id':task['id'], 'status':'failed', 'code':error.code,
                            **{key:getattr(error,key) for key in ('source_asset_id','material_id') if getattr(error,key,None)}})
                        continue
                    if cached is not None:
                        accept(task, cached);continue
                    if config.get('WUYU_READING_CACHE_ONLY') is True:
                        raise PipelineError('reading_cache_only_missing','已读缓存不完整；本次未调用模型，资料保留。')
                    tariff = tariffs(get_quota_service(self.app).policy()['settings'])['text']
                    upper = text_points(task['input_tokens'], plan['policy']['output_tokens'], tariff)
                    if used_calls >= plan['policy']['max_calls'] or used_points + upper > plan['policy']['max_total_points']:
                        results.append({'task_id': task['id'], 'status': 'failed', 'code': 'reading_policy_budget'});continue
                    charge = None
                    try:
                        worker = self._pipeline(runtime)
                        charge = WorkflowCharge(self.app, job, 'text', text_input_tokens=task['input_tokens'], text_output_tokens=plan['policy']['output_tokens']).reserve()
                        held = charge.points_upper if charge.points_upper is not None else upper
                        if used_points + held > plan['policy']['max_total_points']:
                            charge.refund('reading_policy_budget')
                            results.append({'task_id': task['id'], 'status': 'failed', 'code': 'reading_policy_budget'})
                            continue
                        used_calls += 1;used_points += held
                        future = pool.submit(run_reader, task, worker, root_for(task), job.id, True, stop.is_set)
                        pending[future] = (task, charge)
                    except Exception as error:
                        if charge:charge.refund()
                        results.append({'task_id': task['id'], 'status': 'failed', 'code': getattr(error,'code','reader_failed')})
                        if isinstance(error,QuotaError):quota_failure=error;stop.set();return
            fill()
            while pending:
                if not cancelled:
                    try:self._check_cancel(job)
                    except CancelledWork:cancelled = True;stop.set()
                    except QuotaError as error:quota_failure=error;stop.set()
                done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    task, charge = pending.pop(future)
                    try:
                        output = future.result()
                        if cancelled:raise CancelledWork()
                        self._check_cancel(job);accept(task, output, charge)
                    except CancelledWork:
                        cancelled = True;stop.set();charge.refund('cancelled_before_acceptance')
                    except Exception as error:
                        charge.refund();results.append({'task_id': task['id'], 'status': 'failed', 'code': getattr(error,'code','reader_failed'),
                            **{key: getattr(error, key) for key in ('source_asset_id','material_id') if getattr(error, key, None)}})
                        if isinstance(error,QuotaError):quota_failure=error;stop.set()
                fill()
        if cancelled:raise CancelledWork()
        if quota_failure:raise quota_failure
        report = apply_omission_audit(finish_reading(plan, results, brief), plan, results)
        reader_failure = reading_failure_code(report, results)
        if (reader_failure and report.get('presentation_structure') != 'explicit_consecutive_original_pages'
                and continuation_options(report)['failure_category'] == 'runtime'):
            # op181 D: a reading that failed at runtime has no trustworthy page plan. Do not
            # present the topic-grouped fallback as a draft; keep findings/artifacts for retry.
            report['slide_specs'] = []
            report['content_plan_complete'] = False
            report['planning_issues'] = [{'code': 'reading_incomplete_no_page_plan',
                'message': '材料阅读未完成，暂不生成逐页草稿；已读结果保留，请重新分析未完成的材料。'}]
        if reader_failure:
            # A failed response cannot establish whether the material is enough.
            for issue in report.get('planning_issues', []):
                if issue.get('code') == 'presentation_evidence_insufficient':
                    issue['message'] = '材料阅读尚未完成，暂不能判断证据是否足够。请先处理阅读失败；无需减少页数或重新上传完好的材料。'
        synthesis_plan = plan
        # Build partial pages only from successfully acknowledged evidence. A
        # synthesis prompt must never receive an unread original tail merely
        # because the complete extraction is still available in the project.
        options = continuation_options(report)
        if (not report['coverage_complete'] and report['findings']
                and options['failure_category'] in {'source_unread','content_plan_invalid'}):
            synthesis_plan = verified_subset(plan, results)
            planning_results = deepcopy(results)
            for record in planning_results:
                if record.get('status') == 'completed':
                    record['result']['findings'] = [f for f in record['result']['findings']
                                                    if f.get('statement_context_complete') is not False]
            subset_report = finish_reading(synthesis_plan, planning_results, brief)
            report['evidence_validation']['incomplete_context_findings_not_used'] = len(report['findings']) - len(subset_report['findings'])
            report['findings'] = subset_report['findings']
            report['evidence_validation']['accepted_findings'] = len(report['findings'])
            for key in ('slide_specs','planning_issues','content_plan_complete','presentation_structure'):
                report[key] = subset_report[key]
        self._mutate(job.project_id,lambda current:update_reading(current,{
            'source_reading_complete':report['coverage_complete'],'coverage_complete':report['coverage_complete'],
            'visual_coverage':report.get('visual_coverage',{}),
            'evidence_validation':{k:v for k,v in report.get('evidence_validation',{}).items()if k!='rejections'},
            'omitted_source_assets':report.get('omitted_source_assets',[]),
            'omitted_material_ids':report.get('omitted_material_ids',[]),
            'selected_scope_complete':report.get('selected_scope_complete',False),
            'synthesis':{'status':'pending'},'content_plan_complete':False}),job=job)
        can_plan_partial = (bool(report['findings']) and continuation_options(report)['failure_category']
                            in {'source_unread','content_plan_invalid'})
        if (report['coverage_complete'] or can_plan_partial) and config.get('WUYU_READING_SYNTHESIS_ENABLED'):
            if used_calls>=plan['policy']['max_calls']:
                raise PipelineError('reading_policy_budget','材料已读取，主模型凝练达到管理员调用预算，后续工作已停止。')
            report = self._reading_synthesize(job,synthesis_plan,report,brief,config)
        report['reading_usage'] = {'new_logical_calls': used_calls, 'estimated_reserved_points_upper_sum': used_points,
                                  'complete_tasks':sum(r['status']=='completed' for r in results)}
        report['web_supplement'] = self._reading_web_supplement(job, brief, policy)
        self._check_cancel(job)
        draft_text = markdown_draft(report, brief, plan['segments'])
        options = continuation_options(dict(report, status='draft_incomplete'))
        if options['can_continue_with_omissions']:
            draft_text = draft_text.replace('可继续制作：否，须先处理未读/失败材料。',
                '可继续制作：可补充/替换未读部分；也可在核对本稿后，明确同意仅使用已验证部分继续。未读部分不参与事实和配图。')
        if report.get('omitted_source_assets'):
            draft_text += '\n\n## 用户明确排除的原图\n\n'
            def audit_text(value):
                text = str(value).replace('\r',' ').replace('\n',' ').replace('<','&lt;').replace('>','&gt;')
                return re.sub(r'([\\`\[\]!*])', r'\\\1', text)
            draft_text += '\n'.join('- 原图标识：'+audit_text(a['source_asset_id'])+'；原因：'+audit_text(a['reason'])
                                    for a in report['omitted_source_assets'])+'\n'
        outputs = {'markdown': ('draft.md','reading_markdown',draft_text),
                   'evidence': ('evidence-index.json','reading_evidence',encoded({'protocol_version':PROTOCOL,'segments':plan['segments'],'materials':plan['materials']})),
                   'report': ('reading-report.json','reading_report',encoded(report))}
        artifacts = {}
        for name, (filename, kind, text) in outputs.items():
            path = self._path(job.project_id, 'reading', f'draft-{draft_revision}-' + job.id + '/' + filename)
            descriptor = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:stream.write(text)
            artifacts[name] = self._record_artifact(job.project_id, path, kind, job, {'draft_revision':draft_revision,'source_revision':source_revision,'coverage_complete':report['coverage_complete']})
        self._mutate(job.project_id, lambda current: update_reading(current, {
            'status':'draft_ready' if report['coverage_complete'] else 'draft_incomplete',
            'coverage_complete':report['coverage_complete'],'completed_segments':report['completed_segments'],
            **{key:report[key]for key in ('submitted_materials','selected_materials','excluded_materials','read_materials','failed_materials','all_uploaded_materials_read')},
            'failed_segments':report['failed_segments'],'issues':report['issues'],'normalization_count':report['normalization_count'],'artifacts':artifacts,
            'coverage_basis': report['coverage_basis'],
            'planning_issues': report['planning_issues'], 'content_plan_complete': report['content_plan_complete'],
            'visual_coverage':report.get('visual_coverage',{}),'synthesis':report.get('synthesis',{}),
            'omitted_source_assets':report.get('omitted_source_assets',[]),
            'omitted_material_ids':report.get('omitted_material_ids',[]),
            'selected_scope_complete':report.get('selected_scope_complete',False),
            'evidence_validation':{k:v for k,v in report.get('evidence_validation',{}).items()if k!='rejections'},
            '_acknowledged_source_asset_ids':report.get('acknowledged_source_asset_ids',[]),
            '_slide_specs':report['slide_specs'], '_fingerprint':plan['fingerprint']}), job=job)
        if reader_failure:
            # Preserve raw responses, successful fragments and review artifacts.
            # The job owner records the failure and job ID; it must not publish
            # a completed task just because an incomplete draft was saved.
            error = PipelineError(reader_failure, '材料阅读未完成；已读结果和诊断草稿已保留。')
            if reader_failure in READER_RESPONSE_CODES:
                error.diagnostics = {'validation_code': reader_failure}
            raise error

    def _reading_synthesize(self, job, plan, report, brief, config):
        from .wuyu_reading_synthesis import synthesis_task,apply_synthesis
        from .wuyu_deep_research import research_guidance
        # Accepted research only steers structure; if the project state cannot be read here,
        # synthesis proceeds exactly as it did without research rather than failing.
        try:guidance=research_guidance(self._read(job.project_id)[1])
        except (AttributeError,KeyError,TypeError):guidance=None
        task=synthesis_task(plan,report,brief,guidance)
        root=self._path(job.project_id,'reading','synthesis/'+task['id'].split(':',1)[1][:32])
        root.mkdir(parents=True,exist_ok=True)
        path=root/'response.json'
        cache_candidates=[(task['id'],path)]
        for previous_id in task.get('compatible_task_ids',[]):
            previous_root=self._path(job.project_id,'reading','synthesis/'+previous_id.split(':',1)[1][:32])
            cache_candidates.append((previous_id,previous_root/'response.json'))
        for source_id,cached_path in cache_candidates:
            if (cached_path.is_symlink() or cached_path.parent.resolve() != cached_path.parent.absolute()
                    or cached_path.exists() and not cached_path.is_file()):
                raise PipelineError('reading_cache_invalid','规划缓存文件超限或不安全。')
            if not cached_path.is_file():continue
            if cached_path.stat().st_size>8*1024*1024:
                raise PipelineError('reading_cache_invalid','规划缓存文件超限或不安全。')
            try:cached=json.loads(cached_path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError):
                raise PipelineError('reading_cache_invalid','规划缓存记录损坏，不能自动重新请求模型。') from None
            if (not isinstance(cached, dict) or cached.get('task_id') != source_id or 'raw' not in cached
                    or cached.get('raw_sha256') != sha(cached['raw'])):
                raise PipelineError('reading_cache_invalid','规划缓存身份或校验和不一致，不能自动重新请求模型。')
            if cached.get('finish_reason') == 'length':raise _cached_plan_invalid('output_truncated')
            try:
                merged=apply_synthesis(cached['raw'],task,report,brief)
            except PipelineError as error:
                if error.code not in CACHED_PLAN_VALIDATION_CODES:raise
                raise _cached_plan_invalid(error.code) from None
            merged['synthesis'].update(response_reused=True,source_task_id=source_id)
            return merged
        if config.get('WUYU_READING_CACHE_ONLY') is True:
            raise PipelineError('reading_cache_only_missing','缺少通过校验的规划缓存；本次未调用模型，已读资料保留。')
        self._check_cancel(job)
        charge=WorkflowCharge(self.app,job,'text',text_input_tokens=task['input_tokens'],text_output_tokens=task['output_tokens']).reserve()
        try:
            worker=self._pipeline(dict(config,OPENAI_MAX_RETRIES=0,WUYU_PLANNER_MAX_RETRIES=0))
            marker=root/('dispatch-'+job.id+'.json')
            descriptor=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8')as stream:stream.write(encoded({'task_id':task['id'],'job_id':job.id}))
            self._check_cancel(job)
            from .wuyu_providers import GEMINI_WRITING_MODELS
            from .wuyu_content_contracts import synthesis_schema
            from .wuyu_reading_synthesis import FORMS
            options={'response_schema':synthesis_schema(brief.get('page_count',12),FORMS)}if getattr(worker.gateway,'models',{}).get('planner')in GEMINI_WRITING_MODELS else{}
            raw=worker.gateway.text('planner',task['prompt'],max_output_tokens=task['output_tokens'],cancel_check=lambda:False,**options)
            metadata=getattr(worker.gateway,'last_response_metadata',{})
            _write(path,{'task_id':task['id'],'raw':raw,'raw_sha256':sha(raw),'finish_reason':metadata.get('finish_reason')})
            if metadata.get('finish_reason')=='length':raise PipelineError('output_truncated','主模型凝练输出被截断；已读结果保留，未发布残缺计划。')
            merged=apply_synthesis(raw,task,report,brief)
            self._check_cancel(job)
            output={'reading_result':True,'model_report':{'provider_called':True,'usage':metadata.get('usage',{})}}
            def accepted(current):
                current['reading']['synthesis']=deepcopy(merged['synthesis'])
                charge.mark_accepted(db.session.get(WuyuWorkflowJob,job.id))
            with charge.accepting(output=output):self._mutate(job.project_id,accepted,job=job)
            charge.settle();return merged
        except Exception:
            charge.refund();raise

    def _reading_web_supplement(self, job, brief, policy):
        _, body = self._read(job.project_id)
        from .wuyu_deep_research import research_public
        research = research_public(body)
        report = research.get('report') or {}
        if research.get('status') != 'accepted' or not report:
            return {'status': 'not_requested', 'sources': [], 'actual_cost': 'unknown', 'model_read': False}
        return {'status': 'accepted_research', 'sources': report.get('sources', []),
                'actual_cost': 'unknown', 'model_read': True, 'source_kind': 'web',
                'uploaded_material_evidence': False, 'fully_verified': False}
