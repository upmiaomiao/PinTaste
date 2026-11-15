"""Export typography: explicit script faces and evidence-based semantic sizes.

Source TraceSpecs are immutable. Size groups require an explicit semantic role,
apart from native table headers/body and chart labels whose roles are known.
An overflow in one box constrains only its own role/variant, never the whole deck.
"""
from copy import deepcopy
import math
import re
from statistics import median

LATIN_FONT = 'Arial'
CJK_FONT = '微软雅黑'
# Share of a role group's boxes that must overflow before the group size drops.
OVERFLOW_GROUP_RATIO = 0.25
_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_C = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
_ROLE = re.compile(r'^[A-Za-z][A-Za-z0-9_ -]{0,63}$')


def set_script_typefaces(properties):
    """Write real a:latin/a:ea/a:cs on an rPr, defRPr or endParaRPr node."""
    from pptx.oxml.xmlchemy import OxmlElement
    for script, family in (('latin', LATIN_FONT), ('ea', CJK_FONT), ('cs', LATIN_FONT)):
        tag = '{' + _A + '}' + script
        node = properties.find(tag)
        if node is None:
            node = OxmlElement('a:' + script)
            # These three font elements precede symbol/hyperlink properties in
            # CT_TextCharacterProperties, rather than appending after them.
            later = {'sym', 'hlinkClick', 'hlinkMouseOver', 'rtl', 'extLst'}
            if script == 'latin':
                later.update(('ea', 'cs'))
            elif script == 'ea':
                later.add('cs')
            before = next((child for child in properties if child.tag.rsplit('}', 1)[-1] in later), None)
            if before is None:
                properties.append(node)
            else:
                properties.insert(properties.index(before), node)
        node.set('typeface', family)
    return properties


def _font_points(font, default=18):
    value = font.get('size', default)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    value *= .75 if font.get('size_unit', 'pt') == 'px' else 1
    return float(value) if 1 <= value <= 160 else None


def _role(element, default=None):
    role = element.get('typography_role', element.get('text_role', default))
    variant = element.get('typography_variant', 'default')
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        return None
    if not isinstance(variant, str) or not _ROLE.fullmatch(variant):
        return None
    return role.strip().lower().replace(' ', '_'), variant.strip().lower().replace(' ', '_')


