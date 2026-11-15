"""Bounded, non-executing SVG M/C/L/Z -> native polygon approximation.

Coordinates are local pixels. Unsupported SVG constructs fail closed. The
control-hull distance to each accepted finite chord is a conservative geometric
error bound; source paths remain in the untouched provider-response files.
"""
import hashlib
import math
import re
from .wuyu_errors import PipelineError

_TOKEN = re.compile(r'[MCLZ]|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
_ALLOWED_FIELDS = {'type', 'shape', 'bounds', 'path', 'fill', 'line', 'rotation', 'opacity'}


def _reject():
    raise PipelineError('unsupported_trace_shape', '自由曲线不是可安全转换的有界闭合M/C/L/Z路径，未猜测或替换形状。')


def _distance_to_segment(point, start, end):
    dx, dy = end[0]-start[0], end[1]-start[1]
    length = dx*dx+dy*dy
    t = max(0.0, min(1.0, ((point[0]-start[0])*dx+(point[1]-start[1])*dy)/length)) if length else 0
    return math.hypot(point[0]-start[0]-t*dx, point[1]-start[1]-t*dy)


def _clip_polygon(points, width, height):
    # Sutherland-Hodgman intersection of the filled polygon and its visible box.
    for axis, boundary, keep_greater in [(0,0,True),(0,width,False),(1,0,True),(1,height,False)]:
        result = []
        for start,end in zip(points[-1:]+points[:-1],points):
            inside_start = start[axis] >= boundary if keep_greater else start[axis] <= boundary
            inside_end = end[axis] >= boundary if keep_greater else end[axis] <= boundary
            if inside_start != inside_end:
                t = (boundary-start[axis])/(end[axis]-start[axis])
                intersection = [start[0]+t*(end[0]-start[0]),start[1]+t*(end[1]-start[1])]
                intersection[axis] = boundary
                result.append(intersection)
            if inside_end:
                result.append(end)
        points = []
        for point in result:
            if not points or math.dist(point,points[-1]) > 1e-10:
                points.append(point)
        if len(points)>1 and math.dist(points[0],points[-1])<=1e-10:
            points.pop()
        if not points:
            _reject()
    return points


def _axis_extrema(a,b,c,d):
    A, B, C = -a+3*b-3*c+d, 2*(a-2*b+c), b-a
    roots = []
    if abs(A)<1e-12:
        if abs(B)>1e-12: roots=[-C/B]
    else:
        discriminant=B*B-4*A*C
        if discriminant>=0:
            roots=[(-B+math.sqrt(discriminant))/(2*A),(-B-math.sqrt(discriminant))/(2*A)]
    return [a,d]+[(1-t)**3*a+3*(1-t)**2*t*b+3*(1-t)*t*t*c+t**3*d for t in roots if 0<t<1]


def restricted_path_polygon(path, width, height, *, tolerance_px=.5, max_vertices=100, canvas_edges=()):
    if (not isinstance(path, str) or not 1 <= len(path) <= 8192
            or isinstance(tolerance_px, bool) or not isinstance(tolerance_px, (int, float))
            or not math.isfinite(tolerance_px) or not 0 < tolerance_px <= .5
            or type(max_vertices) is not int or not 3 <= max_vertices <= 100
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= 5376 for v in (width, height))):
        _reject()
    tokens, end = [], 0
    for match in _TOKEN.finditer(path):
        if path[end:match.start()].strip(' \t\r\n,'):
            _reject()
        tokens.append(match.group()); end = match.end()
    if path[end:].strip(' \t\r\n,') or len(tokens) > 260 or not tokens or tokens[0] != 'M':
        _reject()
    position, points, accepted_errors, commands = 0, [], [], 0
    geometry_x, geometry_y = [], []
    edges = set(canvas_edges)
    if not edges <= {'left','right','top','bottom'}:
        _reject()
    limit = 4*max(width, height)

    def pair(endpoint=False):
        nonlocal position
        if position+2 > len(tokens):
            _reject()
        try:
            values = [float(tokens[position]), float(tokens[position+1])]
        except ValueError:
            _reject()
        position += 2
        if any(not math.isfinite(v) or abs(v) > limit for v in values):
            _reject()
        if endpoint and ((values[0]<0 and 'left' not in edges) or (values[0]>width and 'right' not in edges)
                or (values[1]<0 and 'top' not in edges) or (values[1]>height and 'bottom' not in edges)):
            _reject()
        if endpoint:
            geometry_x.append(values[0]); geometry_y.append(values[1])
        return values

    def append(point):
        if not points or point != points[-1]:
            points.append(point)
        # One closing duplicate is allowed temporarily; polygon closes itself.
        if len(points) > max_vertices+1:
            _reject()

    def midpoint(a, b):
        return [(a[0]+b[0])/2, (a[1]+b[1])/2]

    def flatten(a, b, c, d, depth=0):
        error = max(_distance_to_segment(b, a, d), _distance_to_segment(c, a, d))
        if error <= tolerance_px:
            append(d); accepted_errors.append(error)
            return
        if depth >= 16:
            _reject()
        ab, bc, cd = midpoint(a,b), midpoint(b,c), midpoint(c,d)
        abc, bcd = midpoint(ab,bc), midpoint(bc,cd)
        middle = midpoint(abc,bcd)
        flatten(a,ab,abc,middle,depth+1)
        flatten(middle,bcd,cd,d,depth+1)

    position = 1
    append(pair(endpoint=True))
    while position < len(tokens):
        command = tokens[position]; position += 1; commands += 1
        if commands > 32:
            _reject()
        if command == 'L':
            append(pair(endpoint=True))
        elif command == 'C':
            a = points[-1]; b = pair(); c = pair(); d = pair(endpoint=True)
            geometry_x.extend(_axis_extrema(a[0],b[0],c[0],d[0]))
            geometry_y.extend(_axis_extrema(a[1],b[1],c[1],d[1]))
            flatten(a,b,c,d)
        elif command == 'Z':
            if position != len(tokens):
                _reject()
            break
        else:
            _reject()
    else:
        _reject()
    if points[-1] == points[0]:
        points.pop()
    if not 3 <= len(points) <= max_vertices or len({tuple(p) for p in points}) < 3:
        _reject()
    geometry_bounds = [min(geometry_x),min(geometry_y),max(geometry_x),max(geometry_y)]
    outside = {'left':max(0,-geometry_bounds[0]),'top':max(0,-geometry_bounds[1]),
        'right':max(0,geometry_bounds[2]-width),'bottom':max(0,geometry_bounds[3]-height)}
    clipped_edges = [edge for edge, amount in outside.items() if amount>1e-9]
    if any(edge not in edges for edge in clipped_edges):
        _reject()
    if clipped_edges:
        points = _clip_polygon(points,width,height)
    if not 3 <= len(points) <= max_vertices or any(not (0<=x<=width and 0<=y<=height) for x,y in points):
        _reject()
    # A closed path with zero signed area cannot form a filled native polygon.
    area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points, points[1:]+points[:1]))
    if abs(area) < 1e-9:
        _reject()
    return points, max(accepted_errors, default=0.0), {'clipped_to_canvas':bool(clipped_edges),
        'clipped_edges':clipped_edges,'original_curve_bounds_local':geometry_bounds,'outside_geometry_bounds_px':outside,
        'clip_bounds_local':[0,0,width,height] if clipped_edges else None,
        'error_scope':'canvas-visible contour only; invisible outside-canvas geometry is clipped'}


