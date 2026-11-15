"""Local PPTX intake for the existing reviewed image-presentation workflow."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import posixpath
import re
import uuid
import zipfile

from PIL import Image
from models import db, Task
from models.wuyu_workflow import WuyuWorkflowJob
from .wuyu_errors import PipelineError, check_cancel
from .wuyu_materials import NS, check_zip, read_xml, office_preview_safety, allowed_file
from .wuyu_render import render_exported_pptx


def inspect_pptx(path, config, cancel=None):
    """Read ordered native text without running Office or calling a provider."""
    path = Path(path)
    if path.suffix.lower() != '.pptx' or path.stat().st_size > 20 * 1024 * 1024:
        raise PipelineError('relayout_file_limit', '重新排版请上传20MiB以内的PPTX文件。')
    with zipfile.ZipFile(path) as archive:
        check_zip(archive, 128 * 1024 * 1024)
        pres = read_xml(archive, 'ppt/presentation.xml')
        size = pres.find('p:sldSz', NS)
        width, height = int(size.get('cx', 0)), int(size.get('cy', 0))
        if min(width, height) <= 0 or abs(width / height - 16 / 9) > .01:
            raise PipelineError('relayout_aspect_ratio', '当前重新排版仅支持16:9原稿，其他比例尚未转换或拉伸。')
        rels = {r.get('Id'): r for r in read_xml(archive, 'ppt/_rels/presentation.xml.rels')}
        slides = pres.findall('p:sldIdLst/p:sldId', NS)
        if not 1 <= len(slides) <= 80:
            raise PipelineError('relayout_page_limit', '重新排版支持1–80页，请拆分过大的PPTX。')
        pages = []
        for number, slide in enumerate(slides, 1):
            check_cancel(cancel)
            rel = rels.get(slide.get('{' + NS['r'] + '}id'))
            if rel is None or rel.get('TargetMode', '').lower() == 'external':
                raise PipelineError('unsafe_archive', 'PPTX页关系无效。')
            target = rel.get('Target', '')
            part = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('ppt', target))
            if not part.startswith('ppt/slides/') or '..' in part.split('/'):
                raise PipelineError('unsafe_archive', 'PPTX页关系越界。')
            xml = read_xml(archive, part)
            # Hidden slides are retained in the source; LO's PDF export can omit
            # them, so reject explicitly rather than shift every later page.
            if xml.get('show') in {'0', 'false'}:
                raise PipelineError('relayout_hidden_slides', '原稿含隐藏页，请先在副本中取消隐藏或移除后上传，避免页码错位。')
            lines = [''.join(t.text or '' for t in p.findall('.//a:t', NS)) for p in xml.findall('.//a:p', NS)]
            text = '\n'.join(line for line in lines if line.strip())
            if len(text) > 30000:
                raise PipelineError('relayout_text_limit', '单页原文超过30000字，请拆分页；尚未截断原文。')
            pages.append({'number': number, 'title': (next((x for x in lines if x.strip()), f'第{number}页'))[:500], 'text': text})
        if sum(len(p['text']) for p in pages) > 200000:
            raise PipelineError('relayout_text_limit', '原稿文字超过20万字，请拆分后上传。')
    unsafe = office_preview_safety(path, cancel)
    if unsafe:
        raise PipelineError('relayout_unsafe_source', '原稿含外部加载资源或活动嵌入内容，暂不能安全转图。')
    return pages


def current_source(body):
    """Only this exact material revision may use direct page-reference intake."""
    value = body.get('relayout') or {}
    source = next((m for m in body['materials'] if m['id'] == value.get('material_id')), None)
    selected = [m for m in body['materials'] if m.get('role') == 'material' and m['id'] not in body.get('reading_exclusions', {})]
    return (source if value.get('status') == 'ready' and source and selected == [source]
            and source.get('sha256') == value.get('source_sha256')
            and value.get('material_revision') == body.get('material_revision', 0) else None)


class RelayoutMixin:
    def _relayout_validate_action(self, body, action, payload):
        from .wuyu_workflow_service import WorkflowAPIError
        if action == 'update_brief' and 'task_mode' in payload:
            if payload['task_mode'] not in {'create','relayout'}:
                raise WorkflowAPIError('任务类型必须为新建或重排。')
            if payload['task_mode'] != body['brief'].get('task_mode','create') and payload['task_mode'] == 'create':
                return
        if (body['brief'].get('task_mode') == 'relayout' or body.get('relayout')) and action in {'request_research','run_research','confirm_research','supplement_research','accept_research'}:
            raise WorkflowAPIError('重排保留原稿内容；需要新增联网资料请新建PPT任务。', 'RELAYOUT_RESEARCH_UNAVAILABLE', 409)
        source = current_source(body)
        if (body['brief'].get('task_mode') == 'relayout' or body.get('relayout')) and not source and action in {
                'analyze_materials','revise_draft','generate_outline','generate_samples','regenerate_samples','generate_next',
                'generate_focused','generate_images','_continue','start_tracing','export','request_research'}:
            raise WorkflowAPIError('请先上传原稿并完成重新排版准备，再确认新模板。', 'RELAYOUT_SOURCE_REQUIRED', 409)
        if not source:
            return
        if action in {'generate_outline', 'analyze_materials', 'revise_draft', 'generate_samples', 'regenerate_samples'}:
            raise WorkflowAPIError('原稿已按原页准备完成，请直接核对文案、确认新模板；修改文案使用page_patches。', 'RELAYOUT_ALREADY_PREPARED', 409)
        if action == 'update_brief':
            expected = {'page_count': body['relayout']['page_count'], 'creation_kind': body['brief'].get('creation_kind', 'deck'),
                        'aspect_ratio': '16:9'}
            if any(key in payload and payload[key] != value for key, value in expected.items()):
                raise WorkflowAPIError('重新排版保留原页数和比例；首版暂不支持增删页或改变比例。')
            if payload.get('review_policy') == 'guided' or payload.get('preserve_outline') is False:
                raise WorkflowAPIError('重新排版使用原逐页文案和最终整稿审核，请保留原稿计划。')
        if action == 'update_outline' and 'page_patches' not in payload:
            raise WorkflowAPIError('请使用page_patches修改指定页，保留其他页原文和原图引用。')
        if action in {'revise_page', 'update_outline'}:
            targets = [payload.get('number')] if action == 'revise_page' else [p.get('number') for p in payload.get('page_patches', []) if isinstance(p, dict)]
            if any(n not in body['relayout']['selected_pages'] for n in targets):
                raise WorkflowAPIError('该页未在本次重新排版范围中；请在生成前重新选择页范围。')

    def prepare_relayout(self, project_id, payload):
        from .wuyu_workflow_service import WorkflowAPIError
        if not isinstance(payload, dict) or set(payload) - {'material_id', 'selected_pages', 'expected_revision'}:
            raise WorkflowAPIError('请选择一份已上传的PPTX和要重新排版的页。')
        revision, body = self._read(project_id)
        source = next((m for m in body['materials'] if m['id'] == payload.get('material_id') and m.get('role') == 'material'), None)
        if not source or not source.get('filename', '').lower().endswith('.pptx'):
            raise WorkflowAPIError('请先在上传资料中上传PPTX，再指定它作为原稿。')
        if source.get('status') != 'completed':
            raise WorkflowAPIError('原稿仍在解析或解析失败，请先等待或重试材料解析。', 'RELAYOUT_SOURCE_NOT_READY', 409)
        if any(m.get('role') == 'material' and m['id'] != source['id'] and m['id'] not in body.get('reading_exclusions', {}) for m in body['materials']):
            raise WorkflowAPIError('保留原稿逐页设计目前使用一份PPTX。请明确暂不使用其他资料，或改为资料内容重组；已上传文件全部保留。')
        roots = self.app.config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [self.app.config['UPLOAD_FOLDER']]
        roots = roots if isinstance(roots, list) else [roots]
        path = allowed_file(Path(self.app.config['UPLOAD_FOLDER']) / source['relative_path'], roots)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source.get('sha256'):
            raise WorkflowAPIError('原稿校验不一致，请重新上传。')
        # Page-count validation is cheap, bounded and independent of model setup.
        try:
            pages = inspect_pptx(path, self.app.config)
        except PipelineError as exc:
            raise WorkflowAPIError(str(exc), exc.code) from None
        except (ValueError, KeyError, AttributeError, zipfile.BadZipFile):
            raise WorkflowAPIError('PPTX结构损坏或加密，无法读取原稿。') from None
        selected = payload.get('selected_pages', list(range(1, len(pages) + 1)))
        if not isinstance(selected, list) or not selected or len(selected) > 80 or any(type(n) is not int or not 1 <= n <= len(pages) for n in selected) or len(set(selected)) != len(selected):
            raise WorkflowAPIError('重新排版页号必须是原稿中不重复的有效页码。')
        selected = sorted(selected)
        fingerprint = hashlib.sha256(json.dumps([source['id'], source['sha256'], selected]).encode()).hexdigest()
        prior = body.get('relayout') or {}
        if prior.get('fingerprint') == fingerprint and prior.get('material_revision') == body.get('material_revision', 0):
            if prior.get('status') == 'ready':
                return self.snapshot(project_id)
            old = WuyuWorkflowJob.query.filter_by(project_id=project_id, action='_prepare_relayout').order_by(WuyuWorkflowJob.created_at.desc()).first()
            if old and old.status in {'queued', 'running'}:
                return dict(self.snapshot(project_id), task_id=old.task_id, job=self.get_job(project_id, old.id))
        def enqueue(current):
            if WuyuWorkflowJob.query.filter_by(project_id=project_id).filter(WuyuWorkflowJob.status.in_(['queued', 'running'])).first():
                raise WorkflowAPIError('请等待当前项目任务结束后再准备重新排版。', 'RELAYOUT_BUSY', 409)
            if current.get('relayout'):
                current.setdefault('relayout_history', []).append(deepcopy(current['relayout']))
            current['epoch'] += 1
            current['paused'] = False
            current['relayout'] = {'status': 'queued', 'material_id': source['id'], 'filename': source['filename'],
                'source_sha256': source['sha256'], 'material_revision': current.get('material_revision', 0),
                'page_count': len(pages), 'selected_pages': selected, 'completed_pages': 0, 'fingerprint': fingerprint,
                'content_policy': 'preserve_original_content', 'aspect_ratio': '16:9'}
            return self._new_job_record(project_id, '_prepare_relayout', {}, current)
        expected = payload.get('expected_revision')
        if type(expected) is not int:
            raise WorkflowAPIError('请提供当前项目revision，刷新后重试。')
        job_id, _ = self._mutate(project_id, enqueue, expected_revision=expected)
        if self.app.config.get('WUYU_WORKFLOW_INLINE'):
            self.run_pending(project_id)
        job = db.session.get(WuyuWorkflowJob, job_id)
        return dict(self.snapshot(project_id), task_id=job.task_id, job=self.get_job(project_id, job_id))

    def _prepare_relayout(self, job):
        _, body = self._read(job.project_id)
        value = body['relayout']
        source = next(m for m in body['materials'] if m['id'] == value['material_id'])
        path = Path(self.app.config['UPLOAD_FOLDER']).resolve() / source['relative_path']
        cancel = lambda: self._check_cancel(job)
        if hashlib.sha256(path.read_bytes()).hexdigest() != value['source_sha256']:
            raise PipelineError('relayout_source_changed', '原稿已改变，请重新上传。')
        pages = inspect_pptx(path, self.app.config, cancel)
        output = self._path(job.project_id, 'relayout_sources', value['source_sha256'])
        output.mkdir(parents=True, exist_ok=True)
        cache = output / 'manifest.json'
        paths = []
        if cache.is_file():
            try:
                saved = json.loads(cache.read_text())
            except (ValueError, OSError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            if saved.get('source_sha256') == value['source_sha256'] and isinstance(saved.get('images'), list) and len(saved['images']) == len(pages):
                for number, item in enumerate(saved['images'], 1):
                    if not isinstance(item, dict):
                        break
                    relative = item.get('relative_path', '')
                    if not re.fullmatch(r'[0-9a-f-]{36}/page-' + f'{number:03d}' + r'\.png', relative):
                        break
                    image = output / relative
                    if image.is_file() and hashlib.sha256(image.read_bytes()).hexdigest() == item['sha256']:
                        paths.append(str(image))
        self._relayout_progress(job, 'converting', len(paths) if len(paths) == len(pages) else 0)
        if len(paths) != len(pages):
            attempt_dir = output / str(uuid.uuid4())
            rendered = render_exported_pptx(path, dict(self.app.config, WUYU_RENDER_TIMEOUT=90), expected_pages=len(pages), cancel_check=cancel, output_dir=attempt_dir)
            if not rendered.get('available') or len(rendered.get('paths', [])) != len(pages):
                raise PipelineError('relayout_render_failed', '原稿转图未完成，请重试；已上传原件保留。', True)
            paths = rendered['paths']
            manifest = {'source_sha256': value['source_sha256'], 'images': [{'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest(), 'relative_path': Path(p).relative_to(output).as_posix()} for p in paths]}
            temporary = cache.with_suffix('.tmp')
            temporary.write_text(json.dumps(manifest))
            temporary.replace(cache)
        assets = []
        for page, raw in zip(pages, paths):
            cancel()
            image = Path(raw)
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            old = next((a for a in body['artifacts'] if a['kind'] == 'relayout_source_page' and a.get('sha256') == digest and a.get('metadata', {}).get('number') == page['number']), None)
            ident = old['id'] if old else self._record_artifact(job.project_id, image, 'relayout_source_page', job, {'number': page['number'], 'material_id': source['id'], 'source_sha256': value['source_sha256']}).removeprefix('artifact:')
            with Image.open(image) as decoded:
                width, height = decoded.size
            assets.append({'id': 'relayout:' + value['source_sha256'][:16] + ':' + str(page['number']), 'path': str(image),
                'sha256': digest, 'artifact_id': ident, 'width': width, 'height': height, 'material_id': source['id'],
                'project_id': job.project_id, 'evidence_kind': 'project_material', 'extraction_method': 'original_pptx_page_render',
                'original_bytes_preserved': False, 'figure_label': f"原稿第{page['number']}页",
                'source': {'filename': source['filename'], 'page': page['number'], 'material_id': source['id']},
                'caption': page['text'][:1000]})
            self._relayout_progress(job, 'registering_pages', page['number'])
        def finish(current):
            source_now = next(m for m in current['materials'] if m['id'] == source['id'])
            # Keep embedded originals, register page references independently.
            source_now['relayout_source'] = True
            source_now['source_assets'] = [a for a in source_now.get('source_assets', []) if a.get('extraction_method') != 'original_pptx_page_render'] + assets
            self._material_capacity_guard(current['materials'])
            count = len(pages)
            current['brief'].update(task_mode='relayout', page_count=count, creation_kind='pages' if count == 1 else 'deck', aspect_ratio='16:9',
                output_preference='image', topic=current['brief'].get('topic') or source['filename'])
            current['workflow'] = self._new_state(job.project_id, current['brief'], 'template_only' if count == 1 else 'template_and_final')
            specs = [{'number': p['number'], 'title': p['title'], 'copy': p['text'], 'points': [],
                'source_asset_ids': [a['id']], 'relayout_source': {'original_page': p['number'], 'preserve_all_content': True},
                'layout_rationale': '保留原页全部信息，按用户新确认的模板与风格重新编排。'} for p, a in zip(pages, assets)]
            current['slide_specs'] = self._bind_source_assets(self._normalize_specs(specs, trusted=True, brief=current['brief']), current)
            current['outline'] = [{'number': p['number'], 'title': p['title']} for p in pages]
            current['relayout'].update(status='ready', completed_pages=count,
                previews=[{'number': a['source']['page'], 'image_uri': 'artifact:' + a['artifact_id']} for a in assets])
            current['planning_report'] = {'mode': 'relayout_original_pages', 'provider_called': False,
                'notice': '原文直接提取，图片文字及图表请对照原页预览审核；尚未生图。'}
        self._mutate(job.project_id, finish, job=job)
        self._relayout_progress(job, 'ready', len(pages))

    def _relayout_progress(self, job, stage, completed):
        def op(body):
            body['relayout'].update(status='ready' if stage == 'ready' else stage, completed_pages=completed)
            progress = {'stage': 'relayout_' + stage, 'total': body['relayout']['page_count'], 'completed': completed, 'failed': 0}
            job.progress = json.dumps(progress)
            task = db.session.get(Task, job.task_id)
            if task:
                task.set_progress(progress)
        self._mutate(job.project_id, op, job=job)

    def _relayout_seed_unchanged(self, job):
        _, body = self._read(job.project_id)
        source = current_source(body)
        if not source:
            return
        value = body['relayout']
        for page in body['workflow']['pages']:
            if page['number'] in value['selected_pages'] or page['generation_status'] == 'ready':
                continue
            asset = next(a for a in source['source_assets'] if a.get('extraction_method') == 'original_pptx_page_render' and a['source']['page'] == page['number'])
            if hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                raise PipelineError('relayout_source_changed', '原页图已改变，请重新准备原稿。')
            attempt = str(uuid.uuid4())
            self._event(job.project_id, 'page.generate_started', {'number': page['number'], 'attempt_id': attempt}, job)
            self._event(job.project_id, 'page.generated', {'number': page['number'], 'attempt_id': attempt,
                'copy': body['slide_specs'][page['number'] - 1]['copy'], 'image_uri': 'artifact:' + asset['artifact_id']}, job)
            self._sync_page(job.project_id, page['number'], body['slide_specs'][page['number'] - 1], asset['path'], epoch=job.input_epoch, job=job)
