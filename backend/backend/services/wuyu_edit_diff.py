"""Typed diff between two versions of an editable PPTX (manual edits made in the cloud editor).

``diff_decks`` turns "slide 3 changed" into compact, typed changes ("title font 28 -> 36",
"picture replaced", "chart type changed") that the design-habit store can learn from.

Pure and local: python-pptx only, no model call and no network. Odd decks never raise; a shape
that cannot be read is skipped and the rest of the slide is still compared.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io

MAX_PAGES = 30
MAX_CHANGES_PER_PAGE = 40
TEXT_LIMIT = 200
NAME_LIMIT = 60
CELL_LIMIT = 60
MAX_GROUP_DEPTH = 3

EMU_PER_PT = 12700
# Geometry tolerance. The editor re-serialises offsets/extents with small rounding differences; the page
# fingerprint (wuyu_office._shape_fingerprint) buckets them by 2000 EMU (~0.16pt) for that reason. A bucket
# still flips when a value sits on its edge, so the diff uses a full point: several times that noise, and
# far below any drag or resize a person makes on purpose.
GEOMETRY_TOLERANCE = EMU_PER_PT
ROTATION_TOLERANCE = 1.0          # degrees; the fingerprint rounds rotation to whole degrees
# Unmatched shapes of the same kind pair up by geometry only when they are this close (centre distance plus
# size difference), unless their content is identical. Further apart they count as removed + added.
NEAREST_LIMIT = 914400            # one inch
VALUE_DIGITS = 9                  # chart values are compared after rounding away float serialisation noise

_TEXT_ATTRS = ('size', 'color', 'name', 'ea', 'bold', 'italic')


# ---------------------------------------------------------------- reading
def _open(source):
    from pptx import Presentation
    if isinstance(source, (bytes, bytearray)):
        return Presentation(io.BytesIO(bytes(source)))
    return Presentation(str(source))


def _clip(value, limit=TEXT_LIMIT):
    value = '' if value is None else str(value)
    return value if len(value) <= limit else value[:limit]


def _pt(emu):
    return round(emu / EMU_PER_PT, 1)


def _quantise(size):
    # The editor saves sizes in 0.5pt steps (32.71 -> 32.5); same quantisation as the page fingerprint.
    return None if size is None else round(size.pt * 2) / 2


def _color(font):
    try:
        color = font.color
        if color is None or color.type is None:
            return None
        try:
            return str(color.rgb)
        except AttributeError:
            theme = color.theme_color
            return 'theme:' + (getattr(theme, 'name', None) or str(theme)) if theme is not None else None
    except Exception:
        return None


def _east_asian(run):
    try:
        from pptx.oxml.ns import qn
        rpr = run._r.find(qn('a:rPr'))
        node = rpr.find(qn('a:ea')) if rpr is not None else None
        return node.get('typeface') if node is not None else None
    except Exception:
        return None


def _runs(shape):
    """[(text, attrs)] for every run with text; attrs follow ``_TEXT_ATTRS``."""
    result = []
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if not run.text:
                continue
            font = run.font
            result.append((run.text, (_quantise(font.size), _color(font), font.name, _east_asian(run),
                                      font.bold, font.italic)))
    return result


def _dominant(runs, index):
    weights = Counter()
    for text, attrs in runs:
        weights[attrs[index]] += len(text)
    return weights.most_common(1)[0][0] if weights else None


def _image_hash(shape):
    try:
        return hashlib.sha256(shape.image.blob).hexdigest()
    except Exception:
        return None


def _family(shape, image):
    """Structural kind used for matching; stable when a text box is emptied or filled."""
    if getattr(shape, 'has_chart', False):
        return 'chart'
    if getattr(shape, 'has_table', False):
        return 'table'
    if image is not None:
        return 'picture'
    try:
        if shape.shape_type == 6:
            return 'group'
    except Exception:
        pass
    return 'shape'


def _placeholder_role(shape):
    try:
        if not shape.is_placeholder:
            return None
        name = getattr(shape.placeholder_format.type, 'name', '') or ''
    except Exception:
        return None
    if 'TITLE' in name:           # TITLE, CENTER_TITLE, SUBTITLE, VERTICAL_TITLE
        return 'title'
    if name in {'BODY', 'OBJECT', 'VERTICAL_BODY', 'VERTICAL_OBJECT'}:
        return 'body'
    return None


def _chart_info(shape):
    chart = shape.chart
    try:
        kind = getattr(chart.chart_type, 'name', None) or str(chart.chart_type)
    except Exception:
        kind = None
    categories, series = [], []
    try:
        categories = [[str(c) for c in plot.categories] for plot in chart.plots]
    except Exception:
        categories = []
    try:
        for item in chart.series:
            values = [round(v, VALUE_DIGITS) if isinstance(v, float) else v for v in item.values]
            series.append((str(item.name), values))
    except Exception:
        series = []
    return {'type': kind, 'categories': categories, 'series': series}


def _info(shape):
    """Everything the diff needs from one shape, read once and defensively."""
    image = _image_hash(shape)
    info = {'id': None, 'name': '', 'family': _family(shape, image), 'image': image, 'geometry': None,
            'rotation': 0.0, 'text': None, 'runs': [], 'table': None, 'chart': None, 'children': None,
            'placeholder_role': _placeholder_role(shape)}
    try:
        info['id'] = shape.shape_id
        info['name'] = shape.name or ''
    except Exception:
        pass
    try:
        box = (shape.left, shape.top, shape.width, shape.height)
        info['geometry'] = None if any(v is None for v in box) else tuple(int(v) for v in box)
        info['rotation'] = float(shape.rotation or 0)
    except Exception:
        pass
    try:
        if shape.has_text_frame:
            info['text'] = shape.text_frame.text
            info['runs'] = _runs(shape)
    except Exception:
        pass
    try:
        if info['family'] == 'table':
            info['table'] = [[cell.text for cell in row.cells] for row in shape.table.rows]
        elif info['family'] == 'chart':
            info['chart'] = _chart_info(shape)
        elif info['family'] == 'group':
            info['children'] = _collect(shape.shapes)
    except Exception:
        pass
    return info


def _collect(shapes):
    result = []
    for shape in shapes:
        try:
            result.append(_info(shape))
        except Exception:
            continue          # an unreadable shape must not hide the rest of the slide
    return result


def _kind(info):
    if info['family'] in {'picture', 'chart', 'table', 'group'}:
        return info['family']
    return 'text' if (info['text'] or '').strip() else 'other'


# ---------------------------------------------------------------- matching
def _same_content(a, b):
    if a['family'] == 'picture':
        return a['image'] == b['image']
    if a['family'] == 'chart':
        return a['chart'] == b['chart']
    if a['family'] == 'table':
        return a['table'] == b['table']
    return bool((a['text'] or '').strip()) and a['text'] == b['text']


def _distance(a, b):
    if a['geometry'] is None or b['geometry'] is None:
        return None
    (al, at, aw, ah), (bl, bt, bw, bh) = a['geometry'], b['geometry']
    centre = abs((al + aw / 2) - (bl + bw / 2)) + abs((at + ah / 2) - (bt + bh / 2))
    return centre + abs(aw - bw) + abs(ah - bh)


def _near(a, b):
    distance = _distance(a, b)
    return distance is not None and distance <= NEAREST_LIMIT


def _match(before, after):
    """Pair shapes of two versions of one slide.

    1. same shape_id and same kind, confirmed by name, content or position (an id alone is not trusted:
       an editor may renumber shapes);
    2. same name and same kind, when that name is unambiguous on both sides;
    3. same kind and identical content, else nearest geometry within ``NEAREST_LIMIT``.
    Returns ``(pairs, removed, added)``.
    """
    pairs, left, right = [], list(range(len(before))), list(range(len(after)))

    def take(i, j):
        pairs.append((before[i], after[j]))
        left.remove(i)
        right.remove(j)

    by_id = {}
    for j in right:
        by_id.setdefault(after[j]['id'], []).append(j)
    for i in list(left):
        a = before[i]
        candidates = [j for j in by_id.get(a['id'], []) if j in right] if a['id'] is not None else []
        if len(candidates) != 1:
            continue
        b = after[candidates[0]]
        if a['family'] == b['family'] and (a['name'] == b['name'] or _same_content(a, b) or _near(a, b)):
            take(i, candidates[0])

    left_names = Counter((before[i]['family'], before[i]['name']) for i in left)
    right_names = Counter((after[j]['family'], after[j]['name']) for j in right)
    for i in list(left):
        key = (before[i]['family'], before[i]['name'])
        if not key[1] or left_names[key] != 1 or right_names[key] != 1:
            continue
        take(i, next(j for j in right if (after[j]['family'], after[j]['name']) == key))

    scored = []
    for i in left:
        for j in right:
            a, b = before[i], after[j]
            if a['family'] != b['family']:
                continue
            distance = _distance(a, b)
            if _same_content(a, b):
                scored.append((0, distance or 0, i, j))
            elif distance is not None and distance <= NEAREST_LIMIT:
                scored.append((1, distance, i, j))
    for _, _, i, j in sorted(scored):
        if i in left and j in right:
            take(i, j)
    order = {id(info): index for index, info in enumerate(before)}
    pairs.sort(key=lambda pair: order[id(pair[0])])
    return pairs, [before[i] for i in left], [after[j] for j in right]


# ---------------------------------------------------------------- per-shape changes
def _roles(infos):
    """Role guess per shape (by identity): placeholder type first, then font-size rank on the slide."""
    sizes = {}
    for info in infos:
        size = _dominant(info['runs'], 0)
        if size is not None and (info['text'] or '').strip():
            sizes[id(info)] = size
    largest = max(sizes.values()) if sizes else None
    holders = sum(1 for size in sizes.values() if size == largest)
    roles = {}
    for info in infos:
        role = info['placeholder_role']
        if role is None:
            size = sizes.get(id(info))
            if size is None or size != largest:
                role = 'body'
            elif len(sizes) == 1:
                role = 'title' if size >= 28 else 'body'
            else:
                # The largest text is a title only when it stands out, not when most boxes share that size.
                role = 'title' if holders <= 2 and holders < len(sizes) else 'body'
        roles[id(info)] = role
    return roles


def _text_changes(a, b, base):
    changes = []
    if a['text'] is None and b['text'] is None:
        return changes
    old, new = a['text'] or '', b['text'] or ''
    if old != new:
        changes.append(dict(base, type='text_changed', before=_clip(old), after=_clip(new),
                            before_chars=len(old), after_chars=len(new)))
    found = {}
    old_chars = [attrs for text, attrs in a['runs'] for _ in text]
    new_chars = [attrs for text, attrs in b['runs'] for _ in text]
    same_text = ''.join(t for t, _ in a['runs']) == ''.join(t for t, _ in b['runs'])
    if same_text and old_chars:
        # Same characters: compare formatting per character, so a re-split of runs by the editor is not
        # an edit and a partial change (one word made bold) is still seen.
        for index, attr in enumerate(_TEXT_ATTRS):
            moves = Counter((o[index], n[index]) for o, n in zip(old_chars, new_chars) if o[index] != n[index])
            if moves:
                (source, target), _count = moves.most_common(1)[0]
                found[attr] = (source, target, sum(moves.values()), len(old_chars))
    elif a['runs'] and b['runs']:
        # Text was rewritten: only the prevailing formatting of the box can be compared.
        for index, attr in enumerate(_TEXT_ATTRS):
            source, target = _dominant(a['runs'], index), _dominant(b['runs'], index)
            if source != target:
                found[attr] = (source, target, None, None)
    for attr, (source, target, count, total) in found.items():
        if attr in {'bold', 'italic'}:
            # None means "inherited"; inherited -> False (and back) is how editors spell the same thing.
            if bool(source) == bool(target):
                continue
            change = dict(base, type=attr, to=bool(target))
            change['from'] = bool(source)
        elif target is None:
            # Explicit value -> inherited (formatting cleared, typically by retyping the text) says nothing
            # about what the user wants instead, so it is not reported.
            continue
        else:
            kind = {'size': 'font_size', 'color': 'font_color', 'name': 'font_name', 'ea': 'font_name'}[attr]
            change = dict(base, type=kind, to=target)
            change['from'] = source
            if attr in {'name', 'ea'}:
                change['script'] = 'latin' if attr == 'name' else 'east_asian'
        if count is not None and count < total:
            change['chars'] = count       # only part of the text was restyled
        changes.append(change)
    return changes


def _geometry_changes(a, b, base):
    changes = []
    if a['geometry'] is not None and b['geometry'] is not None:
        (al, at, aw, ah), (bl, bt, bw, bh) = a['geometry'], b['geometry']
        if abs(al - bl) > GEOMETRY_TOLERANCE or abs(at - bt) > GEOMETRY_TOLERANCE:
            change = dict(base, type='moved', to=[_pt(bl), _pt(bt)], unit='pt')
            change['from'] = [_pt(al), _pt(at)]
            changes.append(change)
        if abs(aw - bw) > GEOMETRY_TOLERANCE or abs(ah - bh) > GEOMETRY_TOLERANCE:
            change = dict(base, type='resized', to=[_pt(bw), _pt(bh)], unit='pt')
            change['from'] = [_pt(aw), _pt(ah)]
            changes.append(change)
    turn = abs(a['rotation'] - b['rotation']) % 360
    if min(turn, 360 - turn) >= ROTATION_TOLERANCE:
        change = dict(base, type='rotated', to=round(b['rotation'], 1))
        change['from'] = round(a['rotation'], 1)
        changes.append(change)
    return changes


def _chart_changes(a, b, base):
    changes = []
    old, new = a['chart'] or {}, b['chart'] or {}
    if old.get('type') != new.get('type') and old.get('type') and new.get('type'):
        change = dict(base, type='chart_type_changed', to=new['type'])
        change['from'] = old['type']
        changes.append(change)
    if old.get('categories') != new.get('categories') or old.get('series') != new.get('series'):
        def points(chart):
            return sum(len(values) for _, values in chart.get('series') or [])
        changes.append(dict(base, type='chart_data_changed',
                            before_series=len(old.get('series') or []), after_series=len(new.get('series') or []),
                            before_points=points(old), after_points=points(new),
                            categories_changed=old.get('categories') != new.get('categories')))
    return changes


def _table_changes(a, b, base):
    old, new = a['table'] or [], b['table'] or []
    if old == new:
        return []
    def width(rows):
        return max((len(row) for row in rows), default=0)
    changed, samples = 0, []
    for r in range(max(len(old), len(new))):
        for c in range(max(width(old), width(new))):
            before = old[r][c] if r < len(old) and c < len(old[r]) else None
            after = new[r][c] if r < len(new) and c < len(new[r]) else None
            if before != after:
                changed += 1
                if len(samples) < 3:
                    samples.append({'row': r + 1, 'col': c + 1, 'before': _clip(before, CELL_LIMIT),
                                    'after': _clip(after, CELL_LIMIT)})
    return [dict(base, type='table_changed', before_size=[len(old), width(old)], after_size=[len(new), width(new)],
                 cells_changed=changed, samples=samples)]


def _diff_shapes(before, after, depth=0):
    changes = []
    pairs, removed, added = _match(before, after)
    roles = _roles(before)
    for a, b in pairs:
        try:
            base = {'shape': _clip(b['name'] or a['name'], NAME_LIMIT), 'kind': _kind(b)}
            text_base = dict(base, role=roles.get(id(a), 'body'))
            changes.extend(_geometry_changes(a, b, base))
            changes.extend(_text_changes(a, b, text_base))
            if a['family'] == 'picture' and a['image'] != b['image']:
                change = dict(base, type='picture_replaced', to=(b['image'] or '')[:12])
                change['from'] = (a['image'] or '')[:12]
                changes.append(change)
            elif a['family'] == 'chart':
                changes.extend(_chart_changes(a, b, base))
            elif a['family'] == 'table':
                changes.extend(_table_changes(a, b, base))
            elif a['family'] == 'group' and depth < MAX_GROUP_DEPTH:
                inner = _diff_shapes(a['children'] or [], b['children'] or [], depth + 1)
                changes.extend(dict(change, group=base['shape']) for change in inner)
        except Exception:
            continue
    for kind, items in (('shape_removed', removed), ('shape_added', added)):
        for info in items:
            try:
                change = {'type': kind, 'shape': _clip(info['name'], NAME_LIMIT), 'kind': _kind(info)}
                if (info['text'] or '').strip():
                    change['text'] = _clip(info['text'], CELL_LIMIT)
                changes.append(change)
            except Exception:
                continue
    return changes


def _notes(slide):
    try:
        return slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ''
    except Exception:
        return ''


def _diff_slide(before, after):
    changes = _diff_shapes(_collect(before.shapes), _collect(after.shapes))
    old, new = _notes(before), _notes(after)
    if old != new:
        changes.append({'type': 'notes_changed', 'before': _clip(old), 'after': _clip(new),
                        'before_chars': len(old), 'after_chars': len(new)})
    return changes


# ---------------------------------------------------------------- public
def diff_decks(before, after, pages=None):
    """Typed changes between two versions of a deck.

    ``before`` / ``after`` are PPTX bytes or a path. ``pages`` (1-based) limits the comparison, e.g. to
    the pages the fingerprint already found changed. Returns ``[{'page': n, 'changes': [...]}]`` for pages
    with at least one change (at most ``MAX_PAGES`` pages, ``MAX_CHANGES_PER_PAGE`` changes each; a page
    that was cut carries ``'truncated': True``). A different slide count gives one ``structure_changed``
    entry with ``page`` None. Unreadable input gives ``[]``; this function does not raise.
    """
    try:
        old_slides, new_slides = list(_open(before).slides), list(_open(after).slides)
    except Exception:
        return []
    if len(old_slides) != len(new_slides):
        return [{'page': None, 'changes': [{'type': 'structure_changed', 'before_slides': len(old_slides),
                                           'after_slides': len(new_slides)}]}]
    if pages is None:
        numbers = list(range(1, len(old_slides) + 1))
    else:
        try:
            numbers = sorted({int(n) for n in pages if isinstance(n, int) and 1 <= n <= len(old_slides)})
        except TypeError:
            numbers = []
    result = []
    for number in numbers:
        if len(result) >= MAX_PAGES:
            break
        try:
            changes = _diff_slide(old_slides[number - 1], new_slides[number - 1])
        except Exception:
            continue
        if not changes:
            continue
        entry = {'page': number, 'changes': changes[:MAX_CHANGES_PER_PAGE]}
        if len(changes) > MAX_CHANGES_PER_PAGE:
            entry['truncated'] = True
        result.append(entry)
    return result
