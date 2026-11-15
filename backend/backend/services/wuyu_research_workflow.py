"""Research is a versioned, user-approved job in the existing workflow owner."""
import contextvars
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid

from .wuyu_deep_research import PROFILES, RESEARCH_ACTIONS, digest, signature, signature_current, research_public, run_research, bounded_web_policy, _query_text, can_reuse_research, can_use_existing_materials, materials_recovery_signature
from .wuyu_errors import PipelineError
from .wuyu_source_assets import estimate_tokens
from .wuyu_workflow_quota import WorkflowCharge


# Set while a research start runs on the strength of the composer switch the user sent with.
_ARMED_CONSENT = contextvars.ContextVar('wuyu_armed_research_consent', default=False)


def default_research_prompt(brief):
    parts = [str(brief.get(key) or '').strip() for key in ('topic', 'goal', 'requirements')]
    return '\n'.join(dict.fromkeys(part for part in parts if part))[:6000]


def _raw_plan_queries(result):
    if not isinstance(result, dict):
        return []
    keys = [k for k in result if isinstance(k, str) and re.sub(r'[ \t\r\n\f\v]', '', k) == 'queries']
    values = result[keys[0]] if len(keys) == 1 else None
    if not isinstance(values, list) or not 1 <= len(values) <= 3 or not all(isinstance(v, str) for v in values):
        return []
    return list(dict.fromkeys(v.strip() for v in values))


def cached_research_step(research, step, request, plan_queries):
    """Exact current prompt first; one proved query-whitespace legacy key only.

    Every other prompt byte, including question, sources and previous summary,
    stays part of the original exact key. Never scan unrelated saved responses.
    """
    steps = research['_steps']

    def lookup(key):
        saved = steps[key]
        if (not isinstance(saved, dict) or not saved.get('status')
                or saved['status'] == 'completed' and not isinstance(saved.get('result'), dict)):
            raise PipelineError('research_dispatch_uncertain', '上次调研调用结果尚未确认，已停止重复扣费；可查看已获得的资料。')
        return saved

    key = digest({'step': step, 'prompt': json.dumps(request, ensure_ascii=False)})
    if key in steps:
        return lookup(key)
    legacy = deepcopy(request)
    if step == 'plan' and isinstance(request.get('already_searched'), list):
        pairs = {(op.get('kind'), op.get('query')) for op in (research.get('_seed_operations') or [])
                 if op.get('status') in {'completed', 'partial'} and op.get('kind') in {'search', 'scholar'}
                 and isinstance(op.get('query'), str)}
        canonical = [{'kind': kind, 'query': query} for kind, query in sorted({(kind, _query_text(query)) for kind, query in pairs})]
        if canonical != request['already_searched']:
            return None
        legacy['already_searched'] = [{'kind': kind, 'query': query} for kind, query in sorted(pairs)]
    elif step == 'reflect' and plan_queries and isinstance(request.get('searched'), list):
        if list(dict.fromkeys(_query_text(query) for query in plan_queries)) != request['searched']:
            return None
        legacy['searched'] = list(plan_queries)
    else:
        return None
    legacy_key = digest({'step': step, 'prompt': json.dumps(legacy, ensure_ascii=False)})
    return lookup(legacy_key) if legacy_key in steps else None


