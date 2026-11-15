"""Explicit administrator file import, with no HTTP or model-call entry point."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
from PIL import Image
from .wuyu_template_library import TemplateLibraryError, _tags


def import_public_templates(library, manifest, asset_root):
    """Stage immutable, verified images before publishing one approved catalog revision."""
    root = Path(asset_root).resolve()
    entries = manifest.get('entries') if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise TemplateLibraryError('导入清单须包含1至100套模板。')
    prepared, identifiers = [], set()
    for entry in entries:
        record, files = deepcopy(entry['record']), entry['files']
        ident = record.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', ident) or ident in identifiers:
            raise TemplateLibraryError('导入模板标识重复或无效。')
        identifiers.add(ident)
        if not isinstance(record.get('title'), str) or not 1 <= len(record['title']) <= 200:
            raise TemplateLibraryError('导入模板标题无效。')
        if record.get('source_type') != 'local' or record.get('preview_kind') not in {'original_template_render', 'sanitized_original_render'}:
            raise TemplateLibraryError('仅导入已确认公开的本地图或现有系统模板。')
        record['tags'] = _tags(record['tags'])
        pages = record.get('pages')
        single = record.get('reference_scope') == 'single_image'
        if not isinstance(pages, list) or len(pages) != (1 if single else 5) or len(files) != len(pages):
            raise TemplateLibraryError('单图须有1张真实预览，整套须有5张真实正文参考。')
        checked, paths = [], set()
        for file, page in zip(files, pages):
            source = root / file['source']
            target = library.root / file['target']
            if (not source.resolve().is_relative_to(root) or source.is_symlink()
                    or any(p.is_symlink() for p in source.parents if p.is_relative_to(root))
                    or not target.resolve().is_relative_to(library.root.resolve())
                    or not re.fullmatch(r'original-previews/[a-f0-9]{64}\.png', file['target'])
                    or page.get('image') != file['target'] or file['target'] in paths):
                raise TemplateLibraryError('导入模板图片路径无效。')
            paths.add(file['target'])
            if not source.is_file() or source.stat().st_size > 30 * 1024**2:
                raise TemplateLibraryError('导入模板图片超过大小限制。')
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != file['sha256'] or digest != Path(file['target']).stem:
                raise TemplateLibraryError('导入模板图片校验失败。')
            with Image.open(source) as image:
                if min(image.size) < 16 or image.width * image.height > 32_000_000:
                    raise TemplateLibraryError('导入模板分辨率无效。')
                image.verify()
            if not page.get('selected') or not page.get('is_body_reference'):
                raise TemplateLibraryError('导入页必须是实际选用的正文参考。')
            checked.append((data, target, digest))
        fingerprint = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        record.update(owner_user_id=None, origin='admin_public_import75', review_status='approved',
                      tagging_status='completed', tag_source='administrator_category_mapping',
                      body_reference_count=len(pages), page_count=len(pages), reference_sufficient=not single,
                      import_fingerprint=fingerprint, renderer_version=5,
                      created_at=datetime.now(timezone.utc).isoformat(), updated_at=datetime.now(timezone.utc).isoformat())
        prepared.append((record, checked))
    library.prepare()
    with library._state() as state:
        for record, _ in prepared:
            old = state['sources'].get(record['id'])
            if old and old.get('import_fingerprint') != record['import_fingerprint']:
                raise TemplateLibraryError('现有模板标识发生冲突，未覆盖原模板。')
        added = 0
        for record, files in prepared:
            if record['id'] in state['sources']:
                continue
            for data, target, digest in files:
                if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                    raise TemplateLibraryError('导入目标路径无效。')
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        raise TemplateLibraryError('导入目标文件校验失败。')
                else:
                    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.pending75')
                    temporary.write_bytes(data)
                    temporary.chmod(0o600)
                    temporary.replace(target)
            state['sources'][record['id']] = record
            added += 1
        state['audit'].append({'at': datetime.now(timezone.utc).isoformat(), 'action': 'user_authorized_public_import75',
                               'count': added, 'model_called': False})
        library._publish(state)
        return {'added': added, 'reused': len(entries)-added, 'total': len(state['sources']), 'model_calls': 0}
