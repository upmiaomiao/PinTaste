"""Validated, explicit TraceSpec styling for the existing python-pptx builder.

Geometry is in input-image pixels at 96 dpi. Font and stroke widths default to
points; a declared ``size_unit/width_unit=px`` is converted at 72/96.
This preserves supported OOXML properties, not a visual-fidelity guarantee.
"""
import math
import re
from .wuyu_errors import PipelineError
from .wuyu_typography import set_script_typefaces

SHAPES = {'rect', 'round_rect', 'ellipse', 'triangle', 'right_triangle', 'diamond',
          'parallelogram', 'trapezoid', 'hexagon', 'chevron', 'pentagon',
          'right_arrow', 'left_arrow', 'up_arrow', 'down_arrow',
          'left_right_arrow', 'up_down_arrow', 'polygon', 'line'}
ALIGNMENTS = {'left', 'center', 'right', 'justify'}
VERTICAL_ALIGNMENTS = {'top', 'middle', 'bottom'}
DASHES = {'solid': 'solid', 'dash': 'dash', 'dot': 'sysDot',
          'dash_dot': 'dashDot', 'long_dash': 'lgDash'}
ARROWS = {'none', 'triangle', 'stealth', 'diamond', 'oval', 'arrow'}
SHAPE_ALIASES = {'rectangle': 'rect', 'roundedRectangle': 'round_rect', 'rounded_rectangle': 'round_rect',
                 'roundRect': 'round_rect', 'rounded_rect': 'round_rect', 'oval': 'ellipse', 'circle': 'ellipse',
                 'rightArrow': 'right_arrow', 'leftArrow': 'left_arrow', 'upArrow': 'up_arrow',
                 'downArrow': 'down_arrow', 'leftRightArrow': 'left_right_arrow', 'upDownArrow': 'up_down_arrow'}


def normalize_trace_styles(trace):
    """Conservative syntax normalization, recorded rather than silently guessed.

    Ambiguous points remain local pixels as the documented schema specifies.
    Font values never change units without an explicit px field or unit marker.
    """
    corrections = []
    def record(index, field, before, after, rule):
        corrections.append({'element_index': index, 'field': field, 'original': before,
                            'normalized': after, 'rule': rule})
    def normalize_opacity(value, index, field):
        if isinstance(value, dict):
            opacity = value.get('opacity')
            if isinstance(opacity, (int, float)) and not isinstance(opacity, bool) and 1 < opacity <= 100:
                value['opacity'] = opacity / 100
                record(index, field + '.opacity', opacity, value['opacity'], 'opacity_above_1_is_percent')
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    normalize_opacity(child, index, field + '.' + key)
        elif isinstance(value, list):
            for n, child in enumerate(value):
                normalize_opacity(child, index, field + f'[{n}]')
    for index, el in enumerate(trace.get('elements', [])):
        if not isinstance(el, dict):
            continue
        kind = el.get('shape')
        if isinstance(kind, str) and kind in SHAPE_ALIASES:
            el['shape'] = SHAPE_ALIASES[kind]
            record(index, 'shape', kind, el['shape'], 'documented_native_shape_alias')
        if 'stroke' in el and 'line' not in el:
            el['line'] = el['stroke']
            record(index, 'line', None, el['line'], 'stroke_is_line_alias')
        font = el.get('font')
        if isinstance(font, dict):
            if 'size_px' in font:
                font['size'], font['size_unit'] = font.pop('size_px'), 'px'
                record(index, 'font.size_unit', None, 'px', 'explicit_size_px_field')
            unit = font.get('size_unit', font.get('unit'))
            aliases = {'pixel': 'px', 'pixels': 'px', 'points': 'pt', 'point': 'pt'}
            if isinstance(unit, str) and unit in aliases:
                font['size_unit'] = aliases[unit]
                record(index, 'font.size_unit', unit, font['size_unit'], 'explicit_font_unit_alias')
        if 'horizontal_align' in el and 'align' not in el:
            el['align'] = el['horizontal_align']
            record(index, 'align', None, el['align'], 'horizontal_align_alias')
        if el.get('vertical_align') == 'center':
            el['vertical_align'] = 'middle'
            record(index, 'vertical_align', 'center', 'middle', 'vertical_center_alias')
        box, vertices = el.get('bounds'), el.get('points')
        if el.get('shape') in ('polygon', 'line') and isinstance(box, list) and len(box) == 4 and isinstance(vertices, list) and vertices:
            numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box)
            numeric &= all(isinstance(vertex, list) and len(vertex) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vertex) for vertex in vertices)
            if numeric:
                x, y, w, h = box
                local = all(0 <= vx <= w and 0 <= vy <= h for vx, vy in vertices)
                absolute = all(x <= vx <= x + w and y <= vy <= y + h for vx, vy in vertices)
                declared_absolute = el.get('points_coordinate_system') in ('canvas', 'canvas_px', 'absolute')
                if absolute and (declared_absolute or not local):
                    el['points'] = [[vx - x, vy - y] for vx, vy in vertices]
                    el['points_coordinate_system'] = 'local_px'
                    record(index, 'points', vertices, el['points'], 'unambiguous_canvas_points_to_local_pixels')
        if el.get('type') == 'image' and el.get('asset_id') == 'approved_final_slide' and el.get('source_crop'):
            del el['asset_id']
            record(index, 'asset_id', 'approved_final_slide', None, 'approved_image_source_crop_is_not_external_asset')
        if el.get('shape') == 'rect' and isinstance(el.get('dash'), list) and isinstance(box, list) and len(box) == 4:
            x, y, w, h = box
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box) and 0 < min(w, h) <= 4:
                before = {'shape': 'rect', 'bounds': list(box), 'dash': el['dash']}
                el['shape'] = 'line'
                el['bounds'] = [x, y + h / 2, w, 0] if w >= h else [x + w / 2, y, 0, h]
                fill = el.get('fill', '#222222')
                el['line'] = {'color': fill if isinstance(fill, str) else fill.get('color', '#222222'),
                              'width': min(w, h), 'width_unit': 'px', 'dash': el.pop('dash')}
                record(index, 'shape', before, {'shape': 'line', 'bounds': el['bounds'], 'line': el['line']}, 'thin_dashed_rectangle_is_native_centerline')
        normalize_opacity(el, index, 'element')
    normalize_opacity(trace.get('background', {}), None, 'background')
    return corrections


