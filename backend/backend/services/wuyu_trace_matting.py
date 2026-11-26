"""Transparent surroundings for small traced graphics (op203).

A traced icon is an exact rectangular crop of the slide image, so it carries the page (or card)
colour around it and shows as a coloured square once placed over other shapes. This clears only the
region that is connected to the crop border AND within TOLERANCE of the border's dominant colour:
real page background. Light design elements (a pale pill behind "...", a light halo) differ from the
page colour by more than the tolerance and are kept: the method prefers leaving a little background
over eating part of the graphic.

The one-pixel outline next to the cleared area is anti-aliased: pixels still fairly close to the
background colour get partial alpha, and the background colour is taken out of them (colour
decontamination) so no light fringe shows on a dark slide. Pixels are never moved or resampled.
Enclosed areas of the background colour (the white inside a ring) are never touched. When the border
is not one colour (photo, gradient) or the result would remove almost everything or almost nothing,
the crop is left exactly as it was.
"""
from collections import Counter, deque

TOLERANCE = 22            # max per-channel difference from the background colour for "background"
EDGE_BAND = 3             # outline pixels within TOLERANCE * EDGE_BAND get partial alpha
UNIFORM_BORDER = 0.55     # share of border pixels that must be background-coloured
MIN_CLEARED, MAX_CLEARED = 0.04, 0.94
MIN_DECONTAMINATE_ALPHA = 96


def _distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def clear_edge_background(image, tolerance=TOLERANCE):
    """Return (RGBA image, info) with the edge-connected background made transparent, or (None, reason)."""
    rgba = image.convert('RGBA')
    w, h = rgba.size
    if w < 3 or h < 3:
        return None, 'too_small'
    px = rgba.load()
    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] + \
             [(0, y) for y in range(1, h - 1)] + [(w - 1, y) for y in range(1, h - 1)]
    opaque = [px[p][:3] for p in border if px[p][3] > 0]
    if not opaque:
        return None, 'already_transparent'
    # Quantize so that anti-aliasing noise does not split the dominant colour.
    counts = Counter(tuple(c // 8 for c in rgb) for rgb in opaque)
    bucket = counts.most_common(1)[0][0]
    members = [rgb for rgb in opaque if tuple(c // 8 for c in rgb) == bucket]
    background = tuple(sorted(rgb[i] for rgb in members)[len(members) // 2] for i in range(3))
    if sum(1 for rgb in opaque if _distance(rgb, background) <= tolerance) / len(opaque) < UNIFORM_BORDER:
        return None, 'border_not_uniform'
    seen = bytearray(w * h)
    queue = deque()
    for x, y in border:
        if px[x, y][3] > 0 and _distance(px[x, y][:3], background) <= tolerance and not seen[y * w + x]:
            seen[y * w + x] = 1
            queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] \
                    and _distance(px[nx, ny][:3], background) <= tolerance:
                seen[ny * w + nx] = 1
                queue.append((nx, ny))
    cleared = sum(seen) / (w * h)
    if not MIN_CLEARED <= cleared <= MAX_CLEARED:
        return None, 'nothing_to_clear' if cleared < MIN_CLEARED else 'would_clear_everything'
    out = rgba.copy()
    op = out.load()
    for y in range(h):
        for x in range(w):
            if seen[y * w + x]:
                r, g, b, _ = op[x, y]
                op[x, y] = (r, g, b, 0)
    band = tolerance * EDGE_BAND
    for y in range(h):
        for x in range(w):
            if seen[y * w + x] or not any(0 <= nx < w and 0 <= ny < h and seen[ny * w + nx]
                                          for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))):
                continue
            r, g, b, a0 = op[x, y]
            d = _distance((r, g, b), background)
            if d >= band:
                continue
            a = min(a0, round(255 * (d - tolerance) / (band - tolerance)) if d > tolerance else 0)
            if a >= MIN_DECONTAMINATE_ALPHA:
                f = a / 255
                r, g, b = (min(255, max(0, round((c - (1 - f) * bg) / f))) for c, bg in zip((r, g, b), background))
            op[x, y] = (r, g, b, a)
    return out, {'method': 'edge_connected_background', 'background': '#%02X%02X%02X' % background,
                 'cleared_ratio': round(cleared, 3), 'tolerance': tolerance}


# ---- traced-page integration ---------------------------------------------------------------------
SMALL_GRAPHIC_SHARE = 0.06   # only crops up to 6% of the page area count as small graphics
MATCH = TOLERANCE * 1.5      # how close the cleared colour must be to something that will show through


def _hex(value):
    if isinstance(value, str) and len(value) == 7 and value.startswith('#'):
        try:
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return None
    return None


def _fill_colours(fill):
    """Solid colour, or a few samples along a gradient; 'none'/unknown gives nothing."""
    solid = _hex(fill) or (_hex(fill.get('color')) if isinstance(fill, dict) else None)
    if solid:
        return [solid]
    if isinstance(fill, dict) and isinstance(fill.get('stops'), list):
        stops = [c for c in (_hex(s.get('color')) for s in fill['stops'] if isinstance(s, dict)) if c]
        samples = list(stops)
        for a, b in zip(stops, stops[1:]):
            samples += [tuple(round(a[i] + (b[i] - a[i]) * t / 4) for i in range(3)) for t in (1, 2, 3)]
        return samples
    return []


def underlying_colours(trace, index):
    """Colours that will show through where the graphic is cleared: the page background and the
    fills of native shapes drawn before it that contain the graphic's centre."""
    background = trace.get('background') or {}
    colours = _fill_colours(background.get('fill', background.get('color', '#FFFFFF')))
    x, y, w, h = trace['elements'][index]['bounds']
    cx, cy = x + w / 2, y + h / 2
    for element in trace['elements'][:index]:
        if element.get('type') != 'shape' or element.get('shape') in ('line',):
            continue
        sx, sy, sw, sh = element['bounds']
        if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
            colours += _fill_colours(element.get('fill'))
    return colours


def clear_small_graphic(trace, index, crop, page_size):
    """(RGBA, info) for a small traced graphic whose surroundings match what lies beneath it, else (None, reason)."""
    if crop.width * crop.height > page_size[0] * page_size[1] * SMALL_GRAPHIC_SHARE:
        return None, 'not_small'
    out, info = clear_edge_background(crop)
    if out is None:
        return None, info
    cleared = _hex(info['background'])
    if not any(_distance(cleared, colour) <= MATCH for colour in underlying_colours(trace, index)):
        # Coloured surroundings that nothing rebuilt beneath (e.g. an app-icon tile) belong to the graphic.
        return None, 'surroundings_not_rebuilt_beneath'
    return out, info
