"""WorkflowViews: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from models.wuyu_workflow import WuyuWorkflowJob
from .wuyu_workflow_contracts import WorkflowAPIError
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class WorkflowViewsPorts:
    _active_runtime_config: Callable[..., Any]
    _current_hybrid_export: Callable[..., Any]
    _hybrid_signature: Callable[..., Any]
    _image_signature: Callable[..., Any]
    _job_dict: Callable[..., Any]
    _pipeline: Callable[..., Any]
    _public_artifact: Callable[..., Any]
    _public_material: Callable[..., Any]
    _public_recipe: Callable[..., Any]
    _read: Callable[..., Any]
    _reading_public: Callable[..., Any]
    _template_references: Callable[..., Any]
    app: Callable[..., Any]
    hook__failure_diagnostics: Callable[..., Any]
    hook__failure_message: Callable[..., Any]
    hook__public_runtime: Callable[..., Any]
    hook__public_source: Callable[..., Any]
    hook__safe_failure_code: Callable[..., Any]
    hook_current_relayout_source: Callable[..., Any]
    hook_image_export_ready: Callable[..., Any]
    hook_material_usage: Callable[..., Any]
    hook_phase: Callable[..., Any]
    hook_public_template_choice: Callable[..., Any]
    hook_research_public: Callable[..., Any]
    material_limits: Callable[..., Any]
    native_complete: Callable[..., Any]
    provider_status: Callable[..., Any]
    queue_snapshot: Callable[..., Any]



class WorkflowViews:
    def __init__(self, ports: WorkflowViewsPorts):
        self.ports = ports

    def provider_status(self):
        # Never return environment/config values or instantiate a remote client.
        hook = self.ports.app().config.get('WUYU_PROVIDER_STATUS')
        if callable(hook):
            return hook()
        from services.wuyu_providers import WuyuGateway, MODELS
        models = dict(MODELS)
        try:
            gateway = WuyuGateway(self.ports._active_runtime_config())
            models, configured = gateway.models, gateway.configuration_status()
        except Exception:
            configured = {role: False for role in MODELS}
            # Keep existing projects inspectable when settings need repair. Name
            # the selected profile honestly even when it has no credential; do
            # not report the old environment model as a silent fallback.
            try:
                from services.wuyu_runtime_settings import get_runtime_settings
                selected = get_runtime_settings(dict(self.ports.app().config))
                models['image'] = selected['image_model']
            except Exception:
                pass
        return {public: {'model': models[role], 'configured': configured[role]}
                for public, role in [('domain', 'planner'), ('image', 'image'), ('trace', 'trace')]}

    def test_providers(self, role=None):
        if role is not None and role not in {'domain', 'text', 'planner', 'image', 'trace', 'vision'}:
            raise WorkflowAPIError('Unknown provider role')
        role = {'domain': 'planner', 'text': 'planner', 'vision': 'trace'}.get(role, role)
        try:
            pipeline = self.ports._pipeline(self.ports._active_runtime_config())
        except (ValueError, OSError):
            raise WorkflowAPIError('Runtime model settings require administrator repair', 'RUNTIME_SETTINGS_INVALID', 503) from None
        if not hasattr(pipeline, 'test_providers'):
            raise WorkflowAPIError('Provider testing is not available', 'PROVIDER_TEST_UNAVAILABLE', 503)
        try:
            return pipeline.test_providers(role=role)
        except Exception:
            raise WorkflowAPIError('Provider connection test failed; inspect server configuration', 'PROVIDER_TEST_FAILED', 503) from None

    def _public_artifact(self, project_id, item, topic=None):
        from .wuyu_delivery_names import delivery_name
        url = f"/api/projects/{project_id}/workflow/artifacts/{item['id']}"
        metadata = item.get('metadata') or {}
        preview_ids = metadata.get('preview_artifact_ids', [])
        previews = [f'/api/projects/{project_id}/workflow/artifacts/{ident}' for ident in preview_ids]
        return {k: v for k, v in item.items() if k != 'relative_path'} | {
            'filename':delivery_name(item,topic),
            'format': metadata.get('format'), 'url': url,
            'preview_url': url if item['mime_type'].startswith('image/') else previews[0] if previews else None,
            'preview_urls': previews, 'download_url': url + '?download=1'}

    def snapshot(self, project_id):
        revision, body = self.ports._read(project_id)
        workflow = deepcopy(body['workflow'])
        urls = {'artifact:' + a['id']: self.ports._public_artifact(project_id, a)['url'] for a in body['artifacts']}
        urls.update({'artifact:' + a['id'] + '.pptx': self.ports._public_artifact(project_id, a)['url']
                     for a in body['artifacts'] if a['kind'] == 'pptx'})
        root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
        path_urls = {str(root / a['relative_path']).replace('\\', '/'): self.ports._public_artifact(project_id, a)['url']
                     for a in body['artifacts']}
        def replace(value):
            if isinstance(value, dict):
                public = {}
                for key, item in value.items():
                    if (key == 'path' or key.endswith('_path')) and isinstance(item, str) and (Path(item).is_absolute() or re.match(r'^[A-Za-z]:[\\/]', item)):
                        public[key] = path_urls.get(item.replace('\\', '/'))
                    else:
                        public[key] = replace(item)
                return public
            if isinstance(value, list):
                return [replace(item) for item in value]
            return urls.get(value, value) if isinstance(value, str) else value
        workflow = replace(workflow)
        workflow['phase'] = self.ports.hook_phase(body['workflow'])
        selection = self.ports.hook_public_template_choice(body)
        if selection['required'] and workflow['phase'] in {'samples', 'template_review', 'generation_request', 'page_generation'}:
            workflow['phase'] = 'template_selection'
        reading = self.ports._reading_public(body, replace)
        if reading and reading['status'] != 'accepted':
            workflow['phase'] = 'reading' if reading['status'] in {'idle', 'reading'} else 'draft_review'
        relayout_pending = body['brief'].get('task_mode') == 'relayout' and not self.ports.hook_current_relayout_source(body)
        if relayout_pending:
            workflow['phase'], workflow['pages'], reading = 'relayout_intake', [], None
        mode = 'demo' if str(self.ports.app().config.get('WUYU_DEMO_MODE') or os.getenv('WUYU_DEMO_MODE', '')).lower() in {'true', '1'} else 'live'
        from services.wuyu_design_memory import native_complete
        if self.ports.app().config.get('TESTING') and self.ports.app().config.get('WUYU_PIPELINE_FACTORY'):
            mode = 'test_stub'
        return {'project_id': project_id, 'revision': revision, 'epoch': body['epoch'], 'workflow': workflow,
                'template_selection': selection,
                'export_capabilities': {'image_pptx': self.ports.hook_image_export_ready(body['workflow']),
                    'save_personal_template': self.ports.native_complete(native_complete, body),
                    'image_signature': self.ports._image_signature(body),
                    'hybrid_pptx': self.ports.hook_image_export_ready(body['workflow']),
                    'hybrid_signature': self.ports._hybrid_signature(body),
                    'editable_pptx': self.ports.hook_phase(body['workflow']) in {'quality_check', 'ready_to_export', 'exported'}},
                'hybrid_export': self.ports._current_hybrid_export(project_id, body),
                'run_mode': mode,
                'brief': dict(body['brief'], review_policy=body['workflow']['review_policy'], **({'page_count': None, 'source_page_count_pending': True} if relayout_pending else {})),
                'planning_report': body.get('planning_report', {}),
                'relayout': replace(body.get('relayout')),
                'reading': reading,
                'research': self.ports.hook_research_public(body),
                'reference_recipe': self.ports._public_recipe(body['reference_recipe']), 'outline': self.ports.hook__public_source(body['outline']),
                'slide_specs': self.ports.hook__public_source(body['slide_specs']), 'jobs': [self.ports._job_dict(j, body) for j in WuyuWorkflowJob.query.filter_by(project_id=project_id).order_by(WuyuWorkflowJob.created_at.desc()).limit(100)],
                'artifacts': [self.ports._public_artifact(project_id, a, body) for a in body['artifacts']],
                'materials': [dict(self.ports._public_material(project_id, item),
                    selected_for_content=item.get('role') in (None, '', 'material') and item['id'] not in body.get('reading_exclusions', {}),
                    exclusion_reason=body.get('reading_exclusions', {}).get(item['id'])) for item in body['materials']],
                'material_limits': self.ports.material_limits(),
                'material_usage': self.ports.hook_material_usage(body['materials']),
                'material_deletions': [{'material_id': ident, 'status': value['status']}
                    for ident, value in body.get('material_deletions', {}).items() if value['status'] == 'pending'],
                'template_references': self.ports._template_references(project_id, body),
                'history': body['history'][-200:], 'provider_status': self.ports.provider_status(), 'paused': body['paused'],
                **self._design_habit_block(project_id, body['brief'])}

    def _design_habit_block(self, project_id, brief):
        """op196: learned-habit / applied-habit / deck-type notice for the conversation; never breaks a snapshot."""
        try:
            from services.wuyu_habit_learning import get_learner
            block = get_learner(self.ports.app()).notice_for(project_id, brief)
        except Exception as exc:
            self.ports.app().logger.warning('Design habit notice skipped: type=%s', type(exc).__name__)
            return {}
        return {'design_habits': block} if block else {}

    def _job_dict(self, job, body=None):
        progress = json.loads(job.progress or '{}')
        error_details = progress.get('error', {})
        if not isinstance(error_details, dict):
            error_details = {}
        error_code = self.ports.hook__safe_failure_code(job.error_code) if job.error_code else None
        error_message = self.ports.hook__failure_message(error_code) if error_code else None
        diagnostics = self.ports.hook__failure_diagnostics(error_details.get('diagnostics'))
        if error_message and error_code == 'unsupported_numeric_claim' and diagnostics.get('unsupported_numbers'):
            where = ('第' + str(diagnostics['page']) + '页') if diagnostics.get('page') else '某一页'
            error_message += '具体是' + where + '的：' + '、'.join(diagnostics['unsupported_numbers']) + '。可删去这些数字，或补充含有它们的来源后重试。'
        if 'error' in progress:
            progress['error'] = {'code': error_code, 'message': error_message,
                                 'retryable': error_details.get('retryable') is True, 'diagnostics': diagnostics}
        job_payload = json.loads(job.payload or '{}')
        runtime = job_payload.get('__runtime_snapshot')
        from .wuyu_page_execution import POLICY_KEY, public_execution_policy
        execution_policy = public_execution_policy(job_payload.get(POLICY_KEY))
        queue = None
        try:
            queue = self.ports.queue_snapshot(job)
        except Exception:
            queue = None  # a position is a courtesy; never let it break reading a job
        delivery={}
        if job.status=='failed' and job.action in {'start_tracing','_continue','retry_failed','resume'}:
            current=body if body is not None else self.ports._read(job.project_id)[1]
            hybrid=self.ports._current_hybrid_export(job.project_id,current)
            if hybrid:
                metadata=hybrid.get('metadata')or{}
                delivery={'delivery_status':'completed_mixed','editable_pages':metadata.get('editable_pages',[]),
                          'image_only_pages':metadata.get('image_only_pages',[]),
                          'delivery_message':'混合可编辑版已可下载；未通过描摹的页面保留图片，可只重试这些页。'}
        return {'id': job.id, 'task_id': job.task_id, 'action': job.action, 'status': job.status, 'input_epoch': job.input_epoch,
                **delivery,
                **({'queue': queue} if queue else {}),
                **({'runtime': self.ports.hook__public_runtime(runtime)} if isinstance(runtime, dict) else {}),
                **({'execution_policy': execution_policy, 'execution_page_workers': execution_policy['page_workers']}
                   if execution_policy else {}),
                'progress': progress, 'cancel_requested': job.cancel_requested,
                'error_code': error_code, 'error_message': error_message,
                'retryable': error_details.get('retryable') is True, 'diagnostics': diagnostics, 'retry_of': job.retry_of,
                'created_at': job.created_at.isoformat() + 'Z'}

    def get_job(self, project_id, job_id):
        job = WuyuWorkflowJob.query.filter_by(id=job_id, project_id=project_id).first()
        if not job:
            raise WorkflowAPIError('Job not found', 'JOB_NOT_FOUND', 404)
        return self.ports._job_dict(job)