def invalid(message):
    raise PipelineError('invalid_trace', message)


def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        invalid('样式数值必须为有限数字。')
    if (low is not None and value < low) or (high is not None and value > high):
        invalid('样式数值超出支持范围。')
    return value


def rgb(value):
    if not isinstance(value, str) or not re.fullmatch(r'#?[0-9a-fA-F]{6}', value):
        invalid('颜色必须为六位RGB。')
    return value.lstrip('#').upper()


def points(value, unit='pt'):
    if unit not in ('pt', 'px'):
        invalid('字体或线宽单位只支持pt或px。')
    return number(value) * (.75 if unit == 'px' else 1)


def validate_font(font):
    if not isinstance(font, dict):
        invalid('font必须为对象。')
    number(points(font.get('size', 18), font.get('size_unit', 'pt')), 1, 160)
    if not isinstance(font.get('family', 'Noto Sans SC'), str) or not font.get('family', 'Noto Sans SC').strip() or not 1 <= len(font.get('family', 'Noto Sans SC')) <= 128:
        invalid('字体名称无效。')
    rgb(font.get('color', '#222222'))
    number(font.get('opacity', 1), 0, 1)
    for key in ('bold', 'italic', 'underline'):
        if key in font and not isinstance(font[key], bool):
            invalid('字体粗体、斜体和下划线必须为布尔值。')


def validate_fill(fill):
    if fill is None or fill == 'none':
        return
    if isinstance(fill, str):
        rgb(fill)
        return
    if not isinstance(fill, dict):
        invalid('fill必须为RGB颜色、none或支持的填充对象。')
    kind = fill.get('type', 'solid')
    if kind == 'none':
        return
    if kind == 'solid':
        rgb(fill.get('color', '#FFFFFF'))
        number(fill.get('opacity', 1), 0, 1)
    elif kind == 'linear_gradient':
        number(fill.get('angle', 0), 0, 360)
        stops = fill.get('stops')
        if not isinstance(stops, list) or not 2 <= len(stops) <= 16:
            invalid('线性渐变需要2至16个有序色标。')
        previous = -1
        for stop in stops:
            if not isinstance(stop, dict):
                invalid('渐变色标必须为对象。')
            offset = number(stop.get('offset'), 0, 1)
            if offset <= previous:
                invalid('渐变色标必须按offset严格递增。')
            previous = offset
            rgb(stop.get('color'))
            number(stop.get('opacity', 1), 0, 1)
        if stops[0]['offset'] != 0 or stops[-1]['offset'] != 1:
            invalid('渐变色标必须覆盖0至1。')
    else:
        invalid('仅支持无填充、纯色及线性渐变，未静默替换其他填充。')


