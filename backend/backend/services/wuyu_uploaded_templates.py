"""Account-private projections of existing uploads, with explicit independent reuse."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import uuid

from flask import after_this_request, has_request_context
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from models import db, Project, ReferenceFile
from .wuyu_template_library import AXES, TemplateLibraryError


class UploadedTemplates:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.config['UPLOAD_FOLDER']).resolve()

    def _rows(self, owner_id, ident=None):
        # Extract metadata in SQLite: unrelated source text/chunks never enter the library response.
        query = """SELECT p.id AS project_id, r.file_path, r.file_size,
            json_object('id',json_extract(m.value,'$.id'),'filename',r.filename,
                'relative_path',json_extract(m.value,'$.relative_path'),
                'sha256',json_extract(m.value,'$.sha256'), 'bytes',r.file_size,
                'template_images',json_extract(m.value,'$.template_images'),
                'reused_from',json_extract(m.value,'$.reused_from')) AS metadata
            FROM wuyu_workflows w JOIN projects p ON p.id=w.project_id
            JOIN json_each(w.body,'$.materials') m
            JOIN reference_files r ON r.id=json_extract(m.value,'$.id') AND r.project_id=p.id
            WHERE p.user_id=:owner AND r.user_id=:owner
                AND json_extract(m.value,'$.role')='template'
                AND json_extract(m.value,'$.status')='completed' AND r.parse_status='completed'
                AND json_array_length(json_extract(m.value,'$.template_images'))>0
        """
        dialect = db.engine.dialect.name
        if dialect == 'postgresql':
            query = """SELECT p.id AS project_id, r.file_path, r.file_size,
                jsonb_build_object('id',m.value->'id','filename',r.filename,
                    'relative_path',m.value->'relative_path',
                    'sha256',m.value->'sha256','bytes',r.file_size,
                    'template_images',m.value->'template_images',
                    'reused_from',m.value->'reused_from') AS metadata
                FROM wuyu_workflows w JOIN projects p ON p.id=w.project_id
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN jsonb_typeof(CAST(w.body AS jsonb)->'materials')='array'
                    THEN CAST(w.body AS jsonb)->'materials' ELSE '[]'::jsonb END) m(value)
                JOIN reference_files r ON r.id=m.value->>'id' AND r.project_id=p.id
                WHERE p.user_id=:owner AND r.user_id=:owner
                    AND m.value->>'role'='template'
                    AND m.value->>'status'='completed' AND r.parse_status='completed'
                    AND CASE WHEN jsonb_typeof(m.value->'template_images')='array'
                        THEN jsonb_array_length(m.value->'template_images') ELSE 0 END>0
            """
        elif dialect != 'sqlite':
            raise TemplateLibraryError('私人模板存储暂不可用。', 'TEMPLATE_STORAGE_UNAVAILABLE', 503)
        params = {'owner': owner_id}
        if ident is not None:
            if not isinstance(ident, str) or not re.fullmatch(r'uploaded:[0-9a-f-]{36}', ident):
                raise TemplateLibraryError('私人模板不存在。', 'TEMPLATE_NOT_FOUND', 404)
            query += ' AND r.id=:id'
            params['id'] = ident[9:]
        query += (" ORDER BY (m.value->>'reused_from' IS NOT NULL), r.created_at DESC, r.id"
                  if dialect == 'postgresql' else
                  " ORDER BY (json_extract(m.value,'$.reused_from') IS NOT NULL), r.created_at DESC, r.id")
        try:
            for row in db.session.execute(text(query), params).mappings():
                material = row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])
                yield row['project_id'], row['file_path'], material
        except SQLAlchemyError:
            if dialect != 'postgresql':
                raise
            raise TemplateLibraryError('私人模板存储暂不可用。', 'TEMPLATE_STORAGE_UNAVAILABLE', 503) from None

    def _file(self, project_id, relative, limit):
        if not isinstance(relative, str):
            raise TemplateLibraryError('私人模板原文件不可用。', 'TEMPLATE_SOURCE_CHANGED', 409)
        path = self.root / relative
        allowed = self.root / project_id / 'wuyu'
        if not path.resolve().is_relative_to(allowed) or any(p.is_symlink() for p in [path, *path.parents] if p != self.root):
            raise TemplateLibraryError('私人模板文件路径无效。', 'TEMPLATE_SOURCE_CHANGED', 409)
        if not path.is_file() or not 0 < path.stat().st_size <= limit:
            raise TemplateLibraryError('私人模板文件已删除或不可用。', 'TEMPLATE_SOURCE_CHANGED', 409)
        return path

    def _verify(self, project_id, reference_path, material, digest=False):
        if material['relative_path'] != reference_path or not re.fullmatch('[0-9a-f]{64}', material.get('sha256') or ''):
            raise TemplateLibraryError('私人模板原文件已变化。', 'TEMPLATE_SOURCE_CHANGED', 409)
        original = self._file(project_id, reference_path, 50 * 1024**2)
        if original.stat().st_size != material['bytes']:
            raise TemplateLibraryError('私人模板原文件已变化。', 'TEMPLATE_SOURCE_CHANGED', 409)
        if digest:
            self._hash(original, material['sha256'])
        images = material.get('template_images')
        if not isinstance(images, list) or not 1 <= len(images) <= 24:
            raise TemplateLibraryError('私人模板没有可用预览。', 'TEMPLATE_SOURCE_CHANGED', 409)
        for image in images[:12]:
            path = self._file(project_id, image.get('relative_path'), 32 * 1024**2)
            if digest:
                self._hash(path, image.get('sha256'))
        return original

    @staticmethod
    def _hash(path, expected):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024**2):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise TemplateLibraryError('私人模板文件校验失败，请重新解析。', 'TEMPLATE_SOURCE_CHANGED', 409)

    def _record(self, owner_id, ident):
        row = next(self._rows(owner_id, ident), None)
        if row is None:
            raise TemplateLibraryError('私人模板不存在。', 'TEMPLATE_NOT_FOUND', 404)
        self._verify(*row)
        return row

    def _card(self, material, detail=True):
        ident = 'uploaded:' + material['id']
        pages = [{'id': ident + ':p' + str(n), 'page_number': n,
                  'title': '模板预览 ' + str(n), 'layout_family': 'overview',
                  'image_url': f'/api/template-library/{ident}/previews/{n}.png'}
                 for n, _ in enumerate(material['template_images'][:12], 1)]
        return {'id': ident, 'title': material['filename'][:200], 'source_type': 'user',
                'review_status': 'approved', 'neutralized': False, 'preview_kind': 'private_uploaded_template',
                'preview_disclaimer': '仅自己可见的上传模板；用于其他项目时会创建独立副本。原内容仅作视觉参考。',
                'cover_url': pages[0]['image_url'], 'page_count': len(pages),
                'body_reference_count': max(0, len(pages)-1), 'reference_sufficient': True,
                'tags': {axis: [] for axis in AXES}, 'warnings': [], 'tagging_status': 'not_requested',
                'tag_source': 'private_upload', **({'pages': pages} if detail else {})}

    def list(self, owner_id, detail=False):
        return [card for _, card in self.keyed(owner_id, detail)]

    def keyed(self, owner_id, detail=False):
        """(template key, card) pairs; the key is the identity template_key() records (op195)."""
        cards, seen = [], set()
        for row in self._rows(owner_id):
            material = row[2]
            if material['sha256'] in seen:
                continue
            try:
                self._verify(*row)
            except TemplateLibraryError:
                continue
            seen.add(material['sha256'])
            cards.append(('upload:' + material['sha256'], self._card(material, detail)))
        return cards

    def detail(self, owner_id, ident):
        return self._card(self._record(owner_id, ident)[2])

    def preview(self, owner_id, ident, number):
        project_id, _, material = self._record(owner_id, ident)
        if type(number) is not int or not 1 <= number <= min(12, len(material['template_images'])):
            raise TemplateLibraryError('私人模板预览不存在。', 'TEMPLATE_NOT_FOUND', 404)
        image = material['template_images'][number-1]
        path = self._file(project_id, image['relative_path'], 32 * 1024**2)
        self._hash(path, image.get('sha256'))
        return path

    def attach(self, owner_id, ident, body):
        workflow = self.app.extensions['wuyu_workflow']
        target_project = body['workflow']['project_id']
        project = db.session.get(Project, target_project)
        if project is None or project.user_id != owner_id:
            raise TemplateLibraryError('项目不存在。', 'TEMPLATE_NOT_FOUND', 404)
        source_project, reference_path, source = self._record(owner_id, ident)
        self._verify(source_project, reference_path, source, digest=True)
        for item in body['materials']:
            if item['id'] == source['id'] or item.get('reused_from') == {'id': source['id'], 'sha256': source['sha256']}:
                return item
        workflow._material_management_guard(body)
        ident = str(uuid.uuid4())
        relative = Path(target_project) / 'wuyu'
        original_rel = relative / 'materials' / (ident + Path(reference_path).suffix)
        preview_rel = relative / 'template_previews' / ident
        material = {key: deepcopy(source[key]) for key in ('filename', 'sha256', 'bytes')}
        material.update(id=ident, relative_path=original_rel.as_posix(), role='template', status='completed',
                        text='', chunks=[], warnings=[], source_assets=[], template_images=[],
                        template_render_status='available', reused_from={'id': source['id'], 'sha256': source['sha256']})
        for n, image in enumerate(source['template_images'][:12], 1):
            material['template_images'].append({**image, 'relative_path': (preview_rel / f'{n}.png').as_posix(), 'artifact_id': str(uuid.uuid4())})
        workflow._material_capacity_guard(body['materials'] + [material])
        if not has_request_context():
            raise TemplateLibraryError('请通过模板确认界面复用私人模板。', 'USER_INTERACTION_REQUIRED', 403)
        created = []

        @after_this_request
        def cleanup(response):
            # Check committed state on a fresh connection, independent of a failed/rolled-back ORM transaction.
            with db.engine.connect() as connection:
                retained = connection.execute(text('SELECT id FROM reference_files WHERE id=:id AND project_id=:project'),
                                              {'id': ident, 'project': target_project}).first()
            if retained is None:
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                folder = self.root / preview_rel
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            return response

        def copy_checked(source_path, destination_relative, expected):
            target = self.root / destination_relative
            # New UUID destinations are exclusive; validate every parent before creating files.
            if not target.resolve().is_relative_to(self.root / target_project / 'wuyu') or any(p.is_symlink() for p in target.parents):
                raise TemplateLibraryError('目标模板路径无效。', 'TEMPLATE_SOURCE_CHANGED', 409)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source_path.open('rb') as reader, target.open('xb') as writer:
                created.append(target)
                while chunk := reader.read(1024**2):
                    digest.update(chunk)
                    writer.write(chunk)
            if digest.hexdigest() != expected:
                raise TemplateLibraryError('模板在复用时发生变化，请刷新重试。', 'TEMPLATE_SOURCE_CHANGED', 409)

        copy_checked(self.root / reference_path, original_rel, source['sha256'])
        from .wuyu_workflow_service import _now
        for source_image, image in zip(source['template_images'], material['template_images']):
            copy_checked(self.root / source_image['relative_path'], image['relative_path'], image['sha256'])
            path = self.root / image['relative_path']
            body['artifacts'].append({'id': image['artifact_id'], 'kind': 'template_image',
                'relative_path': image['relative_path'], 'filename': path.name, 'bytes': path.stat().st_size,
                'sha256': image['sha256'], 'mime_type': 'image/png', 'metadata': {'number': image['number'], 'role': 'template'}, 'created_at': _now()})
        db.session.add(ReferenceFile(id=ident, project_id=target_project, user_id=owner_id,
            filename=material['filename'], file_path=material['relative_path'], file_size=material['bytes'],
            file_type=Path(reference_path).suffix.lstrip('.'), parse_status='completed'))
        body['materials'].append(material)
        return material
