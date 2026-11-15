"""TraceSpec adapter for the project's existing PPTXBuilder/ExportService."""
import json
import math
import io
import re
import unicodedata
import hashlib
from copy import deepcopy
from pathlib import Path
from PIL import Image
from .wuyu_errors import PipelineError, check_cancel
from .wuyu_text_fit import fit_native_text
from .wuyu_flow_animation import apply_flow_animation
from .wuyu_source_assets import (prepare_ppt_source_media, public_media_provenance,
                                 is_svg_media, svg_source_dimensions, attach_original_svg)
from .wuyu_asset_quality import (image_file_quality, vector_quality, quality_summary,
                                  protect_presentation_media, inspect_presentation_media)
from .wuyu_typography import (normalize_trace_typography, apply_chart_typography,
                               normalize_presentation_typography)
from .wuyu_trace_styles import (SHAPES, ALIGNMENTS, number, points, rgb, validate_fill,
                                validate_line, validate_font, validate_text_layout,
                                normalize_trace_styles, apply_fill, apply_line, apply_font, apply_text, clear_theme_effects)


def correct_minor_bounds(trace, width, height, tolerance=2):
    """Clip only independently measured <=2px edge overshoots, never rescale."""
    corrections = []
    if not isinstance(trace, dict) or not isinstance(trace.get('elements'), list):
        return corrections
    for index, el in enumerate(trace['elements']):
        box = el.get('bounds') if isinstance(el, dict) else None
        if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
            continue
        x, y, w, h = box
        edges = (-x, -y, x + w - width, y + h - height)
        if min(w, h) <= 0 or max(edges) <= 0 or max(edges) > tolerance:
            continue
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        if right <= left or bottom <= top:
            continue
        corrected = [left, top, right - left, bottom - top]
        el['bounds'] = corrected
        corrections.append({'element_index': index, 'original_bounds': list(box), 'bounds': corrected,
                            'reason': 'edge_overshoot_at_most_2px_clipped_without_rescaling'})
    return corrections


