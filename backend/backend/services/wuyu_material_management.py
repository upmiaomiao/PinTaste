"""Project material capacity, reading admission, and owner-confirmed erasure."""
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
import os
import json
import shutil
import uuid

from .wuyu_errors import PipelineError


def _integer(config, name, default, maximum):
    value = config.get(name, os.getenv(name, default))
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= maximum:
        raise PipelineError('material_capacity_config', '材料容量设置无效，请联系管理员。')
    return int(value)


def capacity(config):
    return {
        'max_project_bytes': _integer(config, 'WUYU_MAX_PROJECT_MATERIAL_BYTES', 500 * 1024**2, 2500 * 1024**2),
        # The complete JSON response also contains chunks, locators and previews.
        # Keep retained source text below the existing 32 MiB transport ceiling.
        'max_project_text_chars': _integer(config, 'WUYU_MAX_PROJECT_MATERIAL_CHARS', 2_000_000, 2_000_000),
        'max_project_chunks': _integer(config, 'WUYU_MAX_PROJECT_MATERIAL_CHUNKS', 10_000, 10_000),
        'max_project_source_assets': _integer(config, 'WUYU_MAX_PROJECT_SOURCE_ASSETS', 1000, 1000),
        'max_project_parsed_bytes': _integer(config, 'WUYU_MAX_PROJECT_PARSED_BYTES', 24 * 1024**2, 24 * 1024**2),
        'reading_prepare_seconds': _integer(config, 'WUYU_READING_PREPARE_SECONDS', 60, 180),
    }


def usage(materials):
    rows = list(materials)
    return {
        'files': sum(m.get('origin') != 'web_research' for m in rows),
        'bytes': sum(max(0, int(m.get('bytes') or 0)) for m in rows if m.get('origin') != 'web_research'),
        'text_chars': sum(len(m['text']) if isinstance(m.get('text'), str) else
                          sum(len(c.get('text') or '') for c in m.get('chunks', [])) for m in rows),
        'chunks': sum(len(m.get('chunks') or []) for m in rows),
        'source_assets': sum(len(m.get('source_assets') or []) for m in rows),
    }


def admit_reading(materials, config, cancel_check=None):
    exclusions = config.get('WUYU_READING_EXCLUSIONS') or {}
    selected = [m for m in materials if m.get('role') in (None, '', 'material') and m['id'] not in exclusions]
    stats, limits = usage(selected), capacity(config)
    if cancel_check:
        cancel_check()
    if (stats['text_chars'] > limits['max_project_text_chars'] or stats['chunks'] > limits['max_project_chunks']
            or stats['source_assets'] > limits['max_project_source_assets']):
        raise PipelineError('reading_material_capacity',
                            '选用材料超过本轮阅读容量；请减少选用范围或删除不需要的材料。本次尚未调用模型。')
    return stats