def validate_line(line):
    if line is None or line == 'none':
        return
    if not isinstance(line, dict):
        invalid('line必须为对象或none。')
    rgb(line.get('color', '#222222'))
    number(points(line.get('width', 1), line.get('width_unit', 'pt')), 0, 48)
    number(line.get('opacity', 1), 0, 1)
    dash = line.get('dash', 'solid')
    if isinstance(dash, list):
        if not 2 <= len(dash) <= 16 or len(dash) % 2:
            invalid('自定义虚线需要有界的成对[dash_px,gap_px]列表。')
        for value in dash:
            number(value, .01, 1000)
        if line.get('width', 1) <= 0:
            invalid('自定义虚线必须有正线宽。')
    elif not isinstance(dash, str) or dash not in DASHES:
        invalid('线条虚线格式不受支持。')
    for key in ('begin_arrow', 'end_arrow'):
        if not isinstance(line.get(key, 'none'), str) or line.get(key, 'none') not in ARROWS:
            invalid('线条箭头格式不受支持。')


def validate_text_layout(el):
    validate_font(el.get('font', {}))
    if not isinstance(el.get('align', 'left'), str) or not isinstance(el.get('vertical_align', 'top'), str) or el.get('align', 'left') not in ALIGNMENTS or el.get('vertical_align', 'top') not in VERTICAL_ALIGNMENTS:
        invalid('文字水平或垂直对齐格式不受支持。')
    if 'wrap' in el and not isinstance(el['wrap'], bool):
        invalid('wrap必须为布尔值。')
    margins = el.get('margins', [0, 0, 0, 0])
    if not isinstance(margins, list) or len(margins) != 4:
        invalid('文字边距必须为[left,top,right,bottom]像素列表。')
    for margin in margins:
        number(margin, 0, 200)
    if margins[0] + margins[2] >= el['bounds'][2] or margins[1] + margins[3] >= el['bounds'][3]:
        invalid('文字边距不能覆盖整个文本框。')
    formats = el.get('paragraph_formats', [])
    if not isinstance(formats, list) or len(formats) > len(el['text'].split('\n')):
        invalid('段落格式数量超过文字段落数。')
    for fmt in formats:
        if not isinstance(fmt, dict) or not isinstance(fmt.get('align', el.get('align', 'left')), str) or fmt.get('align', el.get('align', 'left')) not in ALIGNMENTS:
            invalid('段落格式或水平对齐无效。')
        for key in ('space_before', 'space_after'):
            if key in fmt:
                number(fmt[key], 0, 200)
        if 'line_spacing' in fmt:
            number(fmt['line_spacing'], .5, 5)


def alpha(color_element, opacity):
    from pptx.oxml.xmlchemy import OxmlElement
    for child in list(color_element):
        if child.tag.endswith('}alpha'):
            color_element.remove(child)
    node = OxmlElement('a:alpha')
    node.set('val', str(round(opacity * 100000)))
    color_element.append(node)


def apply_fill(fill_format, fill):
    from pptx.dml.color import RGBColor
    from pptx.oxml.xmlchemy import OxmlElement
    validate_fill(fill)
    if fill is None or fill == 'none' or (isinstance(fill, dict) and fill.get('type') == 'none'):
        fill_format.background()
    elif isinstance(fill, str) or fill.get('type', 'solid') == 'solid':
        detail = {'color': fill} if isinstance(fill, str) else fill
        fill_format.solid()
        fill_format.fore_color.rgb = RGBColor.from_string(rgb(detail.get('color', '#FFFFFF')))
        alpha(fill_format.fore_color._color._srgbClr, detail.get('opacity', 1))
    else:
        fill_format.gradient()
        gradient = fill_format._fill._gradFill
        for child in list(gradient):
            gradient.remove(child)
        stops = OxmlElement('a:gsLst')
        for stop in fill['stops']:
            node = OxmlElement('a:gs'); node.set('pos', str(round(stop['offset'] * 100000)))
            color = OxmlElement('a:srgbClr'); color.set('val', rgb(stop['color']))
            alpha(color, stop.get('opacity', 1)); node.append(color); stops.append(node)
        gradient.append(stops)
        linear = OxmlElement('a:lin'); linear.set('ang', str(round(fill.get('angle', 0) % 360 * 60000))); linear.set('scaled', '1')
        gradient.append(linear)