def validate_trace(trace, width, height, require_data=False):
    if not isinstance(trace, dict) or not isinstance(trace.get('elements'), list) or len(trace['elements']) > 500:
        raise PipelineError('invalid_trace', 'TraceSpec必须含有有界elements列表。')
    canvas = trace.get('canvas', {})
    if not isinstance(canvas, dict) or canvas.get('width') != width or canvas.get('height') != height or canvas.get('dpi', 96) != 96:
        raise PipelineError('invalid_trace', 'TraceSpec画布与最终页图尺寸不一致。')
    number(width, 96, 5376); number(height, 96, 5376)
    if not trace['elements']:
        raise PipelineError('invalid_trace', '描摹未返回可重建对象。')
    corrections = normalize_trace_styles(trace)
    if corrections:
        trace.setdefault('normalization_corrections', []).extend(corrections)
    for el in trace['elements']:
        if not isinstance(el, dict) or el.get('type') not in ('text', 'image', 'table', 'chart', 'shape'):
            raise PipelineError('invalid_trace', 'TraceSpec包含不支持的对象类型。')
        box = el.get('bounds')
        if not isinstance(box, list) or len(box) != 4 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in box):
            raise PipelineError('invalid_trace', '对象坐标必须为有限数值[x,y,width,height]。')
        x, y, w, h = box
        is_line = el['type'] == 'shape' and el.get('shape') == 'line'
        if min(x, y) < 0 or min(w, h) < 0 or (max(w, h) == 0 if is_line else min(w, h) == 0) or x + w > width + .01 or y + h > height + .01:
            raise PipelineError('trace_out_of_bounds', '描摹对象超出页面。')
        if el['type'] == 'text':
            if not isinstance(el.get('text'), str) or not el['text'].strip() or len(el['text']) > 20000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', el['text']):
                raise PipelineError('invalid_trace', '描摹文字为空或过长。')
            validate_text_layout(el)
        if el['type'] in ('shape', 'text'):
            if 'fill' in el:
                validate_fill(el['fill'])
            validate_line(el.get('line'))
        if 'rotation' in el:
            number(el['rotation'], -360, 360)
        if el['type'] == 'shape':
            kind = el.get('shape', 'rect')
            if not isinstance(kind, str) or kind not in SHAPES:
                raise PipelineError('unsupported_trace_shape', 'TraceSpec原生形状类型不受支持，未静默替换为矩形。')
            if 'corner_radius' in el:
                if kind != 'round_rect':
                    raise PipelineError('invalid_trace', 'corner_radius仅适用于round_rect。')
                number(el['corner_radius'], 0, min(w, h) / 2)
            vertices = el.get('points')
            if kind == 'polygon' or vertices is not None:
                count_ok = isinstance(vertices, list) and ((3 <= len(vertices) <= 100) if kind == 'polygon' else 2 <= len(vertices) <= 100 if kind == 'line' else False)
                if not count_ok:
                    raise PipelineError('invalid_trace', '多边形需要3至100个局部像素顶点，开放折线需要2至100个局部像素顶点。')
                for vertex in vertices:
                    if not isinstance(vertex, list) or len(vertex) != 2:
                        raise PipelineError('invalid_trace', '形状顶点必须为[x,y]。')
                    number(vertex[0], 0, w); number(vertex[1], 0, h)
                if len(set(tuple(v) for v in vertices)) < (3 if kind == 'polygon' else 2):
                    raise PipelineError('invalid_trace', '形状顶点不能重合。')
        if el['type'] == 'table':
            rows = el.get('rows')
            if rows is not None or require_data:
                if not isinstance(rows, list) or not 1 <= len(rows) <= 100 or not isinstance(rows[0], list) or not 1 <= len(rows[0]) <= 30 or any(not isinstance(row, list) or len(row) != len(rows[0]) for row in rows):
                    raise PipelineError('invalid_trace', '原生表格须为不超过100行30列的矩形数据。')
                if any(isinstance(value, (dict, list)) or len(str(value)) > 20000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', str(value)) for row in rows for value in row):
                    raise PipelineError('invalid_trace', '原生表格单元格必须为有界标量。')
            style = el.get('table_style', {})
            if not isinstance(style, dict):
                raise PipelineError('invalid_trace', '表格样式必须为对象。')
            for field in ('font', 'header_font'):
                if field in style:
                    validate_font(style[field])
            for field in ('header_fill', 'cell_fill'):
                if field in style:
                    validate_fill(style[field])
            if not isinstance(style.get('align', 'left'), str) or style.get('align', 'left') not in ALIGNMENTS:
                raise PipelineError('invalid_trace', '表格文字对齐无效。')
        if el['type'] == 'chart':
            from .wuyu_native_charts import validate_native_chart
            validate_native_chart(el, require_data=require_data)
    bg = trace.get('background', {})
    if not isinstance(bg, dict):
        raise PipelineError('invalid_trace', 'background必须为对象。')
    if any(k in bg for k in ('image_path', 'image', 'source_image')):
        raise PipelineError('text_ghosting_risk', '不能将带原文字的整页图作为可编辑文字底图。')
    validate_fill(bg.get('fill', bg.get('color', '#FFFFFF')))
    return trace


def crop_trace_images(trace, image_path, output_dir, assets, allowed_roots):
    from .wuyu_materials import allowed_file
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    asset_map = {}
    for i, asset in enumerate(assets or []):
        if isinstance(asset, dict) and asset.get('id') and asset.get('path'):
            asset_map[str(asset['id'])] = allowed_file(asset['path'], allowed_roots)
            if asset.get('sha256') and hashlib.sha256(asset_map[str(asset['id'])].read_bytes()).hexdigest() != asset['sha256']:
                raise PipelineError('source_asset_hash_mismatch', '原始素材hash已变化，拒绝描摹。')
    with Image.open(image_path) as source:
        for n, el in enumerate(trace['elements']):
            if el['type'] != 'image':
                continue
            if el.get('asset_id') and el.get('asset_id') not in asset_map:
                raise PipelineError('unknown_trace_asset', '描摹引用了不存在的原始素材ID。')
            if el['bounds'][2] * el['bounds'][3] > source.width * source.height * .9:
                raise PipelineError('text_ghosting_risk', '禁止用整页图片对象伪装原生描摹。')
            if el.get('asset_id') in asset_map:
                original = asset_map[el['asset_id']]
                if is_svg_media(original):
                    ow, oh = svg_source_dimensions(original)
                    x, y, w, h = el['bounds']
                    scale = min(w / ow, h / oh)
                    nw, nh = ow * scale, oh * scale
                    el['bounds'] = [x + (w - nw) / 2, y + (h - nh) / 2, nw, nh]
                    left, top, right, bottom = [round(v) for v in (el['bounds'][0], el['bounds'][1],
                        el['bounds'][0] + nw, el['bounds'][1] + nh)]
                    if right <= left or bottom <= top:
                        raise PipelineError('invalid_trace', 'SVG兼容预览裁切范围不足1像素。')
                    preview = dest / f'vector-preview-{n:03d}.png'
                    source.crop((left, top, right, bottom)).save(preview, format='PNG')
                    el.update(image_path=str(original), raster_fallback_path=str(preview),
                              asset_origin='original_supplied_vector', image_fit='contain_preserving_original_aspect_ratio',
                              source_asset_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                              asset_quality=vector_quality())
                    continue
                if el.get('asset_origin') == 'verified_project_original':
                    if hashlib.sha256(original.read_bytes()).hexdigest() != el.get('source_asset_sha256'):
                        raise PipelineError('source_asset_hash_mismatch', '论文原图hash与确认的来源不一致。')
                    el['image_path'] = str(original)
                    el['image_fit'] = 'original_media_with_optional_verified_crop'
                    el['asset_quality'] = image_file_quality(original, el['bounds'],
                                                            crop_pixels=el.get('source_crop_pixels'))
                    continue
                with Image.open(original) as media:
                    x, y, w, h = el['bounds']
                    scale = min(w / media.width, h / media.height)
                    nw, nh = media.width * scale, media.height * scale
                    el['bounds'] = [x + (w - nw) / 2, y + (h - nh) / 2, nw, nh]
                el['image_path'] = str(original)
                el['asset_origin'] = 'original_supplied_media'
                el['image_fit'] = 'contain_preserving_original_aspect_ratio'
                el['source_asset_sha256'] = hashlib.sha256(original.read_bytes()).hexdigest()
                el['asset_quality'] = image_file_quality(original, el['bounds'])
                continue
            crop = el.get('source_crop') or el['bounds']
            if not isinstance(crop, list) or len(crop) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in crop):
                raise PipelineError('invalid_trace', '图片裁切坐标必须为四个有限数字。')
            x, y, w, h = crop
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > source.width or y + h > source.height:
                raise PipelineError('invalid_trace', '图片裁切坐标超出原图。')
            for text_index, text in enumerate(trace['elements']):
                if text['type'] != 'text':
                    continue
                tx, ty, tw, th = text['bounds']
                overlap = max(0, min(x + w, tx + tw) - max(x, tx)) * max(0, min(y + h, ty + th) - max(y, ty))
                if overlap > tw * th * .05:
                    from .wuyu_trace_recovery import raster_overlap_occlusion_proof
                    proof = raster_overlap_occlusion_proof(trace, n, text_index, crop)
                    if proof is not None:
                        trace.setdefault('raster_occlusion_proofs', []).append(proof)
                        continue
                    raise PipelineError('text_ghosting_risk', '图像裁切包含将被原生重建的文字，会产生重影。')
            if w * h > source.width * source.height * .9:
                raise PipelineError('text_ghosting_risk', '禁止用整页裁切伪装可编辑图像对象。')
            target = dest / f'crop-{n:03d}.png'
            pixel_box = (round(x), round(y), round(x + w), round(y + h))
            if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
                raise PipelineError('invalid_trace', '图片裁切范围不足1像素。')
            # Always crop the approved full-resolution image. A lossless PNG is
            # embedded at these exact pixels; no thumbnail or resize is involved.
            source.crop(pixel_box).save(target, format='PNG')
            el['image_path'] = str(target)
            el['asset_origin'] = 'exact_raster_crop; no invented_vector'
            el['asset_quality'] = {**image_file_quality(target, el['bounds']),
                                   'source_pixel_dimensions': list(source.size),
                                   'crop_pixel_box': list(pixel_box), 'pixels_resampled': False}
    trace['asset_quality_summary'] = quality_summary([
        {'element_index': index, 'quality': el['asset_quality']}
        for index, el in enumerate(trace['elements']) if el.get('asset_quality')])
    return trace


