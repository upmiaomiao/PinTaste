"""Use a PPT the owner already uploaded as content material as their template (op204).

``use_material_as_template`` feeds the stored bytes of an uploaded .pptx/.pdf back through the
ordinary template upload (``add_materials(role='template')``), so the same quota, capacity, reading
guard, automatic selection and representative-page rendering apply as for "上传自己的模板". The new
template material records ``source_material_id``; asking again for the same material returns the
existing template instead of copying it twice.

``set_template_pages`` lets the owner drop some of the automatically chosen representative pages
before (or after) confirming the template. Removed pages are kept aside and can be restored. At
least two pages stay (one when the template only ever had one), so a template never becomes empty.
"""
from pathlib import Path

from .wuyu_workflow_contracts import WorkflowAPIError

TEMPLATE_SOURCE_EXTENSIONS = {'.pptx', '.pdf'}
MIN_PAGES = 2


class _StoredUpload:
    """The minimal FileStorage surface add_materials reads: filename and a binary stream."""

    def __init__(self, filename, stream):
        self.filename, self.stream = filename, stream


def _material(body, material_id):
    material = next((m for m in body['materials'] if m.get('id') == material_id), None)
    if material is None:
        raise WorkflowAPIError('材料不存在。', 'MATERIAL_NOT_FOUND', 404)
    return material


def template_candidates(body):
    """Content PPT/PDF materials that can become a template, with any template already made from each."""
    made = {m.get('source_material_id'): m['id'] for m in body['materials']
            if m.get('role') == 'template' and m.get('source_material_id')}
    return [{'material_id': m['id'], 'filename': m.get('filename', ''), 'template_material_id': made.get(m['id'])}
            for m in body['materials']
            if m.get('role') == 'material' and m.get('origin') != 'web_research'
            and Path(m.get('filename', '')).suffix.lower() in TEMPLATE_SOURCE_EXTENSIONS and m.get('relative_path')]


def use_material_as_template(service, project_id, material_id, user_id):
    _, body = service._read(project_id)
    source = _material(body, material_id)
    if source.get('role') != 'material' or Path(source.get('filename', '')).suffix.lower() not in TEMPLATE_SOURCE_EXTENSIONS:
        raise WorkflowAPIError('只有上传的 PPTX 或 PDF 资料可以用作模板。', 'TEMPLATE_SOURCE_INVALID', 400)
    existing = next((m for m in body['materials'] if m.get('role') == 'template'
                     and m.get('source_material_id') == material_id), None)
    if existing is not None:
        return dict(service.snapshot(project_id), uploaded_material_ids=[existing['id']], template_reused=True)
    root = Path(service.app.config['UPLOAD_FOLDER']).resolve()
    path = (root / source['relative_path']).resolve()
    if not path.is_relative_to(root / project_id) or not path.is_file():
        raise WorkflowAPIError('原始资料文件不可用，请重新上传。', 'TEMPLATE_SOURCE_UNAVAILABLE', 409)
    with path.open('rb') as stream:
        result = service.add_materials(project_id, [_StoredUpload(source['filename'], stream)], None,
                                       user_id, role='template')
    new_id = result['uploaded_material_ids'][-1]

    def link(body):
        _material(body, new_id)['source_material_id'] = material_id
    service._mutate(project_id, link)
    return dict(service.snapshot(project_id), uploaded_material_ids=[new_id], template_reused=False)


def set_template_pages(service, project_id, material_id, remove=(), restore=()):
    """Remove and/or restore representative page numbers of one uploaded template."""
    if any(type(n) is not int for n in (*remove, *restore)) or len(remove) + len(restore) > 48:
        raise WorkflowAPIError('页码无效。', 'INVALID_REQUEST', 400)
    from .wuyu_reading_workflow import template_reading_guard

    def apply(body):
        service._material_management_guard(body)
        template_reading_guard(body)
        material = _material(body, material_id)
        if material.get('role') != 'template' or material.get('status') != 'completed':
            raise WorkflowAPIError('模板还在解析，请稍后再调整页面。', 'TEMPLATE_NOT_READY', 409)
        kept = list(material.get('template_images') or [])
        removed = list(material.get('template_images_removed') or [])
        original = len(kept) + len(removed)
        for number in restore:
            item = next((i for i in removed if i.get('number') == number), None)
            if item is not None:
                removed.remove(item); kept.append(item)
        for number in remove:
            item = next((i for i in kept if i.get('number') == number), None)
            if item is not None:
                kept.remove(item); removed.append(item)
        if len(kept) < min(MIN_PAGES, original):
            raise WorkflowAPIError('模板至少保留 %d 页。' % min(MIN_PAGES, original), 'TEMPLATE_PAGES_MINIMUM', 409)
        material['template_images'] = sorted(kept, key=lambda i: i.get('number', 0))
        if removed:
            material['template_images_removed'] = sorted(removed, key=lambda i: i.get('number', 0))
        else:
            material.pop('template_images_removed', None)
    service._mutate(project_id, apply)
    return service.snapshot(project_id)