def apply_line(shape, line):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt
    from pptx.oxml.xmlchemy import OxmlElement
    validate_line(line)
    if line is None or line == 'none' or line.get('width', 1) == 0:
        shape.line.fill.background()
        return
    shape.line.color.rgb = RGBColor.from_string(rgb(line.get('color', '#222222')))
    shape.line.width = Pt(points(line.get('width', 1), line.get('width_unit', 'pt')))
    alpha(shape.line.fill.fore_color._color._srgbClr, line.get('opacity', 1))
    node = shape.line._get_or_add_ln()
    for child in list(node):
        if child.tag.endswith(('}prstDash', '}custDash', '}headEnd', '}tailEnd')):
            node.remove(child)
    dash_value = line.get('dash', 'solid')
    if isinstance(dash_value, list):
        dash = OxmlElement('a:custDash')
        width_px = points(line.get('width', 1), line.get('width_unit', 'pt')) / .75
        for index in range(0, len(dash_value), 2):
            segment = OxmlElement('a:ds')
            segment.set('d', str(round(dash_value[index] / width_px * 100000)))
            segment.set('sp', str(round(dash_value[index + 1] / width_px * 100000)))
            dash.append(segment)
    else:
        dash = OxmlElement('a:prstDash'); dash.set('val', DASHES[dash_value])
    node.append(dash)
    for key, tag in (('begin_arrow', 'headEnd'), ('end_arrow', 'tailEnd')):
        arrow = OxmlElement('a:' + tag); arrow.set('type', line.get(key, 'none')); node.append(arrow)


def apply_font(target, font):
    from pptx.util import Pt
    from pptx.dml.color import RGBColor
    # A mixed Chinese/English run uses distinct DrawingML script faces. Setting
    # only Font.name silently assigns the same Latin face to every script.
    target.name = 'Arial'
    target.size = Pt(points(font.get('size', 18), font.get('size_unit', 'pt')))
    target.bold = font.get('bold', False)
    target.italic = font.get('italic', False)
    target.underline = font.get('underline', False)
    target.color.rgb = RGBColor.from_string(rgb(font.get('color', '#222222')))
    alpha(target._rPr.find('{http://schemas.openxmlformats.org/drawingml/2006/main}solidFill')[0], font.get('opacity', 1))
    set_script_typefaces(target._rPr)


def clear_theme_effects(shape):
    """Disable implicit PowerPoint theme shadows; never alter raster pixels."""
    from pptx.oxml.ns import qn
    from pptx.oxml.xmlchemy import OxmlElement
    properties = shape._element.find(qn('p:spPr'))
    if properties is not None:
        for tag in ('a:effectLst', 'a:effectDag'):
            for effect in list(properties.findall(qn(tag))):
                properties.remove(effect)
        properties.append(OxmlElement('a:effectLst'))
    style = shape._element.find(qn('p:style'))
    if style is not None:
        effect = style.find(qn('a:effectRef'))
        if effect is not None:
            effect.set('idx', '0')


def apply_text(shape, el):
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
    from pptx.util import Inches, Pt
    align = {'left': PP_ALIGN.LEFT, 'center': PP_ALIGN.CENTER, 'right': PP_ALIGN.RIGHT, 'justify': PP_ALIGN.JUSTIFY}
    anchor = {'top': MSO_ANCHOR.TOP, 'middle': MSO_ANCHOR.MIDDLE, 'bottom': MSO_ANCHOR.BOTTOM}
    frame = shape.text_frame
    # Reapply verbatim text after the original builder's optional bullet conversion.
    frame.text = el['text']
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.word_wrap = el.get('wrap', False)
    frame.vertical_anchor = anchor[el.get('vertical_align', 'top')]
    margins = el.get('margins', [0, 0, 0, 0])
    frame.margin_left, frame.margin_top, frame.margin_right, frame.margin_bottom = [Inches(v / 96) for v in margins]
    formats = el.get('paragraph_formats', [])
    for index, paragraph in enumerate(frame.paragraphs):
        fmt = formats[index] if index < len(formats) else {}
        paragraph.alignment = align[fmt.get('align', el.get('align', 'left'))]
        paragraph.space_before = Pt(fmt.get('space_before', 0))
        paragraph.space_after = Pt(fmt.get('space_after', 0))
        if 'line_spacing' in fmt:
            paragraph.line_spacing = float(fmt['line_spacing'])
        apply_font(paragraph.font, el.get('font', {}))
        for run in paragraph.runs:
            apply_font(run.font, el.get('font', {}))