def export_trace_specs(traces, output_file, cancel_check=None):
    """Build through the original PPTXBuilder, without the old full-image fallback."""
    from utils.pptx_builder import PPTXBuilder
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_CONNECTOR
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    builder = PPTXBuilder()
    builder.create_presentation()
    if not isinstance(traces, list) or not 1 <= len(traces) <= 80:
        raise PipelineError('invalid_trace', '可编辑导出需要1至80页TraceSpec。')
    # Normalization belongs to this local export, never the cached paid trace.
    traces = deepcopy(traces)
    for trace in traces:
        c = trace['canvas']
        validate_trace(trace, c['width'], c['height'], require_data=True)
    traces, typography_audit = normalize_trace_typography(traces)
    first = traces[0]['canvas']
    builder.setup_presentation_size(first['width'], first['height'])

    text_fit_records, animation_records = [], []
    for page_number, trace in enumerate(traces, 1):
        check_cancel(cancel_check)
        c = trace['canvas']
        validate_trace(trace, c['width'], c['height'], require_data=True)
        if (c['width'], c['height']) != (first['width'], first['height']):
            raise PipelineError('canvas_mismatch', '可编辑导出的各页画布必须一致。')
        slide = builder.add_blank_slide()
        shape_ids = []
        background = trace.get('background', {})
        apply_fill(slide.background.fill, background.get('fill', background.get('color', '#FFFFFF')))
        for element_index, el in enumerate(trace['elements']):
            check_cancel(cancel_check)
            x, y, w, h = el['bounds']
            bbox = [x, y, x + w, y + h]
            pos = [Inches(v / 96) for v in (x, y, w, h)]
            if el['type'] == 'text':
                builder.add_text_element(slide, el['text'], bbox)
                shape = slide.shapes[-1]
                shape.left, shape.top, shape.width, shape.height = pos
                export_font, fit_record = fit_native_text(el)
                apply_text(shape, {**el, 'font': export_font})
                if fit_record:
                    text_fit_records.append({'page': page_number, 'element_index': element_index,
                                             **fit_record})
                apply_fill(shape.fill, el.get('fill', 'none'))
                apply_line(shape, el.get('line'))
            elif el['type'] == 'image':
                path = el.get('image_path')
                if not path or not Path(path).is_file():
                    raise PipelineError('missing_trace_asset', '描摹图像素材不存在，不能导出占位符。')
                if not is_svg_media(path):
                    try:
                        with Image.open(path) as image:
                            image.verify()
                    except Exception:
                        raise PipelineError('missing_trace_asset', '描摹图像素材损坏，不能导出占位符。') from None
                prepared = prepare_ppt_source_media({'id': el.get('asset_id'), 'path': path,
                    'raster_fallback_path': el.get('raster_fallback_path'),
                    'sha256': el.get('source_asset_sha256') or hashlib.sha256(Path(path).read_bytes()).hexdigest()},
                    Path(output_file).parent / (Path(output_file).name + '.media'))
                path = prepared['embedded_media_path']
                builder.add_image_element(slide, path, bbox)
                if slide.shapes[-1].shape_type != 13:
                    raise PipelineError('trace_asset_export', '原有图片构建器未成功嵌入素材，已拒绝占位导出。')
                picture = slide.shapes[-1]
                if hashlib.sha256(picture.image.blob).hexdigest() != prepared['embedded_media_sha256']:
                    raise PipelineError('source_asset_export_changed', 'PPT图片在嵌入时发生变化，拒绝导出。')
                attach_original_svg(picture, prepared)
                if el.get('asset_origin') == 'verified_project_original':
                    picture = slide.shapes[-1]
                    if hashlib.sha256(picture.image.blob).hexdigest() != prepared['embedded_media_sha256']:
                        raise PipelineError('source_asset_export_changed', 'PPT中的来源图片与已校验嵌入媒体哈希不一致，拒绝导出。')
                    crop = el.get('source_crop_pixels')
                    if crop:
                        with Image.open(path) as original:
                            x, y, w, h = crop
                            if min(x, y) < 0 or min(w, h) <= 0 or x + w > original.width or y + h > original.height:
                                raise PipelineError('invalid_source_asset_view', '原图PPT裁切越界。')
                            picture.crop_left = x / original.width
                            picture.crop_top = y / original.height
                            picture.crop_right = max(0, 1 - (x + w) / original.width)
                            picture.crop_bottom = max(0, 1 - (y + h) / original.height)
                    picture.name = 'Original source figure: ' + str(el.get('figure_label') or el.get('asset_id'))
            elif el['type'] == 'table':
                rows = el.get('rows')
                table = slide.shapes.add_table(len(rows), len(rows[0]), *pos).table
                style = el.get('table_style', {})
                align = {'left': PP_ALIGN.LEFT, 'center': PP_ALIGN.CENTER, 'right': PP_ALIGN.RIGHT, 'justify': PP_ALIGN.JUSTIFY}
                for ri, row in enumerate(rows):
                    for ci, value in enumerate(row):
                        cell = table.cell(ri, ci)
                        cell.text = '' if value is None else str(value)
                        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                        field = 'header_fill' if ri == 0 else 'cell_fill'
                        if field in style:
                            apply_fill(cell.fill, style[field])
                        font = style.get('header_font', style.get('font', {})) if ri == 0 else style.get('font', {})
                        for paragraph in cell.text_frame.paragraphs:
                            paragraph.alignment = align[style.get('align', 'left')]
                            apply_font(paragraph.font, font)
                            for run in paragraph.runs:
                                apply_font(run.font, font)
            elif el['type'] == 'chart':
                from .wuyu_native_charts import NATIVE_CHART_KINDS, apply_native_chart_colors, native_category_data
                if el.get('source_data_verified') is not True:
                    raise PipelineError('chart_source_required', '原生图表必须有项目原始数据。')
                data = native_category_data(el)
                chart = slide.shapes.add_chart(NATIVE_CHART_KINDS[el['chart_type']], *pos, data).chart
                apply_native_chart_colors(chart, el)
                apply_chart_typography(chart, el.get('chart_style', {}).get('font'))
            elif el['type'] == 'shape':
                kinds = {'rect': MSO_AUTO_SHAPE_TYPE.RECTANGLE, 'round_rect': MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE,
                         'ellipse': MSO_AUTO_SHAPE_TYPE.OVAL, 'triangle': MSO_AUTO_SHAPE_TYPE.ISOSCELES_TRIANGLE,
                         'right_triangle': MSO_AUTO_SHAPE_TYPE.RIGHT_TRIANGLE, 'diamond': MSO_AUTO_SHAPE_TYPE.DIAMOND,
                         'parallelogram': MSO_AUTO_SHAPE_TYPE.PARALLELOGRAM, 'trapezoid': MSO_AUTO_SHAPE_TYPE.TRAPEZOID,
                         'hexagon': MSO_AUTO_SHAPE_TYPE.HEXAGON, 'chevron': MSO_AUTO_SHAPE_TYPE.CHEVRON,
                         'pentagon': MSO_AUTO_SHAPE_TYPE.PENTAGON, 'right_arrow': MSO_AUTO_SHAPE_TYPE.RIGHT_ARROW,
                         'left_arrow': MSO_AUTO_SHAPE_TYPE.LEFT_ARROW, 'up_arrow': MSO_AUTO_SHAPE_TYPE.UP_ARROW,
                         'down_arrow': MSO_AUTO_SHAPE_TYPE.DOWN_ARROW, 'left_right_arrow': MSO_AUTO_SHAPE_TYPE.LEFT_RIGHT_ARROW,
                         'up_down_arrow': MSO_AUTO_SHAPE_TYPE.UP_DOWN_ARROW}
                kind = el.get('shape', 'rect')
                if kind == 'polygon':
                    vertices = el['points']
                    freeform = slide.shapes.build_freeform(*vertices[0], scale=Inches(1) / 96)
                    freeform.add_line_segments(vertices[1:], close=True)
                    sh = freeform.convert_to_shape(Inches(x / 96), Inches(y / 96))
                elif kind == 'line':
                    vertices = el.get('points', [[0, 0], [w, h]])
                    if len(vertices) == 2:
                        start, end = vertices
                        sh = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches((x + start[0]) / 96), Inches((y + start[1]) / 96),
                                                        Inches((x + end[0]) / 96), Inches((y + end[1]) / 96))
                    else:
                        # Every original bend is retained as an editable native
                        # path. Never replace a routed line with its endpoints,
                        # close the path, or fill the enclosed interior.
                        freeform = slide.shapes.build_freeform(*vertices[0], scale=Inches(1) / 96)
                        freeform.add_line_segments(vertices[1:], close=False)
                        sh = freeform.convert_to_shape(Inches(x / 96), Inches(y / 96))
                        apply_fill(sh.fill, 'none')
                else:
                    sh = slide.shapes.add_shape(kinds[kind], *pos)
                    if kind == 'round_rect' and 'corner_radius' in el:
                        sh.adjustments[0] = el['corner_radius'] / min(w, h)
                if kind != 'line':
                    apply_fill(sh.fill, el.get('fill', '#FFFFFF'))
                apply_line(sh, el.get('line', {'color': '#222222', 'width': 1} if kind == 'line' else None))
            if 'rotation' in el:
                slide.shapes[-1].rotation = el['rotation'] % 360
            clear_theme_effects(slide.shapes[-1])
            shape_ids.append(slide.shapes[-1].shape_id)
        animation = apply_flow_animation(slide, trace['elements'], shape_ids)
        animation_records.append({'page': page_number, **animation})
    typography_audit['native_properties'] = normalize_presentation_typography(builder.prs)
    protect_presentation_media(builder.prs)
    builder.save(str(output_file))
    Path(str(output_file) + '.animation.json').write_text(json.dumps({
        'version': 1, 'policy': 'native_on_click_appear_per_evidenced_process_stage',
        'pages': animation_records, 'playback_verified': False,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    # Independent audit of export-only adaptations; no source text or private
    # font path is included. Cached paid TraceSpecs remain byte-for-byte intact.
    Path(str(output_file) + '.text-fit.json').write_text(json.dumps({
        'version': 1, 'policy': 'measure_actual_fonts_shrink_only_overflowing_own_box',
        'records': text_fit_records, 'content_changed': False, 'geometry_changed': False,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    Path(str(output_file) + '.typography.json').write_text(json.dumps(
        typography_audit, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(output_file)


def write_presentation_notes(path, pages):
    """Keep detailed explanation in native notes, outside the final composition."""
    from .wuyu_content_contracts import bound_speaker_notes
    notes = [(page.get('spec') or {}).get('speaker_notes', '') for page in pages]
    if any(not isinstance(text, str) for text in notes):
        raise PipelineError('invalid_speaker_notes', '演讲备注必须为文本。')
    # Projects planned before notes were bounded at the source still export.
    notes = [bound_speaker_notes(text) for text in notes]
    result = {'pages_with_notes': sum(bool(text.strip()) for text in notes),
              'characters': sum(len(text) for text in notes)}
    if not result['pages_with_notes']:
        return result
    from pptx import Presentation
    prs = Presentation(path)
    if len(prs.slides) != len(pages):
        raise PipelineError('notes_page_count', '演讲备注与实际导出页数不一致。')
    for slide, text in zip(prs.slides, notes):
        if text.strip():
            slide.notes_slide.notes_text_frame.text = text
    target = Path(path)
    temporary = target.with_name(target.name + '.notes.tmp')
    try:
        prs.save(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def inspect_pptx(path, expected_pages, editable=False, expected_texts=None, expected_source_assets=None,
                 native_page_numbers=None, fallback_image_hashes=None, textless_icon_page_numbers=None):
    from pptx import Presentation
    prs = Presentation(path)
    counts = {'text': 0, 'images': 0, 'tables': 0, 'charts': 0, 'shapes': 0}
    per_page = []
    bounds_ok, fonts_ok, images_ok = True, True, True
    def normalize(value):
        return re.sub(r'[^\w\u3400-\u9fff]', '', unicodedata.normalize('NFKC', str(value)), flags=re.UNICODE).lower()
    for slide in prs.slides:
        page_counts = {key: 0 for key in counts}
        native_text = []
        page_image_hashes = []
        def inspect_text(frame):
            nonlocal fonts_ok
            native_text.append(frame.text)
            for para in frame.paragraphs:
                for font in [para.font] + [run.font for run in para.runs]:
                    if font.size is not None:
                        fonts_ok &= 1 <= font.size.pt <= 160
        for sh in slide.shapes:
            bounds_ok &= sh.left >= -1 and sh.top >= -1 and sh.left + sh.width <= prs.slide_width + 2 and sh.top + sh.height <= prs.slide_height + 2
            page_counts['images'] += sh.shape_type == 13
            if sh.shape_type == 13:
                page_image_hashes.append(hashlib.sha256(sh.image.blob).hexdigest())
                try:
                    with Image.open(io.BytesIO(sh.image.blob)) as im:
                        im.verify()
                except Exception:
                    images_ok = False
            page_counts['tables'] += bool(sh.has_table)
            page_counts['charts'] += bool(sh.has_chart)
            page_counts['shapes'] += sh.shape_type in (1, 5, 9)
            if sh.has_table:
                for row in sh.table.rows:
                    for cell in row.cells:
                        inspect_text(cell.text_frame)
            if sh.has_chart:
                native_text.extend(str(category.label) for category in sh.chart.plots[0].categories)
                for series in sh.chart.series:
                    native_text.append(series.name)
                    native_text.extend(str(value) for value in series.values if value is not None)
            if sh.has_text_frame and sh.text.strip():
                page_counts['text'] += 1
                inspect_text(sh.text_frame)
        for key in counts:
            counts[key] += page_counts[key]
        required = expected_texts[len(per_page)] if expected_texts is not None and len(per_page) < len(expected_texts) else []
        recognized = normalize(''.join(native_text))
        missing = [text for text in required if normalize(text) and normalize(text) not in recognized]
        required_assets = expected_source_assets[len(per_page)] if expected_source_assets is not None and len(per_page) < len(expected_source_assets) else []
        missing_assets = [a['id'] for a in required_assets if a.get('embedded_media_sha256', a['sha256']) not in page_image_hashes]
        original_bytes = all(a.get('original_bytes_embedded', True) for a in required_assets)
        timing = slide._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}timing')
        step_count = sum(node.get('nodeType') == 'clickEffect' for node in timing.iter(
            '{http://schemas.openxmlformats.org/presentationml/2006/main}cTn')) if timing is not None else 0
        per_page.append({'number': len(per_page) + 1, 'object_counts': page_counts,
                         'animation': {'enabled': step_count >= 2, 'click_steps': step_count,
                                       'preview': 'static_final_state', 'playback_verified': False},
                         'native_text_present': bool(page_counts['text'] + page_counts['tables'] + page_counts['charts']),
                         'native_shape_icon_present': bool(page_counts['shapes'] and
                             not any(page_counts[key] for key in ('text', 'images', 'tables', 'charts'))),
                         'confirmed_text_coverage': 'not_applicable' if fallback_image_hashes is not None and len(per_page) + 1 in fallback_image_hashes else not missing,
                         'missing_confirmed_fragments': missing,
                         'source_asset_hashes_preserved': not missing_assets if original_bytes else 'not_applicable',
                         'source_media_hashes_verified': not missing_assets, 'missing_source_asset_ids': missing_assets,
                         'source_media': [public_media_provenance(a) for a in required_assets],
                         'embedded_image_sha256': page_image_hashes})
    checks = {'page_count': len(prs.slides) == expected_pages, 'bounds': bool(bounds_ok),
              'fonts': bool(fonts_ok) if editable else 'not_applicable',
              'native_objects': all(page['native_shape_icon_present'] if page['number'] in (textless_icon_page_numbers or set())
                                    else page['native_text_present'] for page in per_page
                                    if native_page_numbers is None or page['number'] in native_page_numbers)
                                if editable else 'not_applicable',
              'confirmed_text_coverage': all(page['confirmed_text_coverage'] for page in per_page) and len(expected_texts) == len(per_page) if expected_texts is not None else 'not_applicable',
              'image_decode': images_ok, 'visual_fidelity': 'not_applicable'}
    checks['source_media_sha256'] = all(p['source_media_hashes_verified'] for p in per_page) if expected_source_assets is not None else 'not_applicable'
    if fallback_image_hashes is not None:
        native_reports = [page for page in per_page if page['number'] in (native_page_numbers or set())]
        checks['confirmed_text_coverage'] = all(page['confirmed_text_coverage'] is True for page in native_reports) if native_reports else 'not_applicable'
        exact = True
        for number, expected_hash in fallback_image_hashes.items():
            slide = prs.slides[number - 1]
            exact &= (len(slide.shapes) == 1 and slide.shapes[0].shape_type == 13
                      and slide.shapes[0].left == slide.shapes[0].top == 0
                      and slide.shapes[0].width == prs.slide_width and slide.shapes[0].height == prs.slide_height
                      and hashlib.sha256(slide.shapes[0].image.blob).hexdigest() == expected_hash)
        checks['confirmed_image_fallbacks'] = bool(exact)
    original_bytes = all(a.get('original_bytes_embedded', True) for assets in expected_source_assets or [] for a in assets)
    checks['original_source_media_sha256'] = all(p['source_asset_hashes_preserved'] is True for p in per_page) if expected_source_assets is not None and original_bytes else 'not_applicable'
    media_quality = inspect_presentation_media(prs)
    return {'passed': all(v is not False for v in checks.values()), 'checks': checks, 'object_counts': counts, 'pages': per_page,
            'media_quality': media_quality, 'warnings': media_quality['warnings'],
            'page_count': len(prs.slides), 'fidelity': 'unverified; structural QA is not visual fidelity approval'}
