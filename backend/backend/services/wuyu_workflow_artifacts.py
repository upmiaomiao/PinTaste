"""ArtifactDelivery: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import mimetypes
import math
import os
from pathlib import Path
import time
from models import db, Project, Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_state import WorkflowError
from services.wuyu_errors import PipelineError
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from .wuyu_resource_limits import export_slot
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class ArtifactDeliveryPorts:
    EXPORT_FORMATS: Callable[..., Any]
    _artifact_file: Callable[..., Any]
    _check_cancel: Callable[..., Any]
    _current_hybrid_export: Callable[..., Any]
    _ensure_image_export: Callable[..., Any]
    _event: Callable[..., Any]
    _export_additional: Callable[..., Any]
    _export_provider: Callable[..., Any]
    _hybrid_signature: Callable[..., Any]
    _image_signature: Callable[..., Any]
    _model_batch: Callable[..., Any]
    _model_workers: Callable[..., Any]
    _mutate: Callable[..., Any]
    _path: Callable[..., Any]
    _progress: Callable[..., Any]
    _public_artifact: Callable[..., Any]
    _public_recipe: Callable[..., Any]
    _read: Callable[..., Any]
    _record_artifact: Callable[..., Any]
    _record_export: Callable[..., Any]
    _serve_hybrid_requests: Callable[..., Any]
    _try_hybrid_delivery: Callable[..., Any]
    _versions: Callable[..., Any]
    app: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook__model_report: Callable[..., Any]
    hook__now: Callable[..., Any]
    hook__quality_report: Callable[..., Any]
    hook__safe_failure_code: Callable[..., Any]
    hook_image_export_ready: Callable[..., Any]
    hook_phase: Callable[..., Any]
    stop_event: Callable[..., Any]



class ArtifactDelivery:
    def __init__(self, ports: ArtifactDeliveryPorts):
        self.ports = ports

    def _path(self, project_id, section, name):
        root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
        path = root / project_id / 'wuyu' / section / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _record_artifact(self, project_id, path, kind, job, metadata=None):
        path = Path(path).resolve()
        root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
        if not path.is_file() or not path.is_relative_to(root / project_id):
            raise WorkflowAPIError('A provider returned an invalid artifact path')
        item = {'id': self.ports.hook__id(), 'kind': kind, 'relative_path': path.relative_to(root).as_posix(),
                'filename': path.name, 'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'mime_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                'metadata': metadata or {}, 'created_at': self.ports.hook__now()}
        self.ports._mutate(project_id, lambda body: body['artifacts'].append(item), job=job)
        return 'artifact:' + item['id']

    def artifact_path(self, project_id, artifact_id):
        _, body = self.ports._read(project_id)
        item = next((a for a in body['artifacts'] if a['id'] == artifact_id), None)
        if not item:
            raise WorkflowAPIError('Artifact not found', 'ARTIFACT_NOT_FOUND', 404)
        root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
        path = (root / item['relative_path']).resolve()
        if not path.is_relative_to(root / project_id):
            raise WorkflowAPIError('Artifact not found', 'ARTIFACT_NOT_FOUND', 404)
        if not path.is_file():
            # The existing controller checks project ownership first. Only
            # verified, explicitly allowlisted export copies can be restored.
            from services.wuyu_cos_artifacts import ArtifactStore, StorageError
            try:
                store = ArtifactStore.from_app(self.ports.app())
                if store is None or not store.eligible(project_id, item):
                    raise WorkflowAPIError('Artifact not found', 'ARTIFACT_NOT_FOUND', 404)
                path = store.restore(project_id, item)
            except (StorageError, OSError):
                raise WorkflowAPIError('文件副本暂时无法取回，请稍后重试；原记录保留。',
                                       'ARTIFACT_STORAGE_UNAVAILABLE', 503) from None
        # This path is reached only after the existing controller authorization.
        # Bookkeeping is local-only; it never starts a COS upload consumer.
        from services.wuyu_cos_artifacts import ArtifactStore, StorageError, KINDS
        try:
            store = ArtifactStore.from_app(self.ports.app())
        except (StorageError, OSError):
            # Cloud configuration must not break an existing hot-file download.
            # Missing access state is never evidence that a file can be evicted.
            store = None
        try:
            if (store is not None and store.eligible(project_id, item)
                    and item.get('kind') in KINDS and type(item.get('bytes')) is int
                    and 1 <= item['bytes'] <= store.settings.max_bytes):
                store.lifecycle().accessed(project_id, item)
        except StorageError as error:
            # A background copy holds this lock during network I/O. Preserve
            # hot-file availability; incomplete access counts are not coldness.
            if str(error) != 'COS_ASSET_BUSY' or not path.is_file():
                raise WorkflowAPIError('文件状态暂时不可用，请稍后重试；原文件保留。',
                                       'ARTIFACT_STORAGE_UNAVAILABLE', 503) from None
        except OSError:
            raise WorkflowAPIError('文件状态暂时不可用，请稍后重试；原文件保留。',
                                   'ARTIFACT_STORAGE_UNAVAILABLE', 503) from None
        return path, item['mime_type']

    def _artifact_file(self, body, uri):
        ident = uri.removeprefix('artifact:')
        item = next((a for a in body['artifacts'] if a['id'] == ident), None)
        if not item:
            raise WorkflowAPIError('Source image artifact is missing')
        return str(Path(self.ports.app().config['UPLOAD_FOLDER']).resolve() / item['relative_path'])

    def _trace_and_export(self, job, pipeline, rebuild=False):
        self.ports._check_cancel(job)
        _, body = self.ports._read(job.project_id)
        if rebuild and self.ports.hook_phase(body['workflow']) not in {'exported', 'quality_check', 'ready_to_export', 'exporting'}:
            # A rebuild and its retry may only re-export existing manifests.
            # Never let changed/stale inputs fall through to paid tracing.
            raise WorkflowAPIError('Current traced images are required for a native rebuild')
        if self.ports.hook_phase(body['workflow']) == 'tracing':
            self.ports._ensure_image_export(job, pipeline, body)
            project_id = job.project_id
            batch = self.ports.hook__id()
            body = self.ports._event(project_id, 'trace.started', {'batch_id': batch}, job)
            trace_failures = {}
            started = time.monotonic()
            delivery_after = max(0, float(getattr(job, '_runtime_config', self.ports.app().config).get('WUYU_HYBRID_EXPORT_AFTER_SECONDS', 120)))
            delivery_attempted = self.ports._current_hybrid_export(project_id, body) is not None
            last_delivery_signature = self.ports._hybrid_signature(body) if delivery_attempted else None
            refresh_interval = getattr(job, '_runtime_config', self.ports.app().config).get('WUYU_HYBRID_REFRESH_SECONDS', 2)
            try:refresh_interval = max(0, min(120, float(refresh_interval)))
            except (ValueError, TypeError):refresh_interval = 2
            if not math.isfinite(refresh_interval):refresh_interval = 2
            next_delivery_at = 0.
            try:
                numbers = list(body['workflow']['trace_batch']['pending_pages'])
                plans = []
                for number in numbers:
                    self.ports._check_cancel(job)
                    _, current = self.ports._read(job.project_id)
                    page = current['workflow']['pages'][number - 1]
                    source = self.ports._artifact_file(current, page['artifact']['image_uri'])
                    approved_spec = dict(current['slide_specs'][number - 1], approved=True)
                    logo_assets = [{'id': material['id'], 'role': 'logo',
                                    'path': str(Path(self.ports.app().config['UPLOAD_FOLDER']).resolve() / material['relative_path'])}
                                   for material in current['materials'] if material.get('role') == 'logo' and material.get('relative_path')]
                    image_sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()
                    asset_versions = [{'id': asset['id'], 'sha256': hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()}
                                      for asset in logo_assets]
                    spec_sha = hashlib.sha256(self.ports.hook__json({'spec': approved_spec, 'assets': asset_versions}).encode('utf-8')).hexdigest()
                    cache_key = hashlib.sha256((image_sha + ':' + spec_sha).encode('ascii')).hexdigest()
                    plans.append({'number': number, 'source': source, 'spec': approved_spec, 'assets': logo_assets,
                        'path': str(self.ports._path(project_id, 'traces', f'page-{number:03d}/' + cache_key))})

                # Freeze scalar authorization on the owner thread; model IO
                # threads must never dereference expired ORM job attributes.
                trace_retry = {'trace_retry_id': job.id} if job.retry_of else {}
                def invoke(plan, worker, cancel):
                    return worker.trace_slide(plan['source'], plan['spec'], plan['path'], assets=plan['assets'], cancel_check=cancel, **trace_retry)
                def accept(plan, output):
                    number = plan['number']
                    if not isinstance(output.get('qa'), dict) or output['qa'].get('passed') is not True:
                        raise WorkflowAPIError('Slide tracing did not pass quality checks')
                    path = output.get('path') or output.get('manifest_path')
                    if not path:
                        path = self.ports._path(project_id, 'traces', batch + '/' + str(number) + '.json')
                        Path(path).write_text(self.ports.hook__json(output.get('editable_manifest', output)), encoding='utf-8')
                    manifest = output.get('editable_manifest', {})
                    qa = self.ports.hook__quality_report(output.get('qa'))
                    uri = self.ports._record_artifact(project_id, path, 'editable_slide', job,
                        {'number': number, 'qa': qa, 'model_report': self.ports.hook__model_report(output.get('model_report') or manifest.get('model_report')),
                         'reused_raw_response': qa.get('reused_raw_response', bool(manifest.get('reused_raw_response')))})
                    objects = (manifest.get('objects') or manifest.get('elements')) if isinstance(manifest, dict) else manifest
                    if not isinstance(objects, list) or not objects:
                        raise WorkflowAPIError('Tracing must return real editable objects')
                    self.ports._event(project_id, 'trace.page_completed', {'batch_id': batch, 'page': {
                        'number': number, 'artifact_uri': uri, 'editable_objects': objects}}, job)
                    _, current = self.ports._read(project_id)
                    self.ports._progress(job, 'tracing', current)
                def reject(plan, error):
                    trace_failures[plan['number']] = self.ports.hook__safe_failure_code(error.code) if isinstance(error, PipelineError) else 'PROVIDER_OPERATION_FAILED'
                def checkpoint(cancelled=False):
                    nonlocal delivery_attempted, last_delivery_signature, next_delivery_at
                    requested_delivery = self.ports._serve_hybrid_requests(job, pipeline, draining=cancelled)
                    if cancelled:
                        return
                    delivery_attempted = delivery_attempted or requested_delivery or bool(trace_failures) or time.monotonic() - started >= delivery_after
                    if delivery_attempted:
                        _, delivery_body = self.ports._read(project_id)
                        # All native pages are already available: the strict
                        # export immediately below owns the one final build.
                        if not delivery_body['workflow'].get('trace_batch', {}).get('pending_pages'):
                            return
                        signature = self.ports._hybrid_signature(delivery_body)
                        if signature != last_delivery_signature and time.monotonic() >= next_delivery_at:
                            last_delivery_signature = signature
                            self.ports._try_hybrid_delivery(job, pipeline, trace_failures)
                            next_delivery_at = time.monotonic() + refresh_interval
                self.ports._model_batch(job, plans, lambda plan: plan, invoke, accept,
                    reject, self.ports._model_workers(job, 'trace'), continue_on_error=True, checkpoint=checkpoint, quota_kind='trace', resource_stage='trace')
                self.ports._event(project_id, 'trace.completed', {'batch_id': batch, 'pages': []}, job)
            except Exception as error:
                try:
                    self.ports._event(job.project_id, 'trace.failed', {'batch_id': batch, 'error': 'Tracing did not complete'}, job)
                except (CancelledWork, WorkflowError):
                    pass
                if not isinstance(error, CancelledWork):
                    self.ports._try_hybrid_delivery(job, pipeline, trace_failures)
                raise
        _, body = self.ports._read(job.project_id)
        stage = self.ports.hook_phase(body['workflow'])
        if stage in {'quality_check', 'ready_to_export'} or rebuild and stage in {'exported', 'exporting'}:
            # Actual visual/native-object validation is owned by export_deck.
            # Do not manufacture a passing QA from a generation callback.
            run = self.ports.hook__id()
            self.ports._event(job.project_id, 'qa.started', {'run_id': run}, job)
            try:
                output = self.ports._export_provider(job, pipeline, body)
            except CancelledWork:
                raise
            except Exception:
                self.ports._try_hybrid_delivery(job, pipeline)
                raise
            self.ports._check_cancel(job)
            qa = output.get('qa')
            if not isinstance(qa, dict) or not isinstance(qa.get('passed'), bool) or not qa.get('checks'):
                self.ports._try_hybrid_delivery(job, pipeline)
                raise WorkflowAPIError('Exporter must return a real quality report')
            versions = self.ports._versions(body)
            self.ports._event(job.project_id, 'qa.completed', {'run_id': run, **versions, **qa}, job)
            if not qa['passed']:
                self.ports._try_hybrid_delivery(job, pipeline)
                raise WorkflowAPIError('Quality checks did not pass')
            # Manual (cloud-editor) edits survive a new export: only slides whose
            # image changed since the manual save are taken from this export.
            from .wuyu_office import merge_manual_edits
            merged_path, merge_report = merge_manual_edits(
                body, lambda artifact: self.ports._artifact_file(body, 'artifact:' + artifact['id']), output['path'],
                versions['image_versions'], str(self.ports._path(job.project_id, 'exports', self.ports.hook__id() + '.merged.pptx')))
            if merge_report is not None and not merge_report.get('superseded'):
                output = dict(output, path=merged_path)
            export_run = self.ports.hook__id()
            self.ports._event(job.project_id, 'export.started', {'run_id': export_run}, job)
            uri = self.ports._record_export(job, output, 'editable_pptx')
            self.ports._event(job.project_id, 'export.completed', {'run_id': export_run, **versions, 'pptx_uri': uri + '.pptx'}, job)
            if merge_report is not None:
                def carry_manual_edit(current):
                    manual = current.get('manual_edit') or {}
                    current.setdefault('manual_edit_history', []).append(dict(manual, merge=merge_report))
                    current['manual_edit_history'] = current['manual_edit_history'][-50:]
                    if merge_report.get('superseded'):
                        current['manual_edit'] = None
                    else:
                        current['manual_edit'] = dict(manual, artifact_id=uri.removeprefix('artifact:'), export_run_id=export_run, base='export',
                            image_versions=versions['image_versions'], merged_at=self.ports.hook__now(),
                            edited_pages=merge_report['kept_manual_pages'] and sorted(set(manual.get('edited_pages', [])) & set(merge_report['kept_manual_pages'])))
                self.ports._mutate(job.project_id, carry_manual_edit, job=job)
            def mark_project(current):
                db.session.get(Project, job.project_id).status = 'COMPLETED'
            self.ports._mutate(job.project_id, mark_project, job=job)
            memory = self.ports.app().extensions.get('wuyu_design_memory')
            if memory:
                memory.mark_supported(job.project_id)

    def _versions(self, body):
        return {'image_versions': {str(p['number']): p['image_version'] for p in body['workflow']['pages']},
                'trace_versions': {str(p['number']): p['trace_version'] for p in body['workflow']['pages']}}

    def _image_signature(self, body):
        state = body['workflow']
        return hashlib.sha256(self.ports.hook__json({'template_revision': state['template']['revision'],
            'requirements_revision': state['requirements']['revision'],
            'pages': [{'number': page['number'], 'image_version': page['image_version'],
                       'image_uri': (page.get('artifact') or {}).get('image_uri')}
                      for page in state['pages']]}).encode()).hexdigest()

    def _hybrid_signature(self, body):
        return hashlib.sha256(self.ports.hook__json({'image_signature': self.ports._image_signature(body),
            'pages': [{'number': page['number'], 'trace_version': page['trace_version'],
                       'trace': page.get('trace')} for page in body['workflow']['pages']],
            'specs': body.get('slide_specs', [])}).encode('utf-8')).hexdigest()

    def _current_hybrid_export(self, project_id, body):
        if not self.ports.hook_image_export_ready(body['workflow']):
            return None
        signature = self.ports._hybrid_signature(body)
        for artifact in reversed(body['artifacts']):
            if (artifact['kind'] == 'hybrid_pptx' and artifact.get('metadata', {}).get('hybrid_signature') == signature
                    and Path(self.ports.app().config['UPLOAD_FOLDER'], artifact['relative_path']).is_file()):
                return self.ports._public_artifact(project_id, artifact)
        return None

    def _try_hybrid_delivery(self, job, pipeline, trace_failures=None):
        """Optional delivery cannot erase a failed trace or its successful siblings."""
        self.ports._check_cancel(job)
        _, body = self.ports._read(job.project_id)
        if not self.ports.hook_image_export_ready(body['workflow']) or self.ports._current_hybrid_export(job.project_id, body):
            return
        try:
            self.ports._export_additional(job, pipeline, 'hybrid_pptx', trace_failures=trace_failures)
        except CancelledWork:
            raise
        except Exception:
            self.ports.app().logger.warning('Hybrid artifact delivery unavailable: project=%s', job.project_id)

    def _serve_hybrid_requests(self, owner, pipeline, draining=False):
        """One ORM owner services local-only exports while its model IO runs.

        The project lease remains with the tracing owner. A separate scheduler
        cannot claim the queued request, and no model thread touches a session.
        """
        if self.ports.stop_event().is_set():
            return False
        if not draining:
            self.ports._check_cancel(owner)
        _, current = self.ports._read(owner.project_id)
        requests = WuyuWorkflowJob.query.filter_by(project_id=owner.project_id, input_epoch=current['epoch'],
                                                   action='export', status='queued').order_by(WuyuWorkflowJob.created_at).all()
        delivered = False
        for request_job in requests:
            if json.loads(request_job.payload).get('format') != 'hybrid_pptx' or request_job.cancel_requested:
                continue
            request_job.status, request_job.worker_pid = 'running', os.getpid()
            request_job.started_at = request_job.heartbeat_at = datetime.utcnow()
            task = db.session.get(Task, request_job.task_id)
            if task:
                task.status = 'PROCESSING'
            db.session.commit()
            status, code = 'completed', None
            try:
                self.ports._export_additional(request_job, pipeline, 'hybrid_pptx')
            except CancelledWork:
                status, code = 'cancelled', 'CANCELLED_OR_INPUT_CHANGED'
            except Exception as error:
                status = 'failed'
                code = self.ports.hook__safe_failure_code(error.code) if isinstance(error, PipelineError) else 'PROVIDER_OPERATION_FAILED'
            request_job = db.session.get(WuyuWorkflowJob, request_job.id)
            request_job.status, request_job.error_code, request_job.completed_at = status, code, datetime.utcnow()
            request_job.progress = self.ports.hook__json({'stage': 'export', 'format': 'hybrid_pptx', 'provider_called': False})
            task = db.session.get(Task, request_job.task_id)
            if task:
                task.status, task.error_message, task.completed_at = 'COMPLETED' if status == 'completed' else 'FAILED', code, datetime.utcnow()
            db.session.commit()
            delivered = delivered or status == 'completed'
        return delivered

    def _ensure_image_export(self, job, pipeline, body):
        signature = self.ports._image_signature(body)
        if any(artifact['kind'] == 'image_pptx' and (artifact.get('metadata') or {}).get('image_signature') == signature
               and Path(self.ports.app().config['UPLOAD_FOLDER'], artifact['relative_path']).is_file() for artifact in body['artifacts']):
            return
        try:
            self.ports._export_additional(job, pipeline, 'pptx')
        except CancelledWork:
            raise
        except Exception:
            # This convenient image delivery must not disable the separately
            # requested native conversion. A manual export remains available.
            self.ports.app().logger.warning('Image-only automatic export unavailable: project=%s', job.project_id)

    def _export_provider(self, job, pipeline, body, format='editable_pptx', trace_failures=None):
        started = time.monotonic()
        job_id = job.id
        pages = []
        for page, spec in zip(body['workflow']['pages'], body['slide_specs']):
            evidence_ids = spec.get('evidence_ids') or []
            evidence = [{'id': chunk['id'], 'text': chunk.get('text', ''), 'filename': material['filename'],
                         'source': {k: v for k, v in chunk.get('source', {}).items()
                                    if k in {'filename', 'page', 'sheet', 'row', 'column', 'title'}}
                         if isinstance(chunk.get('source'), dict) else {'filename': material['filename']}}
                        for material in body['materials'] for chunk in material.get('chunks', [])
                        if chunk.get('id') in evidence_ids]
            public_spec = dict(spec, evidence=evidence, reference_recipe=self.ports._public_recipe(body['reference_recipe']))
            if format in {'sources', 'images_zip'}:
                public_spec['planning_report'] = body.get('planning_report', {})
            # The trusted local exporter needs controlled source paths to verify
            # original bytes. It removes them at the JSON/package output boundary.
            item = {'number': page['number'], 'spec': public_spec,
                    'image_path': self.ports._artifact_file(body, page['artifact']['image_uri'])}
            if format == 'hybrid_pptx':
                artifact = next(a for a in body['artifacts'] if 'artifact:' + a['id'] == page['artifact']['image_uri'])
                if hashlib.sha256(Path(item['image_path']).read_bytes()).hexdigest() != artifact['sha256']:
                    raise PipelineError('approved_image_changed', '已确认页图文件发生变化，不能作为保底图片。')
                item['trace_status_reason'] = (trace_failures or {}).get(page['number'], 'trace_pending' if body['workflow'].get('trace_batch') else 'trace_unavailable')
            if page['trace'] and page['trace'].get('source_image_version') == page['image_version']:
                if format == 'hybrid_pptx':
                    try:
                        trace_path = self.ports._artifact_file(body, page['trace']['artifact_uri'])
                        trace_artifact = next(a for a in body['artifacts'] if 'artifact:' + a['id'] == page['trace']['artifact_uri'])
                        if not Path(trace_path).is_file():
                            item['trace_status_reason'] = 'trace_artifact_missing'
                        elif hashlib.sha256(Path(trace_path).read_bytes()).hexdigest() != trace_artifact['sha256']:
                            item['trace_status_reason'] = 'trace_artifact_changed'
                        else:
                            item['manifest_path'] = trace_path
                    except (WorkflowAPIError, StopIteration, OSError):
                        item['trace_status_reason'] = 'trace_artifact_missing'
                else:
                    item['manifest_path'] = self.ports._artifact_file(body, page['trace']['artifact_uri'])
            pages.append(item)
        output = None
        try:
            with export_slot(self.ports.app(), lambda: self.ports._check_cancel(job)):
                output = pipeline.export_deck(pages, format, str(self.ports._path(job.project_id, 'exports',
                    self.ports.hook__id() + self.ports.EXPORT_FORMATS()[format])), cancel_check=lambda: self.ports._check_cancel(job))
            return output
        finally:
            # Local export cost is separate from paid model latency. Preserve
            # the existing result/error if optional diagnostics cannot persist.
            try:
                current = db.session.get(WuyuWorkflowJob, job_id)
                progress = json.loads(current.progress or '{}')
                entry = {'format': format, 'total_seconds': round(time.monotonic()-started, 6),
                    'page_count': len(pages), 'succeeded': output is not None}
                if isinstance(output, dict):
                    entry.update(self.ports.hook__quality_report(output.get('qa', {})).get('export_performance', {}))
                progress['export_timings'] = (progress.get('export_timings', []) + [entry])[-80:]
                current.progress = self.ports.hook__json(progress)
                db.session.commit()
            except Exception:
                db.session.rollback()
                self.ports.app().logger.warning('Local export timing unavailable')

    def _export_additional(self, job, pipeline, format, trace_failures=None):
        self.ports._check_cancel(job)
        _, body = self.ports._read(job.project_id)
        if self.ports.hook_phase(body['workflow']) not in {'tracing', 'quality_check', 'ready_to_export', 'exported'}:
            raise WorkflowAPIError('Review the current images before exporting')
        output = self.ports._export_provider(job, pipeline, body, format, trace_failures)
        self.ports._check_cancel(job)
        qa = output.get('qa')
        if not isinstance(qa, dict) or qa.get('passed') is not True or not qa.get('checks'):
            raise WorkflowAPIError('Export did not pass quality checks')
        return self.ports._record_export(job, output, format)

    def _record_export(self, job, output, format):
        from .wuyu_delivery_names import report_title
        qa = output.get('qa') or {}
        quality = self.ports.hook__quality_report(qa)
        preview_ids = []
        for number, path in enumerate(output.get('preview_paths') or [], 1):
            uri = self.ports._record_artifact(job.project_id, path, 'export_preview', job, {'number': number, 'format': format})
            preview_ids.append(uri.removeprefix('artifact:'))
        report = self.ports._path(job.project_id, 'exports', self.ports.hook__id() + '.qa.json')
        report.write_text(self.ports.hook__json({'format': format, 'qa': quality,
                                'editability': output.get('editability'), 'preview_artifact_ids': preview_ids}), encoding='utf-8')
        report_uri = self.ports._record_artifact(job.project_id, report, 'quality_report', job, {'format': format})
        _, current = self.ports._read(job.project_id)
        return self.ports._record_artifact(job.project_id, output['path'], 'pptx' if format == 'editable_pptx' else
                                     'image_pptx' if format == 'pptx' else format, job,
            {'format': format, 'report_title':report_title(current), 'editability': output.get('editability'), 'quality': quality,
              **({'hybrid_signature': self.ports._hybrid_signature(current),
                  'editable_pages': output.get('editable_pages', []), 'image_only_pages': output.get('image_only_pages', []),
                  'fallback_reasons': output.get('fallback_reasons', {})} if format == 'hybrid_pptx' else {}),
             'image_signature': self.ports._image_signature(current), **self.ports._versions(current),
             'quality_report_artifact_id': report_uri.removeprefix('artifact:'), 'preview_artifact_ids': preview_ids})