def normalize_trace_typography(traces):
    """Return (new_traces, safe_audit) with equal role/variant sizes across pages.

    The median measured source size retains the template scale. Real font metrics
    may lower the group's size only if its own boxes demonstrably overflow. Text,
    line breaks, geometry, bold, emphasis and colors are never rewritten.
    """
    from .wuyu_text_fit import fit_native_text
    normalized = deepcopy(traces)
    groups, records = {}, []
    for page, trace in enumerate(normalized, 1):
        for index, element in enumerate(trace.get('elements', [])):
            kind = element.get('type')
            entries = []
            if kind == 'text':
                font = element.setdefault('font', {})
                entries.append((font, _role(element), [element]))
            elif kind == 'table' and element.get('rows'):
                style = element.setdefault('table_style', {})
                rows = element['rows']
                row_height = element['bounds'][3] / len(rows)
                col_width = element['bounds'][2] / len(rows[0])
                for key, role, selected in (('header_font', 'table_header', rows[:1]), ('font', 'table_body', rows[1:])):
                    font = style.setdefault(key, deepcopy(style.get('font', {})))
                    boxes = [{'text': '' if value is None else str(value),
                              'bounds': [0, 0, col_width, row_height], 'wrap': True,
                              'margins': [9.6, 4.8, 9.6, 4.8]}
                             for row in selected for value in row]
                    key = _role(element, role)
                    if key and ('typography_role' in element or 'text_role' in element):
                        key = (key[0] + '_' + role, key[1])
                    entries.append((font, key, boxes))
            elif kind == 'chart':
                font = element.setdefault('chart_style', {}).setdefault('font', {})
                entries.append((font, _role(element, 'chart_label'), []))
            for font, key, boxes in entries:
                font.update(family=LATIN_FONT, latin_family=LATIN_FONT, east_asian_family=CJK_FONT,
                            complex_script_family=LATIN_FONT)
                # Preserve an explicit small source font; do not enlarge it
                # by merging it into a normal-size semantic role group.
                if key and (size := _font_points(font)) is not None and size >= 6:
                    groups.setdefault(key, []).append({'font': font, 'size': size, 'boxes': boxes,
                                                       'page': page, 'element_index': index})
    for key, entries in groups.items():
        base = math.floor(median(item['size'] for item in entries) * 100) / 100
        # A box that cannot hold the group size used to pull the whole group down
        # to its own size: one long paragraph shrank hundreds of body boxes on
        # every page to 6-8pt. Equal size is kept for the group, and a box that
        # genuinely overflows is fitted on its own at export (fit_native_text).
        # The group size itself is lowered only when overflow is widespread, i.e.
        # the common size is really too large, not when a single box is long.
        measured, overflow_sizes, unverified = 0, [], 0
        for item in entries:
            for box in item['boxes']:
                probe = {**box, 'font': {**item['font'], 'size': base, 'size_unit': 'pt'}}
                override, fit = fit_native_text(probe)
                measured += 1
                if fit and fit['status'] == 'shrunk_to_own_box':
                    overflow_sizes.append(override['size'])
                elif fit:
                    unverified += 1
        overflow_boxes = len(overflow_sizes)
        target = base
        if measured >= 4 and overflow_boxes / measured > OVERFLOW_GROUP_RATIO:
            # Widespread overflow: the common size really is too large. Use the
            # size most overflowing boxes can take, not the worst single box.
            target = min(base, sorted(overflow_sizes)[len(overflow_sizes) // 2])
        elif measured < 4 and overflow_sizes:
            # Tiny group: no population to judge by, keep the old safe behaviour.
            target = min(base, *overflow_sizes)
        for item in entries:
            item['font'].update(size=target, size_unit='pt')
        records.append({'role': key[0], 'variant': key[1], 'members': len(entries),
                        'base_size_pt': base, 'size_pt': target,
                        'verified_overflow_boxes': overflow_boxes,
                        'font_unavailable_boxes': unverified})
    return normalized, {'version': 1, 'latin_font': LATIN_FONT, 'east_asian_font': CJK_FONT,
                         'policy': 'equal_semantic_role_variant_size_measured_overflow_only',
                         'groups': records, 'content_changed': False, 'geometry_changed': False}


def _normalize_text_body(body):
    from pptx.oxml.xmlchemy import OxmlElement
    count = 0
    for paragraph in body.findall('{' + _A + '}p'):
        ppr = paragraph.find('{' + _A + '}pPr')
        if ppr is None:
            ppr = OxmlElement('a:pPr')
            paragraph.insert(0, ppr)
        default = ppr.find('{' + _A + '}defRPr')
        if default is None:
            default = OxmlElement('a:defRPr')
            extension = ppr.find('{' + _A + '}extLst')
            ppr.append(default) if extension is None else ppr.insert(ppr.index(extension), default)
        end = paragraph.find('{' + _A + '}endParaRPr')
        if end is None:
            paragraph.append(OxmlElement('a:endParaRPr'))
    for node in body.iter():
        if node.tag in {'{' + _A + '}' + kind for kind in ('rPr', 'defRPr', 'endParaRPr')}:
            set_script_typefaces(node)
            count += 1
    return count


def apply_chart_typography(chart, font=None):
    """Give native chart labels real script fonts, including generated axis text."""
    from pptx.oxml.xmlchemy import OxmlElement
    from .wuyu_native_charts import normalize_native_axis_ids
    normalize_native_axis_ids(chart)
    root = chart._chartSpace
    existing = root.find('{' + _C + '}txPr')
    if font is None and existing is not None:
        default = next((node for node in existing.iter() if node.tag == '{' + _A + '}defRPr'), None)
        if default is not None and default.get('sz', '').isdigit():
            font = {'size': int(default.get('sz')) / 100}
    size = _font_points(font or {}) or 18
    # Explicit defaults on root and existing axes/legend/labels avoid chart theme
    # faces; title size is retained when it already has an explicit text style.
    owners = [root] + [node for node in root.iter() if node.tag in {
        '{' + _C + '}' + kind for kind in ('catAx', 'valAx', 'dateAx', 'serAx', 'legend', 'dLbls', 'dLbl')}]
    for owner in owners:
        body = owner.find('{' + _C + '}txPr')
        if body is None:
            body = OxmlElement('c:txPr')
            body.append(OxmlElement('a:bodyPr'))
            body.append(OxmlElement('a:lstStyle'))
            body.append(OxmlElement('a:p'))
            # Use the per-owner OOXML sequence. In particular chartSpace txPr
            # precedes externalData, and axis txPr precedes crossAx; appending
            # after either can make PowerPoint repair an otherwise valid deck.
            later = {
                'chartSpace': {'externalData', 'printSettings', 'userShapes', 'extLst'},
                'legend': {'extLst'},
                'dLbls': {'dLblPos', 'showLegendKey', 'showVal', 'showCatName', 'showSerName',
                          'showPercent', 'showBubbleSize', 'separator', 'showLeaderLines', 'leaderLines', 'extLst'},
                'dLbl': {'dLblPos', 'showLegendKey', 'showVal', 'showCatName', 'showSerName',
                         'showPercent', 'showBubbleSize', 'separator', 'extLst'},
            }.get(owner.tag.rsplit('}', 1)[-1], {'crossAx', 'crosses', 'crossesAt', 'auto', 'lblAlgn',
                   'lblOffset', 'tickLblSkip', 'tickMarkSkip', 'noMultiLvlLbl', 'crossBetween',
                   'majorUnit', 'minorUnit', 'dispUnits', 'baseTimeUnit', 'majorTimeUnit', 'minorTimeUnit', 'extLst'})
            following = next((child for child in owner if child.tag.rsplit('}', 1)[-1] in later), None)
            owner.append(body) if following is None else owner.insert(owner.index(following), body)
        _normalize_text_body(body)
        for node in body.iter():
            if node.tag in ('{' + _A + '}defRPr', '{' + _A + '}rPr', '{' + _A + '}endParaRPr'):
                node.set('sz', str(round(size * 100)))
    for body in root.iter():
        if body.tag in ('{' + _C + '}rich', '{' + _C + '}txPr'):
            _normalize_text_body(body)


def normalize_presentation_typography(presentation):
    """Final OOXML safety net covering runs, paragraphs, tables, groups and charts."""
    counts = {'text_properties': 0, 'charts': 0}
    def visit(shapes):
        for shape in shapes:
            if shape.shape_type == 6:  # groups can contain both text and charts
                visit(shape.shapes)
            if shape.has_text_frame:
                counts['text_properties'] += _normalize_text_body(shape.text_frame._txBody)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        counts['text_properties'] += _normalize_text_body(cell.text_frame._txBody)
            if shape.has_chart:
                apply_chart_typography(shape.chart)
                counts['charts'] += 1
    for slide in presentation.slides:
        visit(slide.shapes)
    return counts
