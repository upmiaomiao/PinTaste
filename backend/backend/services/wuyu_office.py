"""Cloud editing of the editable PPTX through ONLYOFFICE Docs.

An editable (traced, exported) deck can be edited; when the full conversion
did not finish, the current mixed ("hybrid") delivery can be edited instead
(pages whose conversion failed are pictures there). A save in the editor
becomes a new project version (``manual_edit``); slides whose content changed
are locked with source ``manual_edit`` so later AI regeneration never replaces
them. When unlocked pages are regenerated and re-exported, the new slides are
merged into the manually edited file instead of discarding the edits.

No provider/model call happens here. Network access is limited to the
configured internal ONLYOFFICE Document Server for downloading a saved file.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import jwt

FILE_AUDIENCE = 'wuyu-office-file'
CALLBACK_AUDIENCE = 'wuyu-office-callback'
TOKEN_SECONDS = 24 * 3600
MAX_SAVED_BYTES = 100 * 1024 * 1024
SAVE_STATUSES = {2, 6}          # 2 = closed with changes, 6 = force save
NOOP_STATUSES = {0, 1, 4}       # editing / closed without changes
ERROR_STATUSES = {3, 7}
_RID = re.compile(r'^rId\d+$')
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class OfficeError(Exception):
    def __init__(self, message, code='OFFICE_UNAVAILABLE', status=409):
        super().__init__(message)
        self.code, self.status = code, status


# ---------------------------------------------------------------- settings
def settings(config):
    enabled = str(config.get('WUYU_OFFICE_ENABLED', '')).lower() in {'1', 'true', 'yes'}
    values = {
        'enabled': enabled,
        'public_url': str(config.get('WUYU_OFFICE_PUBLIC_URL', '')).rstrip('/'),
        'internal_url': str(config.get('WUYU_OFFICE_INTERNAL_URL', '')).rstrip('/'),
        'backend_internal_url': str(config.get('WUYU_OFFICE_BACKEND_INTERNAL_URL', '')).rstrip('/'),
        'jwt_secret': str(config.get('WUYU_OFFICE_JWT_SECRET', '')),
        'bridge_secret': str(config.get('WUYU_OFFICE_BRIDGE_SECRET', '')),
    }
    if enabled and (not all(values[k] for k in ('public_url', 'internal_url', 'backend_internal_url'))
                    or len(values['jwt_secret']) < 32 or len(values['bridge_secret']) < 32
                    or values['jwt_secret'] == values['bridge_secret']):
        raise OfficeError('在线编辑配置不完整，请管理员检查。', 'OFFICE_SETTINGS_INVALID', 503)
    return values


# ---------------------------------------------------------------- sources
def _artifact(body, uri_or_id):
    ident = str(uri_or_id or '').removeprefix('artifact:').removesuffix('.pptx')
    return next((a for a in body.get('artifacts', []) if a['id'] == ident), None)


def editable_source(body, phase, hybrid_id=None):
    """(artifact, kind) for the file the editor should open, or raise OfficeError.

    ``hybrid_id`` is the id of the current mixed delivery (``hybrid_pptx`` whose
    signature matches the project now), used only when no full editable export
    exists.
    """
    state = body['workflow']
    export = state.get('export') or {}
    manual = body.get('manual_edit') or {}
    full = phase(state) == 'exported' and bool(export.get('pptx_uri'))
    if state.get('output_preference') == 'image' or not (full or hybrid_id):
        raise OfficeError('在线编辑仅支持已完成可编辑版（或混合可编辑版）转换的 PPT；图片版请先在对话中申请转换为可编辑版。',
                          'OFFICE_EDITABLE_REQUIRED', 409)
    if not full:
        if manual.get('base') == 'hybrid' and manual.get('hybrid_artifact_id') == hybrid_id:
            artifact = _artifact(body, manual.get('artifact_id'))
            if artifact:
                return artifact, 'manual_edit'
        artifact = _artifact(body, hybrid_id)
        if not artifact:
            raise OfficeError('混合可编辑版文件记录缺失，请重新导出。', 'OFFICE_SOURCE_MISSING', 409)
        return artifact, 'hybrid_export'
    if manual.get('export_run_id') and manual.get('export_run_id') == export.get('run_id'):
        artifact = _artifact(body, manual.get('artifact_id'))
        if artifact:
            return artifact, 'manual_edit'
    artifact = _artifact(body, export['pptx_uri'])
    if not artifact:
        raise OfficeError('可编辑版文件记录缺失，请重新导出。', 'OFFICE_SOURCE_MISSING', 409)
    return artifact, 'editable_export'


def status(body, phase, config, hybrid_id=None):
    try:
        office = settings(config)
    except OfficeError as error:
        return {'available': False, 'reason': str(error), 'code': error.code}
    if not office['enabled']:
        return {'available': False, 'reason': '在线编辑尚未开启。', 'code': 'OFFICE_DISABLED'}
    try:
        artifact, kind = editable_source(body, phase, hybrid_id)
    except OfficeError as error:
        return {'available': False, 'reason': str(error), 'code': error.code}
    manual = body.get('manual_edit') or {}
    return {'available': True, 'source_kind': kind, 'artifact_id': artifact['id'],
            'hybrid': kind == 'hybrid_export' or (kind == 'manual_edit' and manual.get('base') == 'hybrid'),
            'manual_version': manual.get('version', 0) if kind == 'manual_edit' else 0,
            'edited_pages': manual.get('edited_pages', []) if kind == 'manual_edit' else []}


# ---------------------------------------------------------------- tokens
def bridge_token(config, audience, project_id, artifact_id, document_key, user_id=None):
    office = settings(config)
    now = int(time.time())
    payload = {'aud': audience, 'pid': project_id, 'aid': artifact_id, 'key': document_key,
               'iat': now, 'exp': now + TOKEN_SECONDS}
    if user_id is not None:
        payload['uid'] = str(user_id)
    return jwt.encode(payload, office['bridge_secret'], algorithm='HS256')


def read_bridge_token(config, token, audience):
    try:
        return jwt.decode(token, settings(config)['bridge_secret'], algorithms=['HS256'], audience=audience)
    except jwt.InvalidTokenError:
        raise OfficeError('Invalid office bridge token', 'OFFICE_TOKEN_INVALID', 403) from None


def document_key(project_id, artifact):
    return hashlib.sha256(f"{project_id}:{artifact['id']}:{artifact.get('sha256', '')}".encode()).hexdigest()[:40]


def editor_config(config, project_id, artifact, title, user, lang='zh-CN'):
    """ONLYOFFICE DocEditor config; the signed token covers the whole object."""
    office = settings(config)
    if not office['enabled']:
        raise OfficeError('在线编辑尚未开启。', 'OFFICE_DISABLED', 503)
    key = document_key(project_id, artifact)
    user_id = str(getattr(user, 'id', ''))
    file_token = bridge_token(config, FILE_AUDIENCE, project_id, artifact['id'], key)
    callback_token = bridge_token(config, CALLBACK_AUDIENCE, project_id, artifact['id'], key, user_id)
    base = office['backend_internal_url']
    data = {
        'documentType': 'slide',
        'document': {'fileType': 'pptx', 'key': key, 'title': title if title.lower().endswith('.pptx') else title + '.pptx',
                     'url': f'{base}/office-bridge/file/{file_token}',
                     'permissions': {'edit': True, 'download': True, 'print': True, 'review': False, 'comment': False}},
        'editorConfig': {'mode': 'edit', 'lang': lang, 'callbackUrl': f'{base}/office-bridge/callback/{callback_token}',
                         'user': {'id': user_id, 'name': str(getattr(user, 'username', None) or getattr(user, 'email', None) or user_id)},
                         'customization': {'forcesave': True, 'autosave': True, 'compactHeader': False,
                                           'goback': False, 'chat': False, 'comments': False}},
    }
    data['token'] = jwt.encode(deepcopy(data), office['jwt_secret'], algorithm='HS256')
    return {'document_server_url': office['public_url'], 'config': data}


def verify_callback(config, body, authorization=None):
    """Return the verified callback payload signed by the Document Server."""
    secret = settings(config)['jwt_secret']
    token = body.get('token') if isinstance(body, dict) else None
    if not token and authorization and authorization.startswith('Bearer '):
        token = authorization[7:].strip()
    if not token:
        raise OfficeError('Unsigned office callback', 'OFFICE_CALLBACK_UNSIGNED', 403)
    try:
        payload = jwt.decode(token, secret, algorithms=['HS256'])
    except jwt.InvalidTokenError:
        raise OfficeError('Invalid office callback signature', 'OFFICE_CALLBACK_UNSIGNED', 403) from None
    # Header tokens wrap the body in "payload"; body tokens are the body itself.
    return payload.get('payload', payload) if isinstance(payload.get('payload'), dict) else payload


def internal_download_url(config, url):
    """Only the configured Document Server may supply a saved file."""
    office = settings(config)
    parts = urlsplit(str(url or ''))
    allowed = {urlsplit(office['internal_url']).netloc: office['internal_url'],
               urlsplit(office['public_url']).netloc: office['internal_url']}
    if parts.scheme not in {'http', 'https'} or parts.netloc not in allowed:
        raise OfficeError('Saved file URL is not from the configured document server', 'OFFICE_CALLBACK_URL', 400)
    public_path = urlsplit(office['public_url']).path.rstrip('/')
    path = parts.path
    if parts.netloc == urlsplit(office['public_url']).netloc and public_path and path.startswith(public_path + '/'):
        path = path[len(public_path):]
    return allowed[parts.netloc].rstrip('/') + path + (('?' + parts.query) if parts.query else '')


def download_saved(config, url, opener=None):
    import urllib.request
    target = internal_download_url(config, url)
    opener = opener or urllib.request.urlopen
    with opener(target, timeout=60) as response:
        data = response.read(MAX_SAVED_BYTES + 1)
    if len(data) > MAX_SAVED_BYTES:
        raise OfficeError('Saved file is too large', 'OFFICE_SAVED_TOO_LARGE', 413)
    return data


# ---------------------------------------------------------------- slide analysis
def _shape_fingerprint(shape):
    parts = [shape.shape_type and int(shape.shape_type) or 0,
             round((shape.left or 0) / 2000), round((shape.top or 0) / 2000),
             round((shape.width or 0) / 2000), round((shape.height or 0) / 2000), round(shape.rotation or 0)]
    if shape.has_text_frame:
        parts.append(shape.text_frame.text)
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                # ONLYOFFICE re-saves sizes in 0.5pt steps (32.71 -> 32.5); that is serialization noise, not an
                # edit, and would otherwise lock every page that carries a fractional size. A real change moves
                # the size by at least a whole step, so quantising to the same step keeps it detectable.
                size = round(run.font.size.pt * 2) / 2 if run.font.size is not None else None
                color = None
                try:
                    color = str(run.font.color.rgb) if run.font.color and run.font.color.type is not None else None
                except AttributeError:
                    color = None
                parts.append((size, run.font.bold, run.font.italic, color))
    if getattr(shape, 'has_table', False) and shape.has_table:
        parts.append([[cell.text for cell in row.cells] for row in shape.table.rows])
    if getattr(shape, 'has_chart', False) and shape.has_chart:
        chart = shape.chart
        parts.append([[str(c) for c in plot.categories] for plot in chart.plots])
        parts.append([list(s.values) for s in chart.series])
    if shape.shape_type == 13:
        parts.append(hashlib.sha256(shape.image.blob).hexdigest())
    if shape.shape_type == 6:
        parts.append([_shape_fingerprint(child) for child in shape.shapes])
    return parts


def slide_fingerprints(data_or_path):
    """Content-level digest per slide; independent of how the editor re-serializes XML."""
    from pptx import Presentation
    source = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else str(data_or_path)
    prs = Presentation(source)
    result = []
    for slide in prs.slides:
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ''
        digest = repr(([_shape_fingerprint(shape) for shape in slide.shapes], notes)).encode('utf-8')
        result.append(hashlib.sha256(digest).hexdigest())
    return result


def edited_pages(before, after):
    if len(before) != len(after):
        return None
    return [index + 1 for index, (old, new) in enumerate(zip(before, after)) if old != new]


# ---------------------------------------------------------------- merging
def _partname_template(partname):
    return re.sub(r'\d*(\.\w+)$', r'%d\1', str(partname))


def _remap_rids(element, mapping):
    for node in element.iter():
        for name, value in list(node.attrib.items()):
            if name.startswith('{%s}' % R_NS) and value in mapping:
                node.set(name, mapping[value])


def _copy_part(source_part, package, cache):
    """Copy a part and its internal relationship tree into another package."""
    from pptx.opc.package import PartFactory, XmlPart
    from pptx.opc.packuri import PackURI
    from pptx.oxml import parse_xml
    from lxml import etree
    if id(source_part) in cache:
        return cache[id(source_part)]
    partname = package.next_partname(_partname_template(source_part.partname))
    blob = source_part.blob
    mapping, children = {}, []
    for rId, rel in source_part.rels.items():
        if rel.is_external:
            children.append((rId, rel.reltype, rel.target_ref, True))
        else:
            children.append((rId, rel.reltype, rel.target_part, False))
    new_part = PartFactory(PackURI(partname), source_part.content_type, package, blob)
    cache[id(source_part)] = new_part
    for rId, reltype, target, external in children:
        if external:
            mapping[rId] = new_part.rels.get_or_add_ext_rel(reltype, target)
        else:
            mapping[rId] = new_part.rels.get_or_add(reltype, _copy_part(target, package, cache))
    if mapping and isinstance(new_part, XmlPart):
        _remap_rids(new_part._element, mapping)
    elif mapping and blob[:5] == b'<?xml':
        element = parse_xml(blob)
        _remap_rids(element, mapping)
        new_part._blob = etree.tostring(element, xml_declaration=True, encoding='UTF-8', standalone=True)
    return new_part


def replace_slides(target_path, source_path, numbers, output_path):
    """Replace target slides ``numbers`` (1-based) with the source deck's slides.

    The target keeps its layouts/masters; replaced slides keep the target
    layout relationship. Pictures, charts (with embedded workbooks), media and
    hyperlinks are copied with remapped relationship IDs; notes text follows.
    """
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml import parse_xml
    from lxml import etree
    target, source = Presentation(str(target_path)), Presentation(str(source_path))
    if len(target.slides) != len(source.slides):
        raise OfficeError('人工编辑稿与新导出稿页数不一致，不能逐页合并。', 'OFFICE_MERGE_PAGE_COUNT', 409)
    if (target.slide_width, target.slide_height) != (source.slide_width, source.slide_height):
        raise OfficeError('人工编辑稿与新导出稿画布尺寸不一致。', 'OFFICE_MERGE_CANVAS', 409)
    package, cache = target.part.package, {}
    for number in sorted(set(numbers)):
        if not 1 <= number <= len(target.slides):
            raise OfficeError('合并页码超出范围。', 'OFFICE_MERGE_PAGE', 400)
        src, dst = source.slides[number - 1], target.slides[number - 1]
        keep = {RT.SLIDE_LAYOUT, RT.NOTES_SLIDE}
        for rId in [rId for rId, rel in dst.part.rels.items() if rel.reltype not in keep]:
            dst.part.rels.pop(rId)
        mapping = {}
        for rId, rel in src.part.rels.items():
            if rel.reltype in keep:
                continue
            if rel.is_external:
                mapping[rId] = dst.part.rels.get_or_add_ext_rel(rel.reltype, rel.target_ref)
            else:
                mapping[rId] = dst.part.rels.get_or_add(rel.reltype, _copy_part(rel.target_part, package, cache))
        element = parse_xml(etree.tostring(src._element))
        _remap_rids(element, mapping)
        dst.part._element = element
        notes = src.notes_slide.notes_text_frame.text if src.has_notes_slide else ''
        if notes or dst.has_notes_slide:
            dst.notes_slide.notes_text_frame.text = notes
    target.save(str(output_path))
    return str(output_path)


def merge_manual_edits(body, artifact_path, fresh_path, current_image_versions, output_path):
    """Carry the current manual edit forward into a new editable export.

    Returns ``(path, report)``. Without a current manual edit the fresh export
    is returned unchanged and ``report`` is None. Pages whose image version
    changed since the manual save come from the fresh export; all others keep
    the manually edited slide.
    """
    manual = body.get('manual_edit') or {}
    artifact = _artifact(body, manual.get('artifact_id'))
    if not artifact or not isinstance(manual.get('image_versions'), dict):
        return fresh_path, None
    manual_path = artifact_path(artifact)
    if not Path(manual_path).is_file():
        return fresh_path, None
    changed = sorted(int(n) for n, version in current_image_versions.items()
                     if manual['image_versions'].get(n) != version)
    if manual.get('base') == 'hybrid':
        # A mixed delivery has picture-only pages. Only the pages the user
        # actually edited survive; every other page comes from the full export.
        edited = {int(n) for n in manual.get('edited_pages') or []}
        changed = sorted(int(n) for n in current_image_versions if int(n) not in edited or int(n) in changed)
    if len(changed) == len(current_image_versions):
        # Every slide was regenerated: nothing of the manual edit remains.
        return fresh_path, {'manual_version': manual.get('version', 0), 'superseded': True,
                            'replaced_pages': changed, 'kept_manual_pages': []}
    if changed:
        replace_slides(manual_path, fresh_path, changed, output_path)
    else:
        Path(output_path).write_bytes(Path(manual_path).read_bytes())
    kept = sorted(int(n) for n in current_image_versions if int(n) not in changed)
    return str(output_path), {'manual_version': manual.get('version', 0), 'replaced_pages': changed, 'kept_manual_pages': kept}


class OfficeMixin:
    """Persist a document saved in the editor as a new project version."""

    def _office_hybrid_id(self, project_id, body):
        finder = getattr(self, '_current_hybrid_export', None)
        hybrid = finder(project_id, body) if finder else None
        return hybrid.get('id') if isinstance(hybrid, dict) else None

    def office_source(self, project_id):
        from .wuyu_state import phase
        _, body = self._read(project_id)
        return body, editable_source(body, phase, self._office_hybrid_id(project_id, body))

    def office_status(self, project_id):
        from .wuyu_state import phase
        _, body = self._read(project_id)
        return status(body, phase, self.app.config, self._office_hybrid_id(project_id, body))

    def save_office_edit(self, project_id, claims, data, user_id=None):
        import uuid
        from .wuyu_state import apply_event, phase
        from .wuyu_workflow_contracts import _id, _now
        _, body = self._read(project_id)
        source = _artifact(body, claims.get('aid'))
        if not source:
            raise OfficeError('Edited source no longer exists', 'OFFICE_SOURCE_MISSING', 404)
        if hashlib.sha256(data).hexdigest() == source.get('sha256'):
            return {'saved': False, 'reason': 'unchanged'}
        try:
            after = slide_fingerprints(bytes(data))
        except Exception:
            raise OfficeError('Saved file is not a readable PPTX', 'OFFICE_SAVED_INVALID', 422) from None
        before = slide_fingerprints(self._artifact_file(body, 'artifact:' + source['id']))
        changed = edited_pages(before, after)
        path = self._path(project_id, 'manual_edits', str(uuid.uuid4()) + '.pptx')
        path.write_bytes(data)
        uri = self._record_artifact(project_id, path, 'pptx', None, {
            'format': 'editable_pptx', 'manual_edit': True, 'base_artifact_id': source['id'],
            'edited_pages': changed, 'structure_changed': changed is None, 'slide_count': len(after),
            'editor': 'onlyoffice', 'user_id': user_id})
        artifact_id = uri.removeprefix('artifact:')

        def op(current):
            state = current['workflow']
            export = state.get('export') or {}
            previous = current.get('manual_edit') or {}
            export_artifact = _artifact(current, export.get('pptx_uri'))
            full = phase(state) == 'exported' and bool(export.get('pptx_uri'))
            hybrid_id = None if full else self._office_hybrid_id(project_id, current)
            if full:
                base_current = bool((export_artifact and export_artifact['id'] == source['id'])
                    or (source['id'] == previous.get('artifact_id') and previous.get('export_run_id')
                        and previous.get('export_run_id') == export.get('run_id')))
            else:
                base_current = bool(hybrid_id and (source['id'] == hybrid_id or (
                    source['id'] == previous.get('artifact_id') and previous.get('base') == 'hybrid'
                    and previous.get('hybrid_artifact_id') == hybrid_id)))
            if not base_current:
                # The deck changed while the editor was open: keep the file but
                # never let it replace the newer project version silently.
                current.setdefault('manual_edit_orphans', []).append(
                    {'artifact_id': artifact_id, 'base_artifact_id': source['id'], 'saved_at': _now()})
                return False
            count = len(state['pages'])
            pages = list(range(1, count + 1)) if changed is None else [n for n in changed if n <= count]
            if full:
                carried = previous.get('edited_pages', []) if previous.get('export_run_id') and previous.get('export_run_id') == export.get('run_id') else []
            else:
                carried = previous.get('edited_pages', []) if previous.get('base') == 'hybrid' and previous.get('hybrid_artifact_id') == hybrid_id else []
            if previous:
                current.setdefault('manual_edit_history', []).append(deepcopy(previous))
                current['manual_edit_history'] = current['manual_edit_history'][-50:]
            current['manual_edit'] = {
                'artifact_id': artifact_id, 'export_run_id': export.get('run_id') if full else None,
                'base': 'export' if full else 'hybrid', **({} if full else {'hybrid_artifact_id': hybrid_id}),
                'image_versions': {str(p['number']): p['image_version'] for p in state['pages']},
                'version': int(previous.get('version', 0)) + 1, 'edited_pages': sorted(set(carried) | set(pages)),
                'structure_changed': changed is None or previous.get('structure_changed') is True,
                'slide_count': len(after), 'saved_at': _now(), 'user_id': user_id}
            to_lock = [n for n in pages if state['pages'][n - 1].get('locked') is not True]
            if to_lock:
                state = apply_event(state, {'event_id': _id(), 'type': 'pages.locked',
                    'payload': {'numbers': to_lock, 'locked': True, 'source': 'manual_edit'}})
            current['workflow'] = state
            current['history'].append({'at': _now(), 'action': 'manual_edit_saved', 'status': 'accepted'})
            return True

        applied, _ = self._mutate(project_id, op)
        self._capture_manual_edit(project_id, body, source, data, changed, artifact_id, user_id)
        return {'saved': True, 'current': applied, 'artifact_id': artifact_id, 'edited_pages': changed}

    def _capture_manual_edit(self, project_id, body, source, data, changed, artifact_id, user_id=None):
        """Record what the owner changed by hand as design events (one per changed page).

        Runs after the save is committed and is best effort: a missing or unavailable habit store, a diff
        problem or a database error is logged and swallowed, so saving an edit never fails because of it.
        Edits by anyone but the project owner (an administrator helping out) say nothing about the owner's
        habits and are not recorded.
        """
        try:
            extensions = getattr(self.app, 'extensions', None) or {}
            habits = extensions.get('wuyu_design_habits')
            if habits is None or getattr(habits, 'available', True) is False or changed == []:
                return
            memory = extensions.get('wuyu_design_memory')
            if memory is not None and not memory.policy()['settings']['capture_enabled']:
                return
            import json
            from models import db, Project
            project = db.session.get(Project, project_id)
            if project is None or not project.user_id or (user_id and user_id != project.user_id):
                return
            if habits.paused(project.user_id):
                return
            from .wuyu_edit_diff import diff_decks
            entries = diff_decks(self._artifact_file(body, 'artifact:' + source['id']), bytes(data), pages=changed)
            for entry in entries:
                page, changes = entry.get('page'), list(entry.get('changes') or [])
                payload = {'changes': changes, 'base_artifact_id': source['id'], 'artifact_id': artifact_id}
                # The event store caps a payload at 16000 characters, measured the way it stores them
                # (unescaped); drop trailing changes to stay inside it and say so.
                while changes and len(json.dumps(payload, ensure_ascii=False)) > 15000:
                    changes.pop()
                    payload['truncated'] = True
                if not changes:
                    continue
                try:
                    habits.record_event(owner_id=project.user_id, project_id=project_id, kind='manual_edit',
                                        payload=payload, page_number=page, commit=True,
                                        request_key='manual:%s:%s' % (artifact_id, 'structure' if page is None else page))
                except Exception:
                    db.session.rollback()
                    raise
        except Exception as exc:  # memory capture must never break the save
            import logging
            logger = getattr(self.app, 'logger', None) or logging.getLogger(__name__)
            logger.warning('Manual edit capture skipped: project=%s type=%s', project_id, type(exc).__name__)


# ---------------------------------------------------------------- editor page
def editor_page(project_id, api_base='/agent-api'):
    """Minimal full-page host for the ONLYOFFICE editor (same-origin config fetch)."""
    pid = re.sub(r'[^A-Za-z0-9_-]', '', project_id)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>无隅 AI · 在线编辑 PPT</title>
<style>html,body{{margin:0;height:100%;font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif}}
#bar{{height:40px;display:flex;align-items:center;gap:12px;padding:0 14px;border-bottom:1px solid #e3e6ea;font-size:14px;color:#334}}
#bar small{{color:#667}}#editor{{height:calc(100% - 41px)}}#msg{{padding:24px;color:#a33}}</style></head>
<body><div id="bar"><strong>在线编辑</strong><small>保存后自动成为项目新版本；改动过的页面会自动锁定，不会被 AI 重新生成覆盖。</small></div>
<div id="editor"><div id="placeholder"></div></div>
<script>
(async () => {{
  const fail = text => {{ document.getElementById('editor').innerHTML = '<p id="msg"></p>'; document.getElementById('msg').textContent = text; }};
  try {{
    const response = await fetch('{api_base}/api/projects/{pid}/workflow/office/config', {{ credentials: 'same-origin' }});
    const body = await response.json();
    if (!response.ok || !body.success) return fail((body.error && body.error.message) || '暂时无法打开在线编辑。');
    const data = body.data;
    await new Promise((resolve, reject) => {{ const s = document.createElement('script');
      s.src = data.document_server_url + '/web-apps/apps/api/documents/api.js'; s.onload = resolve; s.onerror = reject; document.head.appendChild(s); }});
    new DocsAPI.DocEditor('placeholder', Object.assign({{ width: '100%', height: '100%', type: 'desktop' }}, data.config));
  }} catch (e) {{ fail('在线编辑服务暂时不可用，请稍后重试。'); }}
}})();
</script></body></html>'''