def normalize_restricted_freeforms(trace, *, tolerance_px=.5, max_vertices=100):
    """Modify only supported freeform elements in a caller-owned trace copy."""
    records = []
    for index, element in enumerate(trace.get('elements', [])):
        if not isinstance(element, dict) or element.get('type') != 'shape' or element.get('shape') != 'freeform':
            continue
        box = element.get('bounds')
        if (set(element)-_ALLOWED_FIELDS or not isinstance(box, list) or len(box) != 4
                or any(isinstance(v, bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in box)):
            _reject()
        path = element.get('path')
        canvas=trace.get('canvas',{})
        cw,ch=canvas.get('width'),canvas.get('height')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (cw,ch)) or not (0<=box[0] and 0<=box[1] and box[0]+box[2]<=cw and box[1]+box[3]<=ch):
            _reject()
        edges=[]
        if box[0]==0: edges.append('left')
        if box[1]==0: edges.append('top')
        if box[0]+box[2]==cw: edges.append('right')
        if box[1]+box[3]==ch: edges.append('bottom')
        vertices, error, clipping = restricted_path_polygon(path, box[2], box[3], tolerance_px=tolerance_px, max_vertices=max_vertices, canvas_edges=edges)
        record = {'element_index': index, 'method': 'restricted_svg_cubic_to_polygon',
            'source_path_sha256': hashlib.sha256(path.encode()).hexdigest(), 'bounds': list(box),
            'tolerance_px': tolerance_px, 'max_error_bound_px': error, 'vertex_count': len(vertices), 'points': vertices, **clipping}
        element['shape'] = 'polygon'; element['points'] = vertices
        element['points_coordinate_system'] = 'local_px'
        del element['path']
        records.append(record)
    if records:
        trace.setdefault('freeform_approximations', []).extend(records)
    return records