class ResearchWorkflowMixin:
    def _start_armed_research(self, project_id, idempotency_key=None):
        """request_research after the user sent with 联网搜索/深度探索 on: start that search now.

        Returns None (normal request_research) when nothing is armed, the search is already
        running, or there is no request user."""
        from flask import has_request_context, request
        from .wuyu_research_intent import consume, peek
        actor = getattr(request, 'current_user', None) if has_request_context() else None
        if actor is None or not peek(actor.id):
            return None
        _, body = self._read(project_id)
        if (body.get('research') or {}).get('status') in {'queued', 'running'}:
            return None
        mode = consume(actor.id)
        prompt = default_research_prompt(body.get('brief') or {})
        if not mode or not prompt:
            return None
        token = _ARMED_CONSENT.set(True)
        try:
            return self.execute_action(project_id, 'confirm_research',
                {'mode': mode, 'prompt': prompt, 'acknowledge_cost': True, 'auto_accept': True},
                idempotency_key=(idempotency_key + ':armed')[:120] if isinstance(idempotency_key, str) else None)
        finally:
            _ARMED_CONSENT.reset(token)

    def _research_ui(self, project_id):
        if _ARMED_CONSENT.get():
            return
        from .wuyu_user_consent import require_user_interaction, RESEARCH
        require_user_interaction(self.app, project_id, RESEARCH)

    def _research_validate(self, body, action, payload, running=False):
        from .wuyu_workflow_service import WorkflowAPIError
        state = body.get('research') or {}
        if action in {'confirm_template_choice', 'select_references'} and not running:
            # op177: a template click bumps the epoch, which used to cancel the
            # paid research round silently. Serialize like template_reading_guard.
            from models.wuyu_workflow import WuyuWorkflowJob
            if WuyuWorkflowJob.query.filter_by(project_id=body['workflow']['project_id'], action='run_research').filter(
                    WuyuWorkflowJob.status.in_(['queued', 'running'])).first() is not None:
                raise WorkflowAPIError('资料调研正在进行，请等本轮调研完成后再确认模板；本次操作没有中断调研。',
                                       'TEMPLATE_RESEARCH_IN_PROGRESS', 409)
        if action in {'confirm_research', 'supplement_research', 'accept_research'}:
            self._research_ui(body['workflow']['project_id'])
        if action == 'request_research' and payload:
            raise WorkflowAPIError('请求调研选项不需要额外参数。')
        if action in {'confirm_research', 'supplement_research'}:
            preserve = action == 'confirm_research' and payload.get('preserve_research') is True
            fields = {'mode', 'prompt', 'acknowledge_cost'} | ({'report_id'} if action == 'supplement_research' else set())
            if preserve:
                fields |= {'preserve_research', 'report_id'}
            # auto_accept: the user chose web/deep search in the composer before sending. That click is the
            # consent to use the results, so they go straight into the materials when the search finishes.
            if 'auto_accept' in payload and type(payload['auto_accept']) is not bool:
                raise WorkflowAPIError('auto_accept must be an explicit boolean')
            if (set(payload) - {'auto_accept'} != fields or payload.get('mode') not in PROFILES
                    or not isinstance(payload.get('prompt'), str) or len(payload['prompt']) > 6000
                    or payload['mode'] != 'materials' and (not payload['prompt'].strip() or payload['acknowledge_cost'] is not True)
                    or type(payload['acknowledge_cost']) is not bool):
                raise WorkflowAPIError('请选择调研范围，并确认公开研究问题与消耗提示。')
            if preserve:
                if (set(payload) != fields or payload['mode'] != 'materials' or payload['acknowledge_cost'] is not False
                        or payload['prompt'] != state.get('prompt', '') or payload['report_id'] != state.get('report_id')
                        or not can_use_existing_materials(body)):
                    raise WorkflowAPIError('当前研究或材料版本已变化，请读取最新状态后选择。', 'RESEARCH_REVISION_CONFLICT', 409)
                from models.wuyu_workflow import WuyuWorkflowJob
                if WuyuWorkflowJob.query.filter_by(project_id=body['workflow']['project_id']).filter(
                        WuyuWorkflowJob.status.is_(None) | ~WuyuWorkflowJob.status.in_(['completed', 'failed', 'cancelled', 'interrupted'])).first() is not None:
                    raise WorkflowAPIError('当前仍有任务运行，请等待完成后再选择材料范围。', 'RESEARCH_RECOVERY_BUSY', 409)
            elif (action == 'confirm_research' and payload['mode'] == 'materials'
                    and state.get('status') in {'failed', 'paused'} and state.get('mode') in {'web', 'deep'}):
                raise WorkflowAPIError('请使用保留原研究证据的材料恢复操作。', 'RESEARCH_REVISION_CONFLICT', 409)
            if action == 'supplement_research' and (not research_public(body)['can_supplement']
                    or payload.get('report_id') != state.get('report_id') or payload['mode'] == 'materials'):
                raise WorkflowAPIError('请先中断当前调研或等待本轮结束，再基于当前版本补充意见。', 'RESEARCH_REVISION_CONFLICT', 409)
            if payload['mode'] != 'materials':
                from .wuyu_runtime_settings import get_research_policy
                if not get_research_policy(dict(self.app.config)).get('web_enabled'):
                    raise WorkflowAPIError('管理员暂未开启联网调研。', 'WEB_DISABLED', 409)
        if action == 'accept_research':
            if can_reuse_research(body):
                if set(payload) != {'report_id', 'allow_partial'} or payload.get('allow_partial') is not False or payload.get('report_id') != state['report_id']:
                    raise WorkflowAPIError('请先查看保留的研究资料，再确认是否沿用于当前需求。', 'RESEARCH_REVIEW_REQUIRED', 409)
                from models.wuyu_workflow import WuyuWorkflowJob
                if WuyuWorkflowJob.query.filter_by(project_id=body['workflow']['project_id']).filter(
                        WuyuWorkflowJob.status.is_(None) | ~WuyuWorkflowJob.status.in_(['completed', 'failed', 'cancelled', 'interrupted'])).first() is not None:
                    raise WorkflowAPIError('当前仍有任务运行，请等待完成后再确认沿用资料。', 'RESEARCH_REUSE_BUSY', 409)
                return
            if (set(payload) != {'report_id', 'allow_partial'} or type(payload['allow_partial']) is not bool
                    or state.get('status') != 'review_required' or not signature_current(state.get('signature'), body)
                    or payload['report_id'] != state.get('report_id')
                    or state.get('report', {}).get('partial') and not payload['allow_partial']):
                raise WorkflowAPIError('请先检查当前调研来源和缺失项，再确认使用这版资料。', 'RESEARCH_REVIEW_REQUIRED', 409)
        planning = {'analyze_materials', 'revise_draft', 'continue_from_draft', 'generate_outline',
                    'generate_samples', 'regenerate_samples', 'generate_next', 'generate_focused', 'generate_images'}
        # Continuation may also be a purely local export or a trace of images
        # already reviewed. Do not revoke delivery of existing work.
        if action == '_continue' and not body.get('slide_specs'):
            planning.add('_continue')
        if action in planning | {'run_research'}:
            public = research_public(body)
            if action == 'run_research':
                # A failed deep round keeps its original evidence after a material-only
                # choice. That retained consent must never restart paid research.
                ready = (not state.get('materials_confirmation') and public['consent_current']
                         and state.get('mode') in {'web','deep'} and state.get('status') in {'queued','running','failed'})
            else:
                ready = public['ready_for_planning']
            if not ready:
                message = '请先在对话中选择资料调研范围；联网调研完成后，查看来源与总结并确认使用。'
                if running: raise PipelineError('RESEARCH_REVIEW_REQUIRED', message)
                raise WorkflowAPIError(message, 'RESEARCH_REVIEW_REQUIRED', 409)

    def _research_apply(self, body, action, payload):
        if action in {'pause','cancel'} and (body.get('research') or {}).get('status') in {'queued','running'}:
            body['research']['status']='paused'
            body['research'].setdefault('progress', {})['stage']='paused'
        if (action == 'resume' and (body.get('resume_action') or {}).get('action') == 'run_research'
                and (body.get('research') or {}).get('status') == 'paused'
                and body['research'].get('mode') in {'web', 'deep'}):
            # op177: continue the same paused round (cached steps are not charged twice).
            body['research']['status'] = 'queued'
            body['research'].setdefault('progress', {})['stage'] = 'queued'
        if action == 'request_research':
            if not research_public(body)['ready_for_planning']:
                body.setdefault('research', {})['requested'] = True
            elif not body.get('research'):
                body['research'] = {'requested': True}
        if action in {'confirm_research', 'supplement_research'}:
            if action == 'confirm_research' and payload.get('preserve_research') is True:
                # Preserve the failed research verbatim: no sources become materials,
                # no report becomes accepted, and no model/search job is enqueued.
                state = body['research']
                state['materials_confirmation'] = {'report_id': state['report_id'],
                    'original_signature': state['signature'], 'signature': signature(body),
                    'evidence_signature': materials_recovery_signature(body),
                    'confirmed_at': datetime.now(timezone.utc).isoformat(),
                    'confirmed_via': 'research_confirmation_ui'}
                body['epoch'] += 1
                body['paused'] = False
                body.pop('resume_action', None)
                self._research_invalidate_content(body)
                return
            previous = deepcopy(body.get('research') or {})
            supplement = action == 'supplement_research'
            # Stop obsolete work atomically before authorizing another scope.
            body['epoch'] += 1
            body['paused'] = False
            body.pop('resume_action', None)
            body['research'] = {'requested': True, 'signature': signature(body), 'mode': payload['mode'],
                'prompt': payload['prompt'].strip(), 'report_id': str(uuid.uuid4()),
                'status': 'accepted' if payload['mode'] == 'materials' else 'queued',
                'confirmed_at': datetime.now(timezone.utc).isoformat(), 'confirmed_via': 'research_confirmation_ui',
                'profile': deepcopy(PROFILES[payload['mode']]), '_steps': {},
                **({'auto_accept': True} if payload.get('auto_accept') is True else {}),
                'progress': {'stage': 'queued', 'reserved_tokens': 0, 'model_calls': 0}}
            state = body['research']
            state['round'] = previous.get('round', 1) + 1 if supplement else 1
            state['feedback_history'] = (previous.get('feedback_history', []) + [{
                'round':state['round'], 'prompt':state['prompt'], 'confirmed_at':state['confirmed_at']}])[-20:] if supplement else []
            if supplement:
                sources = previous.get('partial_sources') or (previous.get('report') or {}).get('sources', [])
                operations = previous.get('progress', {}).get('operations') or (previous.get('report') or {}).get('operations', [])
                state.update(report=deepcopy(previous.get('report')), previous_report=deepcopy(previous.get('report') or previous.get('previous_report')),
                    partial_sources=deepcopy(sources), _seed_sources=deepcopy(sources), _seed_operations=deepcopy(operations),
                    _base_question=previous.get('_base_question') or previous.get('prompt', ''))
                state['progress'].update(source_count=len(sources), operations=deepcopy(operations))
            # A new explicit choice must not reuse previously researched sources.
            for material in body['materials']:
                if material.get('origin') == 'web_research':
                    body.setdefault('reading_exclusions', {})[material['id']] = '已切换调研范围，保留历史原文'
            self._research_invalidate_content(body)
        if action == 'accept_research':
            if can_reuse_research(body):
                # Keep the original question/signature/report/material/usage and every export.
                # The separate receipt acknowledges source reuse, never a new search budget.
                state = body['research']
                state['reuse_confirmation'] = {'report_id': state['report_id'],
                    'original_signature': state['signature'], 'signature': signature(body),
                    'confirmed_at': datetime.now(timezone.utc).isoformat(),
                    'confirmed_via': 'research_confirmation_ui'}
                return
            state = body['research']
            report = state['report']
            chunks = [{'id': state['report_id'] + ':' + s['id'], 'text': s['text'],
                'source': {'url': s['url'], 'title': s['title'], 'source_kind': 'web_research',
                    'retrieved_at': report['completed_at'], 'content_scope': s['content_scope'],
                    **{key:deepcopy(s[key]) for key in ('provider','doi','authors','date') if key in s}}}
                for s in report['sources'] if s['text']]
            text = '\n\n'.join(c['text'] for c in chunks)
            body['materials'].append({'id': state['report_id'], 'filename': '公开调研原文摘录.md',
                'role': 'material', 'origin': 'web_research', 'status': 'completed', 'text': text,
                'chunks': chunks, 'sha256': digest(text), 'bytes': len(text.encode()),
                'source_assets': [], 'source_structured_data': {}, 'warnings': [],
                'metadata': {'research_report_id': state['report_id'], 'not_user_upload': True}})
            state['status'] = 'accepted'
            state['accepted_at'] = datetime.now(timezone.utc).isoformat()
            body['epoch'] += 1
            self._research_invalidate_content(body)
            # Reading/generation remain a separate explicit action.

    def _research_invalidate_content(self, body):
        """Keep immutable exports/history, invalidate content bound to old sources."""
        body['outline'], body['slide_specs'], body['planning_report'] = [], [], {}
        body['workflow'] = self._new_state(body['workflow']['project_id'], body['brief'], body['workflow']['review_policy'])
        self._reading_invalidate(body)

    def _run_research(self, job, pipeline):
        from models import db
        from models.wuyu_workflow import WuyuWorkflowJob
        from .wuyu_pipeline import json_object
        from .wuyu_web_research import WuyuWebResearch
        from .wuyu_runtime_settings import get_research_policy
        from .wuyu_providers import GEMINI_WRITING_MODELS
        from .wuyu_planner_context import DEEPSEEK_MODELS
        _, body = self._read(job.project_id)
        state = body['research']; profile = deepcopy(state['profile'])
        # op 175: DeepSeek official (deepseek-flash, 1M context) is also an approved research model.
        if not self.app.config.get('TESTING') and pipeline.gateway.models.get('planner') not in GEMINI_WRITING_MODELS | DEEPSEEK_MODELS:
            raise PipelineError('research_model_required', '调研需要管理员配置 Gemini 或 DeepSeek 内容模型。')
        policy = get_research_policy(job._runtime_config, json.loads(job.payload or '{}').get('__runtime_snapshot'))
        policy = bounded_web_policy(state, policy)
        profile['searches'], profile['fetches'] = policy['web_search_limit'], policy['web_fetch_limit']
        if not profile['searches']:
            raise PipelineError('research_budget_disabled', '联网搜索预算为零，请选择仅用现有材料或联系管理员。')
        service = WuyuWebResearch(job._runtime_config, actor_id=job.actor_user_id, project_id=job.project_id,
                                  scope_id='research:' + state['report_id'], policy=policy)
        def check():
            job._quota_checked_at = 0
            self._check_cancel(job)
        def checkpoint(update):
            def save(b):
                b['research']['partial_sources']=deepcopy(update.get('sources',[]))
                b['research']['progress'].update({k:deepcopy(v) for k,v in update.items() if k!='sources'})
            try:
                check()
                self._mutate(job.project_id, save, job=job)
            except Exception as error:
                from .wuyu_workflow_service import CancelledWork
                if not isinstance(error, CancelledWork): raise
                # A completed response may arrive at the same instant as Stop.
                # Preserve it only for that paused round, never a new scope.
                def preserve(b):
                    current = b.get('research') or {}
                    if current.get('report_id') != state['report_id'] or current.get('status') != 'paused':
                        raise CancelledWork()
                    save(b); current['progress']['stage'] = 'paused'
                self._mutate(job.project_id, preserve)
                raise
        plan_queries = []
        def model(step, request):
            check()
            prompt = json.dumps(request, ensure_ascii=False)
            key = digest({'step':step, 'prompt':prompt})
            _, current = self._read(job.project_id)
            research = current['research']; saved = cached_research_step(research, step, request, plan_queries)
            if saved:
                if saved['status'] == 'completed': return deepcopy(saved['result'])
                raise PipelineError('research_dispatch_uncertain', '上次调研调用结果尚未确认，已停止重复扣费；可查看已获得的资料。')
            incoming = estimate_tokens(prompt) + 1000
            # Gemini thinking shares the output limit. An 8K cap can exhaust
            # reasoning before JSON is emitted; reserve the configured 32K cap.
            outgoing = min(32768, int(job._runtime_config.get('WUYU_PLANNER_OUTPUT_TOKENS',32768)))
            progress = research['progress']
            if progress['model_calls'] >= profile['model_calls'] or progress['reserved_tokens'] + incoming + outgoing > profile['token_limit']:
                raise PipelineError('research_budget_exhausted', '本次调研达到已确认的 token 上限，已保存中间结果。')
            charge = WorkflowCharge(self.app, job, 'text', text_input_tokens=incoming, text_output_tokens=outgoing).reserve()
            try:
                def dispatch(b):
                    b['research']['status'] = 'running'
                    b['research']['_steps'][key] = {'status':'dispatched','step':step}
                    p=b['research']['progress']; p.update(stage=step,model_calls=p['model_calls']+1,
                        reserved_tokens=p['reserved_tokens']+incoming+outgoing)
                self._mutate(job.project_id, dispatch, job=job)
                raw = pipeline.gateway.text('planner', prompt, max_output_tokens=outgoing, cancel_check=check)
                metadata = pipeline.gateway.last_response_metadata
                if metadata.get('finish_reason') == 'length':
                    raise PipelineError('output_truncated','调研输出被截断，已停止并保留此前结果。')
                result = json_object(raw)
                check()
                output={'reading_result':True,'model_report':{'provider_called':True,'usage':metadata.get('usage',{})}}
                def save(b):
                    b['research']['_steps'][key] = {'status':'completed','step':step,'result':result,
                        'usage':deepcopy(metadata.get('usage',{}))}
                    charge.mark_accepted(db.session.get(WuyuWorkflowJob,job.id))
                with charge.accepting(output=output): self._mutate(job.project_id,save,job=job)
                charge.settle()
                return result
            except BaseException:
                charge.refund(); raise
        def tracked_model(step, request):
            result = model(step, request)
            if step == 'plan':
                plan_queries[:] = _raw_plan_queries(result)
            return result
        failures=[]
        def web(kind, value):
            check()
            try:
                result = (service.deepseek_search(value, cancel_check=check) if kind=='search'
                          else service.scholar_search(value, cancel_check=check) if kind=='scholar'
                          else service.fetch(value, cancel_check=check))
                return result
            except PipelineError as error:
                if error.code not in {'web_provider_failed','web_provider_unavailable','web_rate_limit','web_invalid_response','web_invalid_url','web_not_configured','web_empty_response','web_scope_limit','web_daily_limit'}: raise
                failures.append({'kind':kind,'code':error.code})
                return {'status':'failed','results':[],'error_code':error.code}
        prompt = state['prompt'] if not state.get('_base_question') else (
            '原始研究问题：' + state['_base_question'] + '\n本轮用户补充意见：' + state['prompt'])
        report = run_research(prompt, profile, tracked_model, lambda q:web('search',q),
                              lambda url:web('fetch',url), checkpoint, check,
                              scholar=lambda q:web('scholar',q), seed_sources=state.get('_seed_sources'),
                              seed_operations=state.get('_seed_operations'), previous_report=state.get('previous_report'))
        report.update(completed_at=datetime.now(timezone.utc).isoformat(), failures=failures,
                      partial=bool(failures or report['omitted_findings']))
        def finish(b):
            b['research'].update(status='review_required',report=report)
            b['research']['progress']['stage']='review_required'
            if b['research'].get('auto_accept') is True:
                self._research_apply(b, 'accept_research', {'report_id': b['research']['report_id'], 'allow_partial': True})
                b['research']['progress']['stage'] = 'accepted'
        self._mutate(job.project_id,finish,job=job)