class MaterialManagementMixin:
    def _material_management_guard(self, body):
        if any(v.get('status') == 'pending' for v in body.get('material_deletions', {}).values()):
            from .wuyu_workflow_service import WorkflowAPIError
            raise WorkflowAPIError('材料删除尚未完成，请在材料列表重试删除后继续。', 'MATERIAL_DELETE_PENDING', 409)

    def _material_capacity_guard(self, materials):
        from .wuyu_workflow_service import WorkflowAPIError
        stats, limits = usage(materials), self.material_limits()
        if (stats['files'] > limits['max_project_files'] or stats['bytes'] > limits['max_project_bytes']
                or stats['text_chars'] > limits['max_project_text_chars'] or stats['chunks'] > limits['max_project_chunks']
                or stats['source_assets'] > limits['max_project_source_assets']
                or len(json.dumps(materials, ensure_ascii=False, separators=(',', ':')).encode()) > limits['max_project_parsed_bytes']):
            raise WorkflowAPIError('项目材料容量已满，请先删除不需要的材料或缩小文件后再上传。',
                                   'MATERIAL_PROJECT_LIMIT', 413)

    def _material_parse_config(self, body, ident, config):
        from .wuyu_workflow_service import WorkflowAPIError
        self._material_management_guard(body)
        other = usage(m for m in body['materials'] if m['id'] != ident)
        limits = capacity(config)
        chars = limits['max_project_text_chars'] - other['text_chars']
        chunks = limits['max_project_chunks'] - other['chunks']
        if chars <= 0 or chunks <= 0:
            raise WorkflowAPIError('项目解析容量已满，请先删除不需要的材料。', 'MATERIAL_PROJECT_LIMIT', 413)
        return dict(config, WUYU_MAX_MATERIAL_CHARS=min(int(config.get('WUYU_MAX_MATERIAL_CHARS', chars)), chars),
                    WUYU_MAX_MATERIAL_CHUNKS=min(int(config.get('WUYU_MAX_MATERIAL_CHUNKS', chunks)), chunks))

    @contextmanager
    def _material_parser_capacity(self, pipeline, body, ident):
        original = getattr(pipeline, 'config', None)
        pipeline.config = self._material_parse_config(body, ident, original or dict(self.app.config))
        try:
            yield
        finally:
            if original is None:
                del pipeline.config
            else:
                pipeline.config = original

    def delete_material(self, project_id, material_id, user_id, *, confirmed, request_id, expected_revision=None):
        from models import db, Project, ReferenceFile
        from models.wuyu_workflow import WuyuWorkflowJob
        from .wuyu_workflow_service import WorkflowAPIError
        project = db.session.get(Project, project_id)
        if not project or project.user_id != user_id:
            raise WorkflowAPIError('只能删除自己项目中的材料。', 'MATERIAL_DELETE_FORBIDDEN', 403)
        if confirmed is not True:
            raise WorkflowAPIError('请先确认永久删除此材料。', 'MATERIAL_DELETE_CONFIRMATION_REQUIRED', 403)
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise WorkflowAPIError('删除请求标识无效。', 'INVALID_REQUEST', 400) from None
        if type(expected_revision) is not int:
            raise WorkflowAPIError('删除需要当前项目版本。', 'INVALID_REQUEST', 400)
        root = Path(self.app.config['UPLOAD_FOLDER']).resolve()
        project_root = root / project_id
        managed_root = project_root / 'wuyu'

        def owned(relative):
            candidate = root / relative
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(managed_root.resolve()):
                raise WorkflowAPIError('材料文件不在当前项目目录，未执行删除。', 'MATERIAL_DELETE_PATH_DENIED', 409)
            parts = candidate.resolve().relative_to(managed_root.resolve()).parts
            if not parts or parts[0] not in {'materials', 'material_assets', 'template_previews', 'reading'}:
                raise WorkflowAPIError('文件属于生成成果，不能作为原材料删除。', 'MATERIAL_DELETE_PATH_DENIED', 409)
            for parent in candidate.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise WorkflowAPIError('材料目录不安全，未执行删除。', 'MATERIAL_DELETE_PATH_DENIED', 409)
            return candidate

        def begin(body):
            records = body.setdefault('material_deletions', {})
            if any(v['request_id'] == request_id and ident != material_id for ident, v in records.items()):
                raise WorkflowAPIError('删除请求标识已用于另一份材料。', 'IDEMPOTENCY_CONFLICT', 409)
            prior = records.get(material_id)
            if prior:
                if prior['request_id'] != request_id and prior['status'] == 'pending':
                    # A fresh click can resume the same permanent deletion.
                    prior['request_id'] = request_id
                return dict(prior)
            self._material_management_guard(body)
            material = next((m for m in body['materials'] if m['id'] == material_id), None)
            if material is None:
                raise WorkflowAPIError('材料不在当前项目中。', 'MATERIAL_NOT_FOUND', 404)
            if material.get('origin') == 'web_research':
                raise WorkflowAPIError('此入口仅删除用户上传的材料。', 'MATERIAL_DELETE_FORBIDDEN', 403)
            if WuyuWorkflowJob.query.filter_by(project_id=project_id).filter(
                    WuyuWorkflowJob.status.in_(['queued', 'running'])).first():
                raise WorkflowAPIError('请先停止本项目的处理任务，待任务停止后再删除材料。', 'MATERIAL_IN_USE', 409)
            paths = {str((managed_root / 'reading').relative_to(root)),
                     str((managed_root / 'material_assets' / material_id).relative_to(root)),
                     str((managed_root / 'template_previews' / material_id).relative_to(root))}
            if material.get('relative_path'):
                paths.add(material['relative_path'])
            ref = db.session.get(ReferenceFile, material_id)
            if ref:
                if ref.project_id != project_id or ref.user_id not in (None, user_id):
                    raise WorkflowAPIError('材料归属不一致，未执行删除。', 'MATERIAL_DELETE_FORBIDDEN', 403)
                paths.add(ref.file_path)
            removed_artifacts = []
            for artifact in body['artifacts']:
                metadata = artifact.get('metadata') or {}
                if metadata.get('material_id') == material_id or artifact['kind'].startswith('reading_'):
                    paths.add(artifact['relative_path'])
                    removed_artifacts.append(artifact['id'])
            # Validate the complete set before changing any state or file.
            for path in paths:
                owned(path)
            body['materials'] = [m for m in body['materials'] if m['id'] != material_id]
            body['artifacts'] = [a for a in body['artifacts'] if a['id'] not in removed_artifacts]
            body.get('reading_exclusions', {}).pop(material_id, None)
            for asset in material.get('source_assets', []):
                body.get('reading_asset_exclusions', {}).pop(asset['id'], None)
            recipe = body.get('reference_recipe') or {}
            if recipe.get('style_anchor') == 'material:' + material_id or (recipe.get('selection') or {}).get('material_id') == material_id:
                body['reference_recipe'] = {'style_anchor': None, 'references': []}
                body.pop('template_choice', None)
            else:
                recipe['references'] = [r for r in recipe.get('references', []) if r != 'material:' + material_id]
            self._reading_invalidate(body)
            body['outline'], body['slide_specs'], body['planning_report'] = [], [], {}
            body['workflow'] = self._new_state(project_id, body['brief'], body['workflow']['review_policy'])
            body['epoch'] += 1
            body['paused'] = False
            body.pop('resume_action', None)
            if ref:
                db.session.delete(ref)
            record = {'status': 'pending', 'request_id': request_id, 'paths': sorted(paths), 'started_at': datetime.utcnow().isoformat() + 'Z'}
            records[material_id] = record
            body['history'].append({'at': record['started_at'], 'action': 'material_delete_requested', 'material_id': material_id})
            return dict(record)

        revision, before = self._read(project_id)
        known = before.get('material_deletions', {}).get(material_id)
        if known and known['status'] == 'completed':
            return dict(self.snapshot(project_id), deleted_material_id=material_id, already_deleted=True)
        record, _ = self._mutate(project_id, begin, expected_revision=expected_revision)
        try:
            for relative in record.get('paths', []):
                path = owned(relative)
                if path.is_dir():
                    # owned() verifies absolute project containment and symlinks.
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        except OSError:
            raise WorkflowAPIError('材料已停止使用，但文件清理尚未完成；请重试删除。', 'MATERIAL_DELETE_PENDING', 503) from None

        def finish(body):
            entry = body['material_deletions'][material_id]
            entry.pop('paths', None)
            entry.update(status='completed', deleted_at=datetime.utcnow().isoformat() + 'Z')
            # Tombstones contain no source text or filename, and bound replay state.
            records = body['material_deletions']
            for ident in list(records)[:-200]:
                if records[ident]['status'] == 'completed':
                    del records[ident]
        self._mutate(project_id, finish)
        return dict(self.snapshot(project_id), deleted_material_id=material_id)
