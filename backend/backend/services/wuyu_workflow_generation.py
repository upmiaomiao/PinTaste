"""PageGeneration: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from models import db, Project, Page, ReferenceFile
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_state import WorkflowError
from services.wuyu_errors import PipelineError
from . import wuyu_page_retry as page_retry
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class PageGenerationPorts:
    _artifact_file: Callable[..., Any]
    _bind_source_assets: Callable[..., Any]
    _check_cancel: Callable[..., Any]
    _continue: Callable[..., Any]
    _deliver_focused_images: Callable[..., Any]
    _event: Callable[..., Any]
    _export_additional: Callable[..., Any]
    _generate_batch: Callable[..., Any]
    _generate_next: Callable[..., Any]
    _image_signature: Callable[..., Any]
    _model_batch: Callable[..., Any]
    _model_workers: Callable[..., Any]
    _mutate: Callable[..., Any]
    _normalize_specs: Callable[..., Any]
    _outline: Callable[..., Any]
    _outline_reserved: Callable[..., Any]
    _path: Callable[..., Any]
    _pipeline_recipe: Callable[..., Any]
    _plan_outline: Callable[..., Any]
    _progress: Callable[..., Any]
    _read: Callable[..., Any]
    _record_artifact: Callable[..., Any]
    _relayout_seed_unchanged: Callable[..., Any]
    _require_template_choice: Callable[..., Any]
    _sync_page: Callable[..., Any]
    _trace_and_export: Callable[..., Any]
    _validate_specs: Callable[..., Any]
    app: Callable[..., Any]
    hook_WorkflowCharge: Callable[..., Any]
    hook__design_requirements: Callable[..., Any]
    hook__generation_report: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook__material_coverage: Callable[..., Any]
    hook__model_report: Callable[..., Any]
    hook__public_source: Callable[..., Any]
    hook_apply_event: Callable[..., Any]
    hook_phase: Callable[..., Any]



class PageGeneration:
    def __init__(self, ports: PageGenerationPorts):
        self.ports = ports

    def _outline(self, job, pipeline):
        _, body = self.ports._read(job.project_id)
        self.ports._progress(job, 'planning', body)
        charge = self.ports.hook_WorkflowCharge(self.ports.app(), job, 'text').reserve()
        try:
            return self.ports._outline_reserved(job, pipeline, body, charge)
        except BaseException:
            charge.refund()
            raise

    def _plan_outline(self, pipeline, body, materials, job):
        """Ask the planner for the outline; if it invented numbers, ask once more, naming them.

        The numeric check is strict on purpose (no figure without evidence), but it used to end
        the whole job on the first miss. It was the most frequent failure in production, and it
        is almost always the model padding a page with plausible-looking figures. One rewrite
        that names exactly which figures to drop fixes the common case; a second miss is
        reported as before. Only a numeric block that names numbers is retried; the stored brief
        is never modified (the feedback rides on a copy for this one request).
        """
        recipe = self.ports._pipeline_recipe(body)
        try:
            return pipeline.create_outline(body['brief'], materials, recipe)
        except PipelineError as error:
            detail = getattr(error, 'diagnostics', None) or {}
            numbers = [n for n in detail.get('unsupported_numbers', []) if isinstance(n, str)][:12]
            if error.code != 'unsupported_numeric_claim' or not numbers:
                raise
            self.ports._check_cancel(job)
            page = detail.get('page')
            where = ('第%d页' % page) if isinstance(page, int) and not isinstance(page, bool) else '某一页'
            brief = dict(body['brief'])
            brief['requirements'] = (str(brief.get('requirements') or '') +
                '\n【系统校验反馈】上一版大纲的%s出现了没有来源依据的数字：%s。请不要使用这些数字；'
                '只能使用需求或材料里明确出现过的数字，其余内容用文字描述。' % (where, '、'.join(numbers))).strip()
            return pipeline.create_outline(brief, materials, recipe)

    def _outline_reserved(self, job, pipeline, body, charge):
        materials=[m for m in body['materials']if m['id']not in body.get('reading_exclusions',{})]
        output = self.ports._plan_outline(pipeline, body, materials, job)
        self.ports._check_cancel(job)
        specs = output.get('slide_specs') or output.get('outline') if isinstance(output, dict) else output
        self.ports._validate_specs(specs, body['brief']['page_count'], body['brief'])
        def op(current):
            for refreshed in output.get('_material_refreshes', []) if isinstance(output, dict) else []:
                material = next((item for item in current['materials'] if item['id'] == refreshed.get('id')), None)
                if not material or material.get('sha256') != refreshed.get('sha256'):
                    raise WorkflowAPIError('Refreshed material no longer matches its original source')
                material.update({key: deepcopy(refreshed[key]) for key in (
                    'text', 'chunks', 'warnings', 'processing_limits', 'source_structured_data',
                    'ocr_status', 'ocr_report', 'reader_schema_version') if key in refreshed})
                reference = db.session.get(ReferenceFile, material['id'])
                if reference:
                    reference.markdown_content = material.get('text', '')
            current['outline'] = output.get('outline', specs) if isinstance(output, dict) else specs
            current['slide_specs'] = self.ports._bind_source_assets(self.ports._normalize_specs(specs, trusted=True, brief=current['brief']), current)
            current['planning_report'] = {'material_coverage': self.ports.hook__material_coverage(output.get('material_coverage')),
                                          'model_report': self.ports.hook__model_report(output.get('model_report'))} if isinstance(output, dict) else {}
            charge.mark_accepted(db.session.get(WuyuWorkflowJob, job.id))
        with charge.accepting(pipeline=pipeline):
            self.ports._mutate(job.project_id, op, job=job)
        charge.settle(pipeline=pipeline)

    def _samples(self, job, pipeline):
        _, body = self.ports._read(job.project_id)
        if not body['slide_specs']:
            self.ports._outline(job, pipeline)
        _, body = self.ports._read(job.project_id)
        self.ports._progress(job, 'samples', body)
        if body['workflow']['template']['id'] is None:
            self.ports._event(job.project_id, 'template.selected', {'template_id': 'wuyu-reference-recipe'}, job)
        project_id = job.project_id
        recipe = self.ports._pipeline_recipe(body)
        feedback_text = str(json.loads(job.payload).get('feedback') or '')
        plans = [(number, role) for number, role in [(1, 'cover'), (2, 'first_body')]
                 if role not in body['workflow']['template']['samples'] or job.action == 'regenerate_samples']
        def prepare(item):
            number, role = item
            attempt = self.ports.hook__id()
            current = self.ports._event(project_id, 'sample.generate_started', {'role': role, 'attempt_id': attempt}, job)
            spec = deepcopy(current['slide_specs'][number - 1])
            spec['design_requirements'] = self.ports.hook__design_requirements(current['brief'])
            previous = current['workflow']['template']['samples'].get(role)
            feedback = feedback_text
            if previous:
                spec['previous_image_path'] = self.ports._artifact_file(current, previous['image_uri'])
            elif feedback:
                # A design reset has no current sample to edit. Retain the
                # instruction as a new-render requirement, not a fake edit.
                spec['design_requirements']['feedback'] = feedback
                feedback = ''
            return {'number': number, 'role': role, 'attempt': attempt, 'spec': spec, 'recipe': deepcopy(recipe),
                    'feedback': feedback, 'path': str(self.ports._path(project_id, 'samples', attempt + '.png')),
                    'template_revision': current['workflow']['template']['revision'],
                    'requirements_revision': current['workflow']['requirements']['revision']}
        def invoke(plan, worker, cancel):
            return worker.generate_slide(plan['spec'], plan['recipe'], plan['path'],
                feedback=plan['feedback'], cancel_check=cancel)
        def accept(plan, output):
            number, role = plan['number'], plan['role']
            uri = self.ports._record_artifact(project_id, output['image_path'], 'sample_image', job,
                {'number': number, 'role': role,
                 'sample_spec_sha256': hashlib.sha256(self.ports.hook__json(body['slide_specs'][number - 1]).encode()).hexdigest(),
                 'template_revision': plan['template_revision'], 'requirements_revision': plan['requirements_revision'],
                 **self.ports.hook__generation_report(output)})
            self.ports._event(project_id, 'sample.generated', {'role': role, 'attempt_id': plan['attempt'],
                'template_revision': plan['template_revision'],
                'requirements_revision': plan['requirements_revision'],
                'page_kind': 'cover' if number == 1 else 'content',
                'copy': output.get('copy') or plan['spec'].get('copy') or plan['spec'].get('title', '页面内容'), 'image_uri': uri}, job)
        def reject(plan, error):
            self.ports._event(project_id, 'sample.failed', {'role': plan['role'], 'attempt_id': plan['attempt'],
                'template_revision': plan['template_revision'], 'requirements_revision': plan['requirements_revision'],
                'error': 'Sample generation did not complete'}, job)
        self.ports._model_batch(job, plans, prepare, invoke, accept, reject, min(2, self.ports._model_workers(job, 'image')))

    def _generate_batch(self, job, numbers=None):
        project_id, epoch = job.project_id, job.input_epoch
        _, body = self.ports._read(project_id)
        self.ports._require_template_choice(body)
        self.ports._relayout_seed_unchanged(job)
        _, body = self.ports._read(project_id)
        recipe = self.ports._pipeline_recipe(body, approved_samples=body['brief'].get('creation_kind', 'deck') == 'deck'
                                       and body['workflow']['template'].get('confirmation_kind') not in {'library', 'upload'})
        if numbers is not None or page_retry.PAGE_RETRY_SCOPE_KEY in json.loads(job.payload):
            number = page_retry.validate_page_retry(job, body)
            if numbers is not None and numbers != [number]:
                raise PipelineError('page_retry_invalid', 'Page retry must dispatch exactly its target')
            plans = [number]
        else:
            plans = [page['number'] for page in body['workflow']['pages'] if page['generation_status'] != 'ready']
        def prepare(number):
            attempt = self.ports.hook__id()
            current = self.ports._event(project_id, 'page.generate_started', {'number': number, 'attempt_id': attempt}, job)
            page = current['workflow']['pages'][number - 1]
            spec = deepcopy(current['slide_specs'][number - 1])
            spec['design_requirements'] = self.ports.hook__design_requirements(current['brief'])
            spec.pop('previous_image_path', None)
            # A re-planned page is rendered fresh from its new spec; editing the
            # superseded image would carry its old content forward.
            feedback = str(page.get('requested_change') or '')
            if (page.get('artifact') or {}).get('image_uri') and not page.get('fresh_render'):
                spec['previous_image_path'] = self.ports._artifact_file(current, page['artifact']['image_uri'])
            elif page.get('fresh_render') and feedback:
                # No original image is edited, so the pipeline must not be told
                # this is an edit (feedback != '' requires the original). The
                # instruction still steers the render as a design requirement.
                spec['design_requirements'] = dict(spec['design_requirements'], feedback=feedback)
                feedback = ''
            return {'number': number, 'attempt': attempt, 'spec': spec, 'recipe': deepcopy(recipe),
                    'feedback': feedback,
                    'path': str(self.ports._path(project_id, 'pages', attempt + '.png'))}
        def invoke(plan, worker, cancel):
            return worker.generate_slide(plan['spec'], plan['recipe'], plan['path'],
                feedback=plan['feedback'], cancel_check=cancel)
        def accept(plan, output):
            number, spec = plan['number'], plan['spec']
            uri = self.ports._record_artifact(project_id, output['image_path'], 'slide_image', job,
                                        {'number': number, **self.ports.hook__generation_report(output)})
            self.ports._event(project_id, 'page.generated', {'number': number, 'attempt_id': plan['attempt'],
                'copy': output.get('copy') or spec.get('copy') or spec.get('title', '页面内容'), 'image_uri': uri}, job)
            self.ports._sync_page(project_id, number, spec, output['image_path'], epoch=epoch, job=job)
            _, current = self.ports._read(project_id)
            self.ports._progress(job, 'page_generation', current)
        def reject(plan, error):
            self.ports._event(project_id, 'page.failed', {'number': plan['number'], 'attempt_id': plan['attempt'],
                'error': 'Page generation did not complete'}, job)
        self.ports._model_batch(job, plans, prepare, invoke, accept, reject, self.ports._model_workers(job, 'image'))

    def _generate_next(self, job, pipeline):
        self.ports._check_cancel(job)
        _, body = self.ports._read(job.project_id)
        self.ports._require_template_choice(body)
        state = body['workflow']
        current = next((p for p in state['pages'] if p['generation_status'] != 'ready'
                        or state['mode'] == 'guided' and p['confirmed_image_version'] != p['image_version']), None)
        if not current:
            return False
        if current['generation_status'] == 'ready':
            raise WorkflowAPIError('Confirm the current page before generating another')
        number, attempt = current['number'], self.ports.hook__id()
        self.ports._event(job.project_id, 'page.generate_started', {'number': number, 'attempt_id': attempt}, job)
        spec = deepcopy(body['slide_specs'][number - 1])
        spec['design_requirements'] = self.ports.hook__design_requirements(body['brief'])
        # Only the server resolves previous images. Client-supplied filesystem
        # paths never enter provider requests through the SlideSpec contract.
        spec.pop('previous_image_path', None)
        feedback = str(current.get('requested_change') or '')
        if (current.get('artifact') or {}).get('image_uri') and not current.get('fresh_render'):
            spec['previous_image_path'] = self.ports._artifact_file(body, current['artifact']['image_uri'])
        elif current.get('fresh_render') and feedback:
            spec['design_requirements'] = dict(spec['design_requirements'], feedback=feedback)
            feedback = ''
        charge = self.ports.hook_WorkflowCharge(self.ports.app(), job, 'image')
        try:
            charge.reserve()
            output = pipeline.generate_slide(spec, self.ports._pipeline_recipe(body, approved_samples=body['workflow']['template'].get('confirmation_kind') not in {'library', 'upload'}), str(self.ports._path(job.project_id, 'pages', attempt + '.png')),
                feedback=feedback, cancel_check=lambda: self.ports._check_cancel(job))
            self.ports._check_cancel(job)
            uri = self.ports._record_artifact(job.project_id, output['image_path'], 'slide_image', job,
                                        {'number': number, **self.ports.hook__generation_report(output)})
            with charge.accepting(output=output):
                self.ports._event(job.project_id, 'page.generated', {'number': number, 'attempt_id': attempt,
                    'copy': output.get('copy') or spec.get('copy') or spec.get('title', '页面内容'), 'image_uri': uri}, job)
            self.ports._sync_page(job.project_id, number, spec, output['image_path'], epoch=job.input_epoch, job=job)
            charge.settle()
        except Exception:
            charge.refund()
            try:
                self.ports._event(job.project_id, 'page.failed', {'number': number, 'attempt_id': attempt, 'error': 'Page generation did not complete'}, job)
            except (CancelledWork, WorkflowError):
                pass
            raise
        return True

    def _sync_page(self, project_id, number, spec, image_path, epoch, job=None):
        def op(body):
            if body['epoch'] != epoch:
                raise CancelledWork()
            current = body['workflow']['pages'][number - 1]
            if not current['artifact'] or Path(self.ports._artifact_file(body, current['artifact']['image_uri'])).resolve() != Path(image_path).resolve():
                raise CancelledWork()
            page = Page.query.filter_by(project_id=project_id, order_index=number - 1).first()
            if not page:
                page = Page(project_id=project_id, order_index=number - 1)
                db.session.add(page)
            public_spec = self.ports.hook__public_source({k: v for k, v in spec.items() if k not in {'previous_image_path', 'design_requirements'}})
            page.set_outline_content(public_spec)
            page.set_description_content({'text_content': spec.get('copy') or spec.get('points', [])})
            page.generated_image_path, page.cached_image_path, page.status = str(image_path), None, 'COMPLETED'
        self.ports._mutate(project_id, op, job=job)

    def _generate_focused(self, job, pipeline):
        _, body = self.ports._read(job.project_id)
        if body['brief'].get('creation_kind', 'deck') not in {'pages', 'asset'}:
            raise WorkflowAPIError('Direct generation is only available for focused pages or assets')
        if json.loads(job.payload).get('generation_requested') is not True:
            raise WorkflowAPIError('Explicit focused generation authorization is required')
        if not body['slide_specs']:
            self.ports._outline(job, pipeline)
            _, body = self.ports._read(job.project_id)
        if body['workflow']['template']['id'] is None:
            body = self.ports._event(job.project_id, 'template.selected', {'template_id': 'wuyu-reference-recipe'}, job)
        if self.ports.hook_phase(body['workflow']) == 'generation_request':
            self.ports._event(job.project_id, 'focused.requested', {'generation_requested': True}, job)
        self.ports._continue(job, pipeline)

    def _deliver_focused_images(self, job, pipeline, body):
        signature = self.ports._image_signature(body)
        artifact = next((a for a in reversed(body['artifacts']) if a['kind'] == 'image_pptx'
                         and a.get('metadata', {}).get('image_signature') == signature
                         and Path(self.ports.app().config['UPLOAD_FOLDER'], a['relative_path']).is_file()), None)
        if artifact is None:
            uri = self.ports._export_additional(job, pipeline, 'pptx')
        else:
            uri = 'artifact:' + artifact['id']
        body = self.ports._event(job.project_id, 'focused.image_exported', {'artifact_uri': uri}, job)
        self.ports._mutate(job.project_id, lambda current: setattr(db.session.get(Project, job.project_id), 'status', 'COMPLETED'), job=job)
        self.ports._progress(job, 'exported', body)

    def _continue(self, job, pipeline):
        while True:
            self.ports._check_cancel(job)
            _, body = self.ports._read(job.project_id)
            stage = self.ports.hook_phase(body['workflow'])
            self.ports._progress(job, stage, body)
            if stage == 'page_generation':
                if body['workflow']['review_policy'] == 'guided' and job.action != 'generate_next':
                    return
                if body['workflow']['review_policy'] == 'guided':
                    self.ports._generate_next(job, pipeline)
                    return
                self.ports._generate_batch(job)
            elif stage in {'tracing', 'quality_check', 'ready_to_export', 'exporting'}:
                # Generation/review delivers images first. Editable conversion is a
                # separate export/start_tracing request from the delivery panel.
                # Preserve recovery of a native batch already started explicitly.
                if (body['workflow'].get('output_preference') == 'image'
                        or stage == 'tracing' and not body['workflow'].get('trace_batch')):
                    if body['workflow'].get('output_preference') != 'image':
                        def defer_delivery(current):
                            current['workflow'] = self.ports.hook_apply_event(current['workflow'], {'event_id': self.ports.hook__id(),
                                'type': 'output_preference.changed', 'payload': {'output_preference': 'image'}})
                            current['brief']['output_preference'] = 'image'
                        _, body = self.ports._mutate(job.project_id, defer_delivery, job=job)
                    self.ports._deliver_focused_images(job, pipeline, body)
                    return
                self.ports._trace_and_export(job, pipeline)
                return
            else:
                return
