"""MaterialProcessing: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
from pathlib import Path
import uuid
from sqlalchemy import update
from models import db, ReferenceFile
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from .wuyu_workflow_contracts import CancelledWork, WorkflowAPIError
from .wuyu_reading_workflow import template_reading_guard
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class MaterialProcessingPorts:
    EXTENSIONS: Callable[..., Any]
    _cancel_old_jobs: Callable[..., Any]
    _check_cancel: Callable[..., Any]
    _enqueue_response: Callable[..., Any]
    _ensure: Callable[..., Any]
    _material_capacity_guard: Callable[..., Any]
    _material_management_guard: Callable[..., Any]
    _material_parser_capacity: Callable[..., Any]
    _mutate: Callable[..., Any]
    _new_state: Callable[..., Any]
    _path: Callable[..., Any]
    _public_asset: Callable[..., Any]
    _read: Callable[..., Any]
    _reading_invalidate: Callable[..., Any]
    _record_artifact: Callable[..., Any]
    app: Callable[..., Any]
    hook__failure_message: Callable[..., Any]
    hook__id: Callable[..., Any]
    hook__now: Callable[..., Any]
    hook__public_source: Callable[..., Any]
    hook__report_text: Callable[..., Any]
    hook_get_quota_service: Callable[..., Any]
    hook_material_capacity: Callable[..., Any]
    hook_material_error_code: Callable[..., Any]
    hook_material_parse_error_code: Callable[..., Any]
    hook_material_usage: Callable[..., Any]
    material_limits: Callable[..., Any]
    snapshot: Callable[..., Any]



class MaterialProcessing:
    def __init__(self, ports: MaterialProcessingPorts):
        self.ports = ports

    def _public_material(self, project_id, item):
        public = self.ports.hook__public_source({k: v for k, v in item.items() if k not in {
            'relative_path', 'template_images', 'text', 'chunks', 'source_assets'}})
        public.pop('error_message', None)
        if item.get('status') == 'failed':
            public['error_code'] = self.ports.hook_material_error_code(item.get('error_code'))
            public['error_message'] = self.ports.hook__failure_message(public['error_code'])
        public.update(text_chars=len(item.get('text', '')), chunk_count=len(item.get('chunks', [])),
                      text_excerpt=item.get('text', '')[:1200], full_text_available=bool(item.get('chunks')))
        public['source_assets'] = [self.ports._public_asset(project_id, asset) for asset in item.get('source_assets', [])]
        # A presentation whose pages are screenshots has no readable text. Say so, in plain
        # words, so the user is asked before the system tries to read it (and spends points).
        from services.wuyu_deck_intake import describe_presentation, visual_reading_notice
        deck = describe_presentation(item)
        if item.get('relayout_source'):
            public['reading_notice'] = '已按原页转图并提取原生文字，原图作为重新排版参考；请核对图片文字与图表，尚未调用阅读模型。'
        elif deck and deck['needs_visual_reading']:
            public['presentation'] = deck
            public['reading_notice'] = visual_reading_notice(deck)
        if item.get('artifact_id'):
            public['url'] = f"/api/projects/{project_id}/workflow/artifacts/{item['artifact_id']}"
            public['download_url'] = public['url'] + '?download=1'
        public['template_images'] = [{k: v for k, v in image.items() if k != 'relative_path'} |
            {'image_url': f"/api/projects/{project_id}/workflow/artifacts/{image['artifact_id']}"}
            for image in item.get('template_images', [])]
        return public

    def _public_asset(self, project_id, asset):
        public = self.ports.hook__public_source(asset)
        if asset.get('artifact_id'):
            public['image_url'] = f"/api/projects/{project_id}/workflow/artifacts/{asset['artifact_id']}"
        return public

    def material_limits(self):
        return {'max_file_bytes': min(int(self.ports.app().config.get('WUYU_MAX_MATERIAL_BYTES', 50 * 1024 * 1024 - 1)), 50 * 1024 * 1024 - 1),
                'max_project_files': 50, 'max_batch_files': 50, 'max_batch_bytes': 500 * 1024 * 1024,
                **self.ports.hook_material_capacity(dict(self.ports.app().config))}

    def add_materials(self, project_id, files, metadata, user_id, role='material', upload_id=None,
                      declared_sha=None, batch_count=None, batch_bytes=None):
        if self.ports.app().config.get('WUYU_QUOTA_ENABLED',True)and user_id:
            self.ports.hook_get_quota_service(self.ports.app()).assert_work_allowed(user_id)
        self.ports._ensure(project_id)
        limits = self.ports.material_limits()
        if role == 'template':
            template_reading_guard(self.ports._read(project_id)[1])
        if upload_id is not None:
            try:
                uuid.UUID(upload_id)
            except (ValueError, TypeError, AttributeError):
                raise WorkflowAPIError('Invalid upload request ID')
        for value, maximum in [(batch_count, limits['max_batch_files']), (batch_bytes, limits['max_batch_bytes'])]:
            if value is not None and (not str(value).isdigit() or not 1 <= int(value) <= maximum):
                raise WorkflowAPIError('Material batch exceeds upload limits', 'MATERIAL_BATCH_LIMIT', 413)
        def append_material(body, item):
            self.ports._material_management_guard(body)
            if item.get('role') == 'template':
                template_reading_guard(body)
            if role == 'material' and body['brief'].get('task_mode') == 'relayout':
                if (not item.get('filename', '').lower().endswith('.pptx') or item.get('bytes', 0) > 20*1024*1024 or any(m.get('role') == 'material' for m in body['materials'])):
                    raise WorkflowAPIError('重排模式只接受一份20MiB以内的PPTX原稿；更换原稿请新建对话。', 'RELAYOUT_SOURCE_INVALID', 400)
            self.ports._material_capacity_guard(body['materials'] + [item])
            if sum(m.get('origin') != 'web_research' for m in body['materials']) >= limits['max_project_files']:
                raise WorkflowAPIError('Project already has the maximum number of materials', 'MATERIAL_COUNT_LIMIT', 413)
            if item.get('upload_id') and any(m.get('upload_id') == item['upload_id'] for m in body['materials']):
                raise WorkflowAPIError('Upload ID is already in use; read the current project before retrying', 'UPLOAD_ID_CONFLICT', 409)
            body['materials'].append(item)
            if item.get('role') == 'material':
                self.ports._reading_invalidate(body)
                if body.get('reading_protocol')==1:
                    # Keep earlier parse jobs; stop attaching model output based
                    # on a material set the user has just changed.
                    db.session.execute(update(WuyuWorkflowJob).where(
                        WuyuWorkflowJob.project_id==project_id,
                        WuyuWorkflowJob.status.in_(['queued','running']),
                        WuyuWorkflowJob.action!='_parse_materials').values(cancel_requested=True))
        if role not in {'material', 'template', 'logo'}:
            raise WorkflowAPIError('Invalid material role')
        if metadata is not None and role == 'material' and self.ports._read(project_id)[1]['brief'].get('task_mode') == 'relayout':
            raise WorkflowAPIError('重排模式请上传PPTX原稿。', 'RELAYOUT_SOURCE_INVALID')
        if metadata is not None:
            # JSON accepts text evidence only, never arbitrary filesystem paths.
            text = metadata.get('text') if isinstance(metadata, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 200000:
                raise WorkflowAPIError('Provide nonempty evidence text up to 200000 characters')
            material = {'id': self.ports.hook__id(), 'filename': str(metadata.get('filename', '用户提供材料'))[:200], 'role': role,
                        'status': 'completed', 'text': text, 'chunks': [{'id': self.ports.hook__id(), 'text': text, 'source': 'user_supplied',
                            'evidence_kind': 'style_reference' if role in {'template', 'logo'} else 'project_material'}]}
            self.ports._mutate(project_id, lambda body: append_material(body, material))
            return dict(self.ports.snapshot(project_id), uploaded_material_ids=[material['id']])
        if not files or len(files) > limits['max_batch_files']:
            raise WorkflowAPIError('Upload between 1 and 50 files', 'MATERIAL_BATCH_LIMIT', 413)
        if upload_id and len(files) != 1:
            raise WorkflowAPIError('Upload request IDs apply to one file at a time')
        added = []
        batch_total = 0
        for upload in files:
            original = upload.filename or ''
            extension = Path(original).suffix.lower()
            if extension not in self.ports.EXTENSIONS():
                raise WorkflowAPIError('Unsupported material file type')
            _, current = self.ports._read(project_id)
            self.ports._material_management_guard(current)
            previous = next((m for m in current['materials'] if upload_id and m.get('upload_id') == upload_id), None)
            if previous:
                if previous['filename'] != Path(original).name or previous['role'] != role or not declared_sha or previous['sha256'] != declared_sha:
                    raise WorkflowAPIError('Upload ID conflicts with the existing material', 'UPLOAD_ID_CONFLICT', 409)
                return dict(self.ports.snapshot(project_id), uploaded_material_ids=[previous['id']], upload_reused=True)
            if sum(m.get('origin') != 'web_research' for m in current['materials']) >= limits['max_project_files']:
                raise WorkflowAPIError('Project already has the maximum number of materials', 'MATERIAL_COUNT_LIMIT', 413)
            name = self.ports.hook__id() + extension
            path = self.ports._path(project_id, 'materials', name)
            total = 0
            digest = hashlib.sha256()
            with path.open('wb') as handle:
                while chunk := upload.stream.read(1024 * 1024):
                    total += len(chunk)
                    batch_total += len(chunk)
                    if self.ports.hook_material_usage(current['materials'])['bytes'] + total > limits['max_project_bytes']:
                        handle.close()
                        path.unlink(missing_ok=True)
                        raise WorkflowAPIError('项目累计材料超过容量，请先删除不需要的资料。', 'MATERIAL_PROJECT_LIMIT', 413)
                    if total > (min(limits['max_file_bytes'],20*1024*1024) if role == 'material' and current['brief'].get('task_mode') == 'relayout' else limits['max_file_bytes']) or batch_total > limits['max_batch_bytes']:
                        handle.close()
                        path.unlink(missing_ok=True)
                        raise WorkflowAPIError('Material exceeds upload size limit', 'RELAYOUT_SOURCE_INVALID' if role == 'material' and current['brief'].get('task_mode') == 'relayout' else 'MATERIAL_UPLOAD_LIMIT', 413)
                    digest.update(chunk)
                    handle.write(chunk)
            if total == 0:
                path.unlink(missing_ok=True)
                raise WorkflowAPIError('Empty material')
            if declared_sha and digest.hexdigest() != declared_sha:
                path.unlink(missing_ok=True)
                raise WorkflowAPIError('Uploaded bytes do not match the declared checksum', 'MATERIAL_HASH_MISMATCH', 400)
            root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
            ref = ReferenceFile(project_id=project_id, user_id=user_id, filename=Path(original).name,
                file_path=path.relative_to(root).as_posix(), file_size=total, file_type=extension.lstrip('.'), parse_status='pending')
            db.session.add(ref)
            db.session.commit()
            item = {'id': ref.id, 'filename': Path(original).name, 'relative_path': ref.file_path, 'bytes': total, 'role': role,
                    'sha256': digest.hexdigest(), 'status': 'pending', 'chunks': [], 'upload_id': upload_id}
            try:
                self.ports._mutate(project_id, lambda body, item=item: append_material(body, item))
            except Exception:
                path.unlink(missing_ok=True)
                db.session.delete(ref)
                db.session.commit()
                raise
            artifact_uri = self.ports._record_artifact(project_id, path, 'source_material', None,
                                                {'role': role, 'material_id': ref.id})
            def bind_artifact(body):
                next(m for m in body['materials'] if m['id'] == ref.id)['artifact_id'] = artifact_uri.removeprefix('artifact:')
            self.ports._mutate(project_id, bind_artifact)
            added.append(ref.id)
        parse_ids = added
        if role == 'template':
            def select_uploaded(body):
                selected = next(m for m in body['materials'] if m['id'] == added[-1])
                body['reference_recipe'] = {'style_anchor': 'material:' + added[-1], 'references': [],
                    'selection': {'kind': 'uploaded_default', 'title': selected['filename'], 'material_id': added[-1]}}
                body['workflow'] = self.ports._new_state(project_id, body['brief'], body['workflow']['review_policy'])
                body['epoch'] += 1
                body['history'].append({'at': self.ports.hook__now(), 'action': 'uploaded_template_selected', 'status': 'accepted'})
                self.ports._cancel_old_jobs(project_id)
            _, selected_body = self.ports._mutate(project_id, select_uploaded)
            # The new template cancels prior-epoch parse jobs as well as generation.
            # Carry their unfinished materials forward without retrying old failures.
            parse_ids = [item['id'] for item in selected_body['materials']
                         if item['status'] in {'pending', 'processing'} or item['id'] in added]
        return dict(self.ports._enqueue_response(project_id, '_parse_materials', {'material_ids': parse_ids}), uploaded_material_ids=added)

    def _parse_materials(self, job, pipeline, payload):
        failures = []
        for ident in payload['material_ids']:
            self.ports._check_cancel(job)
            _, body = self.ports._read(job.project_id)
            material = next(m for m in body['materials'] if m['id'] == ident)
            if material['status'] == 'completed':
                continue
            path = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve() / material['relative_path']
            try:
                with self.ports._material_parser_capacity(pipeline, body, ident):
                    parsed = pipeline.parse_material(str(path), filename=material['filename'], role=material['role'],
                        output_dir=str(self.ports._path(job.project_id, 'template_previews' if material['role'] == 'template' else 'material_assets', material['id'])),
                        cancel_check=lambda: self.ports._check_cancel(job))
                if set(parsed.get('processing_limits', {}).get('limits_hit', [])) & {'max_chars', 'max_chunks'}:
                    raise PipelineError('material_project_capacity', '材料解析超过项目容量，请缩小文件或先删除不需要的资料。')
                self.ports._check_cancel(job)
                images = []
                for number, image in enumerate(parsed.get('template_images') or [], 1):
                    uri = self.ports._record_artifact(job.project_id, image['path'], 'uploaded_template_preview', job,
                                                {'material_id': ident, 'number': image.get('number', number)})
                    root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
                    image_path = Path(image['path']).resolve()
                    images.append({'number': image.get('number', number), 'artifact_id': uri.removeprefix('artifact:'),
                                   'relative_path': image_path.relative_to(root).as_posix(),
                                   'sha256': hashlib.sha256(image_path.read_bytes()).hexdigest()})
                source_assets = []
                for asset in (parsed.get('source_assets') or [])[:600]:
                    asset = deepcopy(asset)
                    asset_path = Path(asset.get('path', '')).resolve()
                    root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve() / job.project_id
                    if not asset_path.is_relative_to(root) or not asset_path.is_file() or hashlib.sha256(asset_path.read_bytes()).hexdigest() != asset.get('sha256'):
                        raise WorkflowAPIError('Parser returned an invalid source figure')
                    asset.update(material_id=ident, project_id=job.project_id)
                    asset['evidence_kind'] = 'project_material' if material['role'] == 'material' else 'style_reference'
                    uri = self.ports._record_artifact(job.project_id, asset_path, 'source_figure', job,
                        {'material_id': ident, 'source_asset_id': asset['id'], 'figure_label': asset.get('figure_label'),
                         'source': self.ports.hook__public_source(asset.get('source', {})), 'original_bytes_preserved': asset.get('original_bytes_preserved', True)})
                    asset['artifact_id'] = uri.removeprefix('artifact:')
                    source_assets.append(asset)
            except CancelledWork:
                raise
            except Exception as error:
                self.ports._check_cancel(job)
                code = ('MATERIAL_PROJECT_LIMIT' if isinstance(error, WorkflowAPIError)
                    and error.code == 'MATERIAL_PROJECT_LIMIT' else self.ports.hook_material_parse_error_code(error))
                def fail(current):
                    item = next(m for m in current['materials'] if m['id'] == ident)
                    item.update(status='failed', error_code=code)
                    ref = db.session.get(ReferenceFile, ident)
                    ref.parse_status, ref.error_message = 'failed', self.ports.hook__failure_message(code)
                self.ports._mutate(job.project_id, fail, job=job)
                failures.append(ident)
                continue
            text = parsed.get('text') or '\n\n'.join(chunk.get('text', '') for chunk in parsed.get('chunks', []))
            def op(current):
                item = next(m for m in current['materials'] if m['id'] == ident)
                chunks = deepcopy(parsed.get('chunks', []))
                if material['role'] in {'template', 'logo'}:
                    for chunk in chunks:
                        chunk['evidence_kind'] = 'style_reference'
                item.update(status='completed', text=text, chunks=chunks, template_images=images, source_assets=source_assets,
                            warnings=parsed.get('warnings', []), ocr_status=parsed.get('ocr_status'),
                            template_render_status=parsed.get('template_render_status'),
                            source_structured_data=deepcopy(parsed.get('source_structured_data', {})),
                            processing_limits=deepcopy(parsed.get('processing_limits', {})),
                            ocr_report=[{key: value for key, value in report.items()
                                if key in {'page', 'status', 'reason', 'available', 'engine', 'language', 'machine_ocr'}
                                and (value is None or isinstance(value, (bool, int)) or self.ports.hook__report_text(value) is not None)}
                                for report in parsed.get('ocr_report', []) if isinstance(report, dict)])
                self.ports._material_capacity_guard(current['materials'])
                item.pop('error_code', None)
                item.pop('error_message', None)
                if material['role'] in {'template', 'logo'}:
                    for record in item['source_structured_data'].values():
                        record['evidence_kind'] = 'style_reference'
            try:
                self.ports._mutate(job.project_id, op, job=job)
            except WorkflowAPIError as error:
                if error.code != 'MATERIAL_PROJECT_LIMIT':raise
                def full(current):
                    item = next(m for m in current['materials'] if m['id'] == ident)
                    item.update(status='failed', error_code='MATERIAL_PROJECT_LIMIT')
                self.ports._mutate(job.project_id, full, job=job)
                ref = db.session.get(ReferenceFile, ident)
                ref.parse_status, ref.error_message = 'failed', self.ports.hook__failure_message('MATERIAL_PROJECT_LIMIT')
                db.session.commit()
                failures.append(ident)
                continue
            ref = db.session.get(ReferenceFile, ident)
            ref.parse_status, ref.markdown_content, ref.error_message = 'completed', text, None
            db.session.commit()
        self.ports._check_cancel(job)
        if failures:
            raise PipelineError('material_batch_partial', '部分材料未能解析，其他已完成结果已保留；请查看材料列表，在原对话补传或明确排除失败文件。')

    def evidence(self, project_id, query='', limit=8, material_id=None, page=None, offset=0, max_chars=12000, include_figures=True):
        _, body = self.ports._read(project_id)
        query = str(query).strip().lower()[:500]
        limit, offset, max_chars = max(1, min(int(limit), 12)), max(0, int(offset)), max(1000, min(int(max_chars), 24000))
        materials = [m for m in body['materials'] if not material_id or m['id'] == material_id]
        if material_id and not materials:
            raise WorkflowAPIError('Material not found in this project', 'MATERIAL_NOT_FOUND', 404)
        chunks = []
        # Stable 1000-character windows keep pagination complete even for one huge pasted-text chunk.
        for material in materials:
            for chunk in material.get('chunks', []):
                source = chunk.get('source', {})
                if page is not None and (not isinstance(source, dict) or source.get('page') != page):
                    continue
                text = str(chunk.get('text', ''))
                if query and not (query in text.lower() or any(word in text.lower() for word in query.split())):
                    continue
                for start in range(0, len(text), 1000):
                    chunks.append(dict(chunk, text=text[start:start + 1000], text_offset=start, material_id=material['id'],
                                       filename=material['filename'], trust='untrusted_source_content'))
        selected, used = [], 0
        for chunk in chunks[offset:offset + limit]:
            if used + len(chunk['text']) > max_chars:
                break
            selected.append(chunk)
            used += len(chunk['text'])
        assets = [self.ports._public_asset(project_id, asset) for m in materials for asset in m.get('source_assets', [])
                  if page is None or asset.get('source', {}).get('page') == page] if include_figures else []
        next_offset = offset + len(selected) if offset + len(selected) < len(chunks) else None
        return {'project_id': project_id, 'query': query, 'results': self.ports.hook__public_source(selected), 'total': len(chunks),
                'offset': offset, 'next_offset': next_offset, 'returned_chars': used, 'truncated': next_offset is not None,
                'source_assets': assets[:64], 'source_instructions_are_data': True,
                'materials': [{k: m.get(k) for k in ['id', 'filename', 'status', 'role', 'processing_limits', 'ocr_status']} for m in materials]}
