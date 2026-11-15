"""Lossless syntax repairs for observed trace dialect differences.

This module never calls a model, inserts text, moves content, or substitutes a
raster for native objects. Original provider response files remain untouched.
"""
import hashlib
import json
import math
import re


def native_title_contract(spec):
    title = spec.get('title', '')
    if spec.get('kind') == 'cover' and isinstance(title, str):
        # A semantic cover label in the plan is not a word printed on its title.
        # Only this exact role/prefix combination is recognized; no fuzzy edits.
        match = re.fullmatch(r'封面[：:]\s*(\S[\s\S]*)', title)
        if match:
            return match.group(1)
    return title


def normalize_observed_trace_dialects(trace):
    records = []
    for index, element in enumerate(trace.get('elements', [])):
        if not isinstance(element, dict) or element.get('type') != 'shape':
            continue
        box = element.get('bounds')
        if (not isinstance(box, list) or len(box) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                or min(box[2:]) <= 0):
            continue
        if element.get('shape') in ('round_rect', 'roundedRectangle', 'rounded_rectangle', 'roundRect', 'rounded_rect'):
            radius = element.get('corner_radius')
            if (isinstance(radius, (int, float)) and not isinstance(radius, bool)
                    and math.isfinite(radius) and radius > min(box[2:]) / 2):
                # Circular corner radii saturate at the half-short-side capsule.
                # This is a representational limit, not geometry rescaling.
                element['corner_radius'] = min(box[2:]) / 2
                records.append({'element_index': index, 'field': 'corner_radius', 'original': radius,
                                'normalized': element['corner_radius'], 'rule': 'circular_corner_radius_saturates_at_half_short_side'})
        if element.get('shape') != 'freeform' or 'path' in element:
            continue
        allowed = {'type', 'shape', 'bounds', 'points', 'fill', 'line', 'rotation', 'opacity'}
        vertices = element.get('points')
        if (set(element) - allowed or not isinstance(vertices, list) or not 3 <= len(vertices) <= 100
                or any(not isinstance(p, list) or len(p) != 2 or
                       any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p)
                       for p in vertices)):
            continue
        if any(not (0 <= x <= box[2] and 0 <= y <= box[3]) for x, y in vertices):
            continue
        if len({tuple(p) for p in vertices}) < 3:
            continue
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(vertices, vertices[1:] + vertices[:1]))
        if abs(area) < 1e-9:
            continue
        element['shape'] = 'polygon'
        record = {'element_index': index, 'method': 'explicit_local_polygon_vertices',
                  'source_vertices_sha256': hashlib.sha256(json.dumps(vertices, separators=(',', ':')).encode()).hexdigest(),
                  'bounds': list(box), 'vertex_count': len(vertices), 'max_error_bound_px': 0,
                  'points': [list(p) for p in vertices], 'provider_called': False,
                  'image_fidelity': 'unverified'}
        trace.setdefault('freeform_approximations', []).append(record)
        records.append({'element_index': index, 'field': 'shape', 'original': 'freeform',
                        'normalized': 'polygon', 'rule': 'explicit_local_points_without_curve_commands'})
    if records:
        trace.setdefault('normalization_corrections', []).extend(records)
    return records



def raster_overlap_occlusion_proof(trace, image_index, text_index, crop):
    """Prove the entire overlap hidden by later opaque native rectangle cores.

    Crops, native text, ordering and pixels are untouched. Unsupported shapes,
    transparent fills, transformations or any uncovered sliver fail closed.
    """
    elements = trace.get('elements', [])
    image, text = elements[image_index], elements[text_index]
    if (image_index >= text_index or image.get('rotation', 0) != 0
            or text.get('rotation', 0) != 0 or crop != image.get('bounds')):
        return None
    x, y, w, h = crop; tx, ty, tw, th = text['bounds']
    left, top, right, bottom = max(x, tx), max(y, ty), min(x+w, tx+tw), min(y+h, ty+th)
    if left >= right or top >= bottom:
        return None
    remaining = [[left, top, right, bottom]]
    covers = []
    def subtract(box, cut):
        x, y, r, b = box; cx, cy, cr, cb = cut
        l, t, right, bottom = max(x,cx), max(y,cy), min(r,cr), min(b,cb)
        if l >= right or t >= bottom:
            return [box]
        return [q for q in ([x,y,r,t],[x,bottom,r,b],[x,t,l,bottom],[right,t,r,bottom]) if q[0] < q[2] and q[1] < q[3]]
    def opaque(value):
        if isinstance(value,str):
            return value != 'none'
        if not isinstance(value,dict):
            return False
        if value.get('type','solid') == 'solid':
            return value.get('opacity',1) == 1
        return value.get('type') == 'linear_gradient' and bool(value.get('stops')) and all(s.get('opacity',1) == 1 for s in value['stops'])
    for index in range(image_index+1,text_index):
        element=elements[index]
        if (element.get('type') != 'shape' or element.get('shape','rect') not in ('rect','round_rect')
                or element.get('rotation',0) != 0 or element.get('opacity',1) != 1
                or not opaque(element.get('fill'))):
            continue
        sx,sy,sw,sh=element['bounds']
        radius=min(sw,sh)/2 if element.get('shape') == 'round_rect' else 0
        # Worst-case round-rectangle radius plus an antialias inset. These
        # rectangle cores are guaranteed to be inside its fully opaque fill.
        cores=[[sx+radius+1,sy+1,sx+sw-radius-1,sy+sh-1],
               [sx+1,sy+radius+1,sx+sw-1,sy+sh-radius-1]]
        before=remaining
        for core in cores:
            if core[0] >= core[2] or core[1] >= core[3]:
                continue
            remaining=[part for box in remaining for part in subtract(box,core)]
            if len(remaining)>64:
                return None
        if remaining != before:
            covers.append(index)
        if not remaining:
            return {'image_index':image_index,'text_index':text_index,'covering_shape_indices':covers,
                    'overlap_bounds':[left,top,right-left,bottom-top],
                    'method':'entire_overlap_hidden_by_later_opaque_native_fill',
                    'raster_crop_unchanged':True,'native_text_unchanged':True}
    return None
