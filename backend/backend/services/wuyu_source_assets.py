"""Verified project figures: preserve original media, deterministic view and provenance."""
import hashlib
import io
import json
import math
from pathlib import Path
import re
from PIL import Image, ImageDraw
from .wuyu_errors import PipelineError, check_cancel
from .wuyu_materials import allowed_file


def svg_source_dimensions(path):
    """Accept passive, self-contained SVGs only; never resolve resources/fonts.

    The original file is embedded unchanged as an Office SVG. The approved page
    supplies the compatibility PNG, so no renderer, network access or invented
    scientific redraw is involved.
    """
    from lxml import etree
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise PipelineError('source_svg_unsupported', 'SVG超过2MB，无法安全嵌入。')
    try:
        # The original XML is retained, so processing instructions must be
        # checked before ElementTree discards them. Restrict SVGs to UTF-8 to
        # avoid an alternate encoding bypassing this passive-content check.
        text = raw.decode('utf-8-sig')
        if re.search(r'<!DOCTYPE|<!ENTITY|<\?(?!xml(?:\s|\?>))', text, re.IGNORECASE):
            raise ValueError()
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                                 remove_comments=True, remove_pis=True)
        root = etree.fromstring(raw, parser=parser)
        if root.tag != '{http://www.w3.org/2000/svg}svg':
            raise ValueError()
        for node in root.iter():
            name = node.tag.rsplit('}', 1)[-1]
            if name in ('script', 'foreignObject', 'iframe', 'audio', 'video', 'animate', 'animateMotion', 'animateTransform', 'set'):
                raise ValueError()
            for key, value in node.attrib.items():
                attribute = key.rsplit('}', 1)[-1].lower()
                if key == '{http://www.w3.org/XML/1998/namespace}base' or attribute.startswith('on') or (attribute in ('href', 'src') and not value.startswith('#')):
                    raise ValueError()
            styles = ' '.join([node.text or '' if name == 'style' else '', *node.attrib.values()])
            if '@' in styles or '\\' in styles:
                raise ValueError()
            for match in re.finditer(r'url\(\s*(.*?)\s*\)', styles, re.IGNORECASE):
                if not match.group(1).strip('\'\" ').startswith('#'):
                    raise ValueError()
        viewbox = [float(value) for value in re.split(r'[\s,]+', root.get('viewBox', '').strip()) if value]
        if len(viewbox) == 4:
            dimensions = viewbox[2:]
        else:
            dimensions = [float(re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(?:px)?', root.get(key, '')).group(1))
                          for key in ('width', 'height')]
        if any(not math.isfinite(value) or value <= 0 or value > 1e7 for value in dimensions):
            raise ValueError()
        return tuple(dimensions)
    except Exception:
        raise PipelineError('source_svg_unsupported', 'SVG须为有明确尺寸、无脚本和外部引用的静态矢量图。') from None


def is_svg_media(path):
    return Path(path).suffix.lower() == '.svg'


def attach_original_svg(picture, prepared):
    """Office SVG relationship with original bytes and a compatibility PNG."""
    if not prepared.get('vector_media_path'):
        return
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI
    from pptx.oxml.xmlchemy import OxmlElement
    from lxml import etree
    raw = Path(prepared['vector_media_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != prepared['vector_media_sha256']:
        raise PipelineError('source_asset_hash_mismatch', 'SVG原图在嵌入前发生变化。')
    package = picture.part.package
    name = PackURI('/ppt/media/' + prepared['vector_media_sha256'] + '.svg')
    part = next((item for item in package.iter_parts() if item.partname == name and item.blob == raw), None)
    if part is None:
        part = Part(name, 'image/svg+xml', package, raw)
    relationship = picture.part.relate_to(part, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image')
    blip = picture._element.blipFill.blip
    extension_list = blip.find('{http://schemas.openxmlformats.org/drawingml/2006/main}extLst')
    if extension_list is None:
        extension_list = OxmlElement('a:extLst'); blip.append(extension_list)
    extension = OxmlElement('a:ext'); extension.set('uri', '{96DAC541-7B7A-43D3-8B79-37D633B846F1}')
    vector = etree.Element('{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip',
                           nsmap={'asvg': 'http://schemas.microsoft.com/office/drawing/2016/SVG/main'})
    vector.set('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed', relationship)
    extension.append(vector); extension_list.append(extension)


def prepare_ppt_source_media(asset, output_dir):
    """Keep source bytes, optional original SVG and lossless WEBP derivatives.

    The two SHA fields deliberately differ for a conversion. Never describe a
    PNG derivation as the original WEBP bytes being embedded in PowerPoint.
    An SVG requires an approved-page PNG preview and a separate vector part.
    """
    path = Path(asset['path'])
    raw = path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    if source_sha != asset['sha256']:
        raise PipelineError('source_asset_hash_mismatch', '原始素材hash已变化，拒绝导出。')
    if is_svg_media(path):
        dimensions = svg_source_dimensions(path)
        fallback = Path(asset.get('raster_fallback_path') or '')
        if not fallback.is_file():
            raise PipelineError('source_svg_preview_required', 'SVG需要已确认页面的PNG兼容预览；原始矢量文件仍保留。')
        with Image.open(fallback) as preview:
            if preview.format != 'PNG':
                raise PipelineError('source_svg_preview_required', 'SVG兼容预览须为无损PNG。')
            preview.verify()
        return {**asset, 'source_sha256': source_sha, 'embedded_media_path': str(fallback.resolve()),
                'embedded_media_sha256': hashlib.sha256(fallback.read_bytes()).hexdigest(),
                'embedded_mime_type': 'image/png', 'media_conversion': 'original_svg_with_approved_png_compatibility_preview',
                'original_bytes_embedded': True, 'decoded_pixels_preserved': True,
                'vector_media_path': str(path.resolve()), 'vector_media_sha256': source_sha,
                'vector_dimensions': list(dimensions)}
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        media_format = image.format
        if media_format == 'WEBP':
            if getattr(image, 'n_frames', 1) != 1:
                raise PipelineError('animated_source_media_unsupported', '动态WEBP不能直接作为静态论文原图导出；请先上传所需单帧PNG。')
            rgba = image.convert('RGBA')
            buffer = io.BytesIO(); rgba.save(buffer, format='PNG')
            embedded = buffer.getvalue()
            with Image.open(io.BytesIO(embedded)) as decoded:
                if decoded.size != rgba.size or decoded.convert('RGBA').tobytes() != rgba.tobytes():
                    raise PipelineError('source_media_conversion_failed', 'WEBP派生PNG像素校验失败，未替换原图。')
            target_dir = Path(output_dir); target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / (source_sha + '.png')
            if not path.is_file() or path.read_bytes() != embedded:
                path.write_bytes(embedded)
            conversion = 'webp_lossless_decode_to_png'
            mime = 'image/png'
        elif media_format in ('JPEG', 'PNG', 'GIF', 'BMP', 'TIFF', 'WMF'):
            embedded = raw
            conversion = 'original_bytes'
            mime = Image.MIME.get(media_format, asset.get('mime_type'))
        else:
            raise PipelineError('source_media_format_unsupported', '此原图格式不能嵌入PowerPoint；请上传PNG或JPEG，原始材料仍保留。')
    return {**asset, 'source_sha256': source_sha, 'embedded_media_path': str(path.resolve()),
            'embedded_media_sha256': hashlib.sha256(embedded).hexdigest(), 'embedded_mime_type': mime,
            'media_conversion': conversion, 'original_bytes_embedded': conversion == 'original_bytes',
            'decoded_pixels_preserved': True}


def public_media_provenance(asset):
    return {key: asset.get(key) for key in ('id', 'source_sha256', 'embedded_media_sha256',
        'embedded_mime_type', 'media_conversion', 'original_bytes_embedded', 'decoded_pixels_preserved',
        'vector_media_sha256', 'vector_dimensions')}


def estimate_tokens(text):
    """Conservative mixed-language budget: ASCII / 3; non-ASCII 1.5 tokens."""
    ascii_chars = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_chars / 3 + (len(text) - ascii_chars) * 1.5)


def balanced_evidence(chunks, budget):
    """Fair across source documents/pages; full text when it fits, disclose cuts."""
    groups = {}
    for index, chunk in enumerate(chunks):
        source = chunk.get('source', {})
        key = str(source.get('sha256') or source.get('filename') or 'unidentified')
        groups.setdefault(key, []).append(chunk)
    ordered = []
    for index in range(max((len(rows) for rows in groups.values()), default=0)):
        for rows in groups.values():
            if index < len(rows): ordered.append(rows[index])
    maximum = max(1, budget // 180) if budget else 0
    if len(ordered) > maximum:
        indexes = sorted({round(i * (len(ordered) - 1) / max(1, maximum - 1)) for i in range(maximum)})
        selected = [ordered[i] for i in indexes]
    else: selected = ordered
    quotas = [0] * len(selected); remaining = budget
    while remaining and selected:
        active = [i for i, c in enumerate(selected) if quotas[i] < len(str(c.get('text', '')))]
        if not active: break
        allocation = max(1, remaining // len(active))
        for i in active:
            n = min(allocation, len(str(selected[i].get('text', ''))) - quotas[i], remaining)
            quotas[i] += n; remaining -= n
    usable = []
    for chunk, quota in zip(selected, quotas):
        if not quota: continue
        text = str(chunk.get('text', ''))
        if len(text) <= quota: excerpt = text
        else:
            marker = '\n[原文节选，省略处未提供模型]\n'
            room = max(0, quota - len(marker) * 2)
            head = room // 3; middle = (len(text) - room // 3) // 2
            excerpt = text[:head] + marker + text[middle:middle + room // 3] + marker + text[-(room - 2 * head):] if room else text[:quota]
            excerpt = excerpt[:quota]
        usable.append({**chunk, 'text': excerpt, 'excerpted': len(text) > quota})
    return usable


def verify_source_assets(spec, config, cancel_check=None, max_count=2):
    records, ids = spec.get('source_assets') or [], spec.get('source_asset_ids') or []
    if not isinstance(records, list) or not isinstance(ids, list) or len(ids) > max_count or len(records) > max(64, max_count):
        raise PipelineError('source_asset_limit', '论文原图引用数量或记录格式无效。')
    if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or not r['id'] for r in records) or any(not isinstance(key, str) for key in ids):
        raise PipelineError('invalid_source_asset', '原图ID必须来自当前项目的唯一受控材料记录。')
    catalog = {r['id']: r for r in records}
    if len(catalog) != len(records) or len(set(ids)) != len(ids) or any(key not in catalog for key in ids):
        raise PipelineError('invalid_source_asset', '原图ID必须来自当前项目的唯一受控材料记录。')
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = roots if isinstance(roots, list) else [roots]
    verified = []
    for key in ids:
        check_cancel(cancel_check); record = catalog[key]
        if record.get('evidence_kind') != 'project_material' or record.get('extraction_method') not in (
                'pdf_embedded_original', 'office_embedded_original', 'uploaded_original', 'original_pdf_page_render', 'original_pptx_page_render'):
            raise PipelineError('invalid_source_asset', '模板参考或未验证图像不能冒充论文原图。')
        if record.get('extraction_method') in {'original_pdf_page_render', 'original_pptx_page_render'} and (
                record.get('original_bytes_preserved') is not False or
                type(record.get('source', {}).get('page')) is not int or record['source']['page'] < 1):
            raise PipelineError('invalid_source_asset', 'PDF原页参考必须保留真实页码，并标明为原页渲染。')
        path = allowed_file(record.get('path', ''), roots)
        if path.stat().st_size > 20 * 1024 * 1024 or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', ''))):
            raise PipelineError('invalid_source_asset', '原图大小或hash无效。')
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise PipelineError('source_asset_hash_mismatch', '论文原图内容已变化，须重新解析原材料。')
        try:
            with Image.open(path) as image:
                image.load(); dimensions = image.size
                if image.width * image.height > 40_000_000: raise ValueError('pixels')
        except Exception:
            raise PipelineError('invalid_source_asset', '论文原图无法解码。') from None
        if dimensions != (record.get('width'), record.get('height')):
            raise PipelineError('invalid_source_asset', '论文原图尺寸与来源记录不一致。')
        verified.append({**record, 'path': str(path)})
    views = spec.get('source_asset_views') or []
    if not isinstance(views, list) or len(views) > len(ids):
        raise PipelineError('invalid_source_asset_view', '原图局部视图格式无效。')
    seen = set()
    for view in views:
        if not isinstance(view, dict) or view.get('asset_id') not in ids or view['asset_id'] in seen:
            raise PipelineError('invalid_source_asset_view', '原图局部视图须引用本页唯一原图。')
        seen.add(view['asset_id']); crop = view.get('crop'); record = catalog[view['asset_id']]
        if not isinstance(crop, list) or len(crop) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in crop):
            raise PipelineError('invalid_source_asset_view', '原图裁切须为有限像素[x,y,width,height]。')
        x, y, w, h = crop
        if min(x, y) < 0 or min(w, h) < 2 or x + w > record['width'] or y + h > record['height']:
            raise PipelineError('invalid_source_asset_view', '原图局部裁切超出原始图像。')
    return verified


def normalize_planner_source_views(spec, config, cancel_check=None):
    """Decode JSON-array strings only; retain every original figure validation.

    The provider's saved raw response stays immutable. This structural adapter
    returns a new view list and hashes the original crop representation for the
    planning report. Semantic descriptions, malformed arrays and invalid bounds
    are rejected instead of guessed, clamped or silently changed to a full view.
    """
    views = spec.get('source_asset_views') or []
    if not isinstance(views, list):
        raise PipelineError('invalid_source_asset_view', '原图局部视图格式无效。')
    normalized, records = [], []
    for view in views:
        if isinstance(view, dict) and isinstance(view.get('crop'), str):
            original = view['crop']
            try:
                if len(original) > 256:
                    raise ValueError()
                crop = json.loads(original)
            except (ValueError, TypeError):
                raise PipelineError('invalid_source_asset_view', '原图裁切字符串必须编码四个有限数值的JSON数组。') from None
            normalized.append({**view, 'crop': crop})
            records.append({'asset_id': view.get('asset_id'), 'normalization': 'json_array_string',
                            'original_crop_sha256': hashlib.sha256(original.encode('utf-8')).hexdigest()})
        else:
            normalized.append(dict(view) if isinstance(view, dict) else view)
    verify_source_assets({**spec, 'source_asset_views': normalized}, config, cancel_check)
    return normalized, records


def figure_placements(spec, assets, width, height):
    views = {v['asset_id']: v['crop'] for v in spec.get('source_asset_views', [])}
    result = []
    for index, asset in enumerate(assets):
        crop = views.get(asset['id'], [0, 0, asset['width'], asset['height']])
        if len(assets) == 1:
            slot = [.27 * width, .14 * height, .70 * width, .80 * height]
        else:
            slot = [(.05 + index * .48) * width, .24 * height, .44 * width, .69 * height]
        scale = min(slot[2] / crop[2], slot[3] / crop[3])
        w, h = max(1, round(crop[2] * scale)), max(1, round(crop[3] * scale))
        x, y = round(slot[0] + (slot[2] - w) / 2), round(slot[1] + (slot[3] - h) / 2)
        result.append({'asset_id': asset['id'], 'sha256': asset['sha256'], 'bounds': [x, y, w, h],
                       'slot_bounds': [round(v) for v in slot], 'source_crop_pixels': crop,
                       'figure_label': asset.get('figure_label', ''), 'source': asset.get('source', {}),
                       'is_detail': crop != [0, 0, asset['width'], asset['height']]})
    return result


def original_view(asset, placement):
    with Image.open(asset['path']) as source:
        rgba = source.convert('RGBA')
        white = Image.new('RGBA', rgba.size, 'white'); white.alpha_composite(rgba)
        x, y, w, h = placement['source_crop_pixels']
        view = white.convert('RGB').crop((round(x), round(y), round(x + w), round(y + h)))
        return view.resize((placement['bounds'][2], placement['bounds'][3]), Image.Resampling.LANCZOS)


def compose_source_figures(image, spec, assets):
    image = image.convert('RGB'); placements = figure_placements(spec, assets, *image.size)
    for asset, placement in zip(assets, placements):
        sx, sy, sw, sh = placement['slot_bounds']
        ImageDraw.Draw(image).rectangle((sx, sy, sx + sw - 1, sy + sh - 1), fill='white')
        view = original_view(asset, placement); x, y, _, _ = placement['bounds']
        image.paste(view, (x, y))
        placement['rendered_pixel_sha256'] = hashlib.sha256(view.tobytes()).hexdigest()
    return image, placements


def assert_figure_pixels(image, spec, assets):
    placements = figure_placements(spec, assets, *image.size)
    for asset, placement in zip(assets, placements):
        x, y, w, h = placement['bounds']
        if image.convert('RGB').crop((x, y, x + w, y + h)).tobytes() != original_view(asset, placement).tobytes():
            raise PipelineError('source_figure_pixels_changed', '已确认页图中的论文原图区域已变化，不能描摹或导出。')
    return placements


def inject_original_figures(trace, spec, assets, placements):
    """Exclude model reconstructions under the preserved figure, then add originals."""
    remaining, removed = [], []
    for element in trace.get('elements', []):
        x, y, w, h = element.get('bounds', [0, 0, 0, 0]); overlaps = False
        for placement in placements:
            a, b, c, d = placement['slot_bounds']
            intersection = max(0, min(x + w, a + c) - max(x, a)) * max(0, min(y + h, b + d) - max(y, b))
            if intersection > max(w * h, 1) * .02:
                full_background = element.get('type') == 'shape' and w * h > trace['canvas']['width'] * trace['canvas']['height'] * .8
                overlaps |= not full_background
        if overlaps: removed.append({'type': element.get('type'), 'reason': 'reserved_original_figure_region'})
        else: remaining.append(element)
    for asset, placement in zip(assets, placements):
        remaining.append({'type': 'image', 'asset_id': asset['id'], 'bounds': placement['bounds'],
                          'image_path': asset['path'], 'source_asset_sha256': asset['sha256'],
                          'source_crop_pixels': placement['source_crop_pixels'], 'asset_origin': 'verified_project_original',
                          'source': asset.get('source', {}), 'figure_label': asset.get('figure_label', '')})
    trace['elements'] = remaining
    trace['source_figure_placements'] = placements
    trace['excluded_model_figure_objects'] = removed
    return trace
