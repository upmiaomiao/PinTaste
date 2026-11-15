"""Per-source resolution and bounded spatial coverage planning.

This module plans requests, never certifies returned footprint, useful native
resolution, a village boundary, or a model conclusion.  Execution/confirmation
and a two-slot scheduler remain the caller's responsibility.
"""
from __future__ import annotations

import math
import re

from .complaints import _axis_starts, _lonlat, _world
from .schemas import TaskRequest
from .views import _bbox, plan_views

_DECLARED = {"provider_declared", "provider_metadata", "upstream_declared", "source_config_declared",
             "explicit_provider_cap", "native_provider_metadata", "catalogue_declared", "catalog_declared"}


def _zoom(value):
    from .source_catalog import valid_zoom
    return valid_zoom(value)


def _metadata(entry):
    if not isinstance(entry, dict):
        raise ValueError("图源目录信息必须是对象")
    return entry.get("metadata") if isinstance(entry.get("metadata"), dict) else entry


def resolve_source_zoom(source_entry: dict, requested_zoom: int | None = None) -> dict:
    """Choose declared native maximum, then a declared provider render cap.

    An old adapter's default `max_zoom=22` is not a provider declaration.
    It is deliberately ignored unless accompanied by provenance/explicit flag.
    A provider display cap never proves a native ground sample distance.
    """
    metadata = _metadata(source_entry)
    mapping = metadata.get("map_metadata") or {}
    if not isinstance(mapping, dict):
        mapping = {}
    native = _zoom(mapping.get("native_max_zoom")) or _zoom(metadata.get("native_max_zoom"))
    native_declared = (mapping.get("native_max_zoom_declared", metadata.get("native_max_zoom_declared")) is not False)
    if not native_declared:
        native = None
    render = _zoom(mapping.get("max_zoom")) or _zoom(metadata.get("max_zoom")) or _zoom(metadata.get("zoom_max"))
    basis = mapping.get("max_zoom_basis", metadata.get("max_zoom_basis"))
    declared = mapping.get("max_zoom_declared", metadata.get("max_zoom_declared"))
    cap_declared = declared is True or (declared is not False and basis in _DECLARED)
    if not cap_declared:
        render = None
    if requested_zoom is not None and _zoom(requested_zoom) is None:
        raise ValueError("用户请求层级需为1至22的整数")
    fallback = _zoom(metadata.get("preferred_zoom")) or _zoom(mapping.get("preferred_zoom")) or 18
    if requested_zoom is not None:
        chosen, choice = requested_zoom, "user_requested_zoom"
    elif native is not None:
        chosen, choice = min(native, render) if render is not None else native, "declared_native_maximum"
    elif render is not None:
        chosen, choice = render, "declared_provider_cap_native_unknown"
    else:
        chosen, choice = fallback, "configured_fallback_maximum_unknown"
    supported = render is None or chosen <= render
    return {"zoom": chosen, "selection_basis": choice, "requested_zoom": requested_zoom,
            "provider_max_zoom": render, "provider_cap_declared": cap_declared and render is not None,
            "native_max_zoom": native, "native_max_zoom_known": native is not None,
            "maximum_known": native is not None or render is not None,
            "native_resolution_m": None, "native_resolution_proven": False,
            "display_oversampling": bool(native is not None and chosen > native),
            "supported": supported, "status": "ready" if supported else "unsupported_requested_zoom",
            "limitation": ("用户请求层级超过该来源声明的服务上限；未擅自降低层级。" if not supported else
                           "原生层级上限未知；采用明确声明的服务层级上限，不将显示层级称为实际分辨率。" if native is None and render else
                           "未取得来源声明的最高层级，暂用配置的保守显示层级，不宣称已取最高精度。" if native is None else
                           "来源声明的原生层级上限仍不等同于本区域实际原生分辨率或测量精度。")}


def _source_id(entry):
    metadata = _metadata(entry)
    return str(entry.get("source") or metadata.get("source") or metadata.get("id") or "unknown")


def _nearby_authorized(objective):
    return bool(re.search(r"(?:附近|周围|周边|这一带|一带|村内|村中|区域).{0,30}(?:扫描|排查|核查|查看|看看|分析|检查)|"
                          r"(?:扫描|排查|核查|查看|看看|分析|检查).{0,30}(?:附近|周围|周边|这一带|一带|村内|村中|区域)", objective))


def _radius_from_text(objective):
    # Only explicit radius language sets a radius. A bare "500米" may refer to
    # distance to a road, waste size, or a different measurement.
    match = re.search(r"(?:半径|周围|周边|附近)\s*(\d+(?:\.\d+)?)\s*(公里|千米|米|km|m)(?:内|范围)?", objective, re.I)
    if not match:
        return None
    value = float(match.group(1)) * (1000 if match.group(2).lower() in {"公里", "千米", "km"} else 1)
    return value


def vicinity_scope(task: TaskRequest, geographic_basis: dict | None = None, *, radius_m: float | None = None,
                   nearby_authorized: bool | None = None) -> dict:
    """Derive an auditable working region from location precision and intent."""
    if task.bbox is not None:
        return {"status": "ready", "working_bbox": list(task.bbox), "basis": "explicit_task_bbox",
                "is_administrative_boundary": False, "requires_confirmation": False, "center_is_target": False}
    basis = geographic_basis or {}
    center = task.center or basis.get("center_wgs84")
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        return {"status": "needs_location", "working_bbox": None, "requires_confirmation": True,
                "is_administrative_boundary": False, "center_is_target": False}
    lon, lat = center
    if (any(type(value) not in {float, int} or not math.isfinite(value) for value in center)
            or not -180 <= lon <= 180 or not -85 <= lat <= 85):
        raise ValueError("位置参考中心必须为有效WGS84经纬度")
    level = str(basis.get("match_level") or basis.get("level") or "").strip().lower()
    if basis.get("requires_confirmation") is True:
        return {"status": "needs_scope_confirmation", "working_bbox": None,
                "center": list(center), "match_level": level, "basis": "unconfirmed_geocoder_candidate",
                "is_administrative_boundary": False, "requires_confirmation": True, "center_is_target": False,
                "reason": "地名候选仍未唯一确认，先确定要核查的位置。"}
    name = str(basis.get("name") or "")
    broad = level in {"country", "province", "city", "district", "county", "town", "township",
                      "国家", "省", "省份", "城市", "市", "区", "县", "乡镇", "乡", "镇", "区县", "行政区"}
    village = level in {"village", "村", "村庄", "村级", "村委会", "社区", "居委会"} or (level == "poi" and bool(re.search(r"村(?:委会)?$|社区$", name)))
    coarse_poi = level == "poi"
    reported_radius = _radius_from_text(task.objective)
    radius = radius_m if radius_m is not None else reported_radius
    if radius is not None and (type(radius) not in {int, float} or not math.isfinite(radius) or not 1 <= radius <= 5000):
        raise ValueError("周边工作半径需为1至5000米，超出范围请先明确拆分区域")
    explicit_nearby = _nearby_authorized(task.objective) if nearby_authorized is None else bool(nearby_authorized)
    if broad:
        return {"status": "needs_scope_confirmation", "working_bbox": None,
                "center": list(center), "match_level": level, "basis": "administrative_reference_center",
                "is_administrative_boundary": False, "requires_confirmation": True, "center_is_target": False,
                "reason": "地名仅匹配到较大行政区，不能将行政中心或整片辖区当作具体举报位置；需缩小范围。"}
    if village or coarse_poi or explicit_nearby or radius is not None:
        effective_radius = float(radius if radius is not None else 250)
        bounds = _bbox(lon, lat, effective_radius * 2, effective_radius * 2)
        # Asking to check a named village/POI is itself authorization to inspect
        # a bounded neighbourhood around that geocoder reference. It does not
        # authorize province-wide search or assert that the whole village fits.
        authorized = explicit_nearby or village or coarse_poi or radius is not None
        return {"status": "ready" if authorized else "needs_scope_confirmation", "working_bbox": bounds,
                "center": list(center), "match_level": level or "coordinate", "basis": "bounded_geocoded_vicinity" if village or coarse_poi else "user_requested_vicinity",
                "working_radius_m": effective_radius, "radius_basis": "user_explicit" if radius is not None else "bounded_default",
                "is_administrative_boundary": False, "requires_confirmation": not authorized, "center_is_target": False,
                "coverage_limitation": "以地名参考中心形成周边工作范围，不是村界、场地边界或已定位的倾倒点。"}
    return {"status": "ready", "working_bbox": None, "center": list(center), "basis": "explicit_coordinate_point",
            "is_administrative_boundary": False, "requires_confirmation": False,
            "center_is_target": task.coordinate_system == "wgs84", "coordinate_system": task.coordinate_system}


def _coverage_frames(bounds, zoom, pixels, overlap, max_windows):
    world = 256 * 2 ** zoom
    west, south, east, north = bounds
    x0, y1 = _world(west, south, world)
    x1, y0 = _world(east, north, world)
    stride = pixels * (1 - overlap)
    xs = [(x0 + x1 - pixels) / 2] if x1 - x0 <= pixels + 1e-5 else _axis_starts(x0, x1, pixels, stride)
    ys = [(y0 + y1 - pixels) / 2] if y1 - y0 <= pixels + 1e-5 else _axis_starts(y0, y1, pixels, stride)
    required = len(xs) * len(ys) if xs is not None and ys is not None else None
    if required is None or required > max_windows:
        return required, []
    result = []
    for row, y in enumerate(ys):
        for col, x in enumerate(xs):
            if x < 0 or y < 0 or x + pixels > world or y + pixels > world:
                raise ValueError("最高层级窗口跨越世界图幅边界，请缩小并重新确认范围")
            w, n = _lonlat(x, y, world)
            e, s = _lonlat(x + pixels, y + pixels, world)
            result.append({"role": "target_crop", "bbox": [w, s, e, n], "zoom": zoom,
                "width": pixels, "height": pixels, "coverage_window_id": f"r{row+1:04d}c{col+1:04d}",
                "requested_width_m": round((e - w) * 111320 * math.cos(math.radians((n + s) / 2)), 2),
                "requested_height_m": round((n - s) * 111320, 2),
                "native_resolution_m": None, "actual_coverage_verified": False,
                "basis": "source_maximum_zoom_coverage_grid", "coordinate_system": "wgs84",
                "coordinate_assumption": "declared_wgs84"})
    return required, result


def plan_source_coverage(task: TaskRequest | dict, source_entry: dict, geographic_basis: dict | None = None,
                         *, requested_zoom: int | None = None, window_pixels: int | None = None,
                         overlap: float = .15, max_windows: int = 16, radius_m: float | None = None,
                         nearby_authorized: bool | None = None) -> dict:
    """Plan all windows at one source's best declared level, never downsample AOI."""
    task = task if isinstance(task, TaskRequest) else TaskRequest.model_validate(task)
    automatic_pixels = window_pixels is None
    if window_pixels is None:
        window_pixels = 1024
    if window_pixels not in {1024, 2048} or type(window_pixels) is not int:
        raise ValueError("周边扫描图幅需为1024或2048像素")
    if type(max_windows) is not int or not 1 <= max_windows <= 256:
        raise ValueError("周边扫描窗口预算需为1至256")
    if type(overlap) not in {float, int} or not math.isfinite(overlap) or not 0 <= overlap <= .5:
        raise ValueError("周边扫描重叠比例需为0至0.5")
    source = _source_id(source_entry)
    # A confirmed complaint window is an immutable execution contract. Even a
    # source advertising higher zoom must not regrid or enlarge this window.
    if task.complaint_scan and task.complaint_scan.window_id:
        return {"status": "ready", "source": source, "windows": plan_views(task), "required_windows": 1,
                "working_bbox": list(task.bbox), "concurrency": 2, "max_windows": max_windows,
                "zoom_decision": {"zoom": task.complaint_scan.zoom, "selection_basis": "confirmed_complaint_window",
                                  "native_resolution_proven": False},
                "scope": {"basis": "confirmed_complaint_window", "requires_confirmation": False},
                "unrequested_expansion": False}
    decision = resolve_source_zoom(source_entry, requested_zoom)
    scope = vicinity_scope(task, geographic_basis, radius_m=radius_m, nearby_authorized=nearby_authorized)
    base = {"source": source, "windows": [], "zoom_decision": decision, "scope": scope,
            "working_bbox": scope.get("working_bbox"), "max_windows": max_windows, "concurrency": 2,
            "window_pixels": window_pixels, "overlap": overlap, "actual_coverage_verified": False,
            "missing_coverage": "尚未执行取图与判读，所有工作范围的有效覆盖均待核验。"}
    if decision["status"] != "ready":
        return base | {"status": decision["status"], "required_windows": None}
    if scope["status"] != "ready":
        return base | {"status": scope["status"], "required_windows": None}
    bounds = scope.get("working_bbox")
    if bounds is None:
        # Exact point: one full pixel frame centred at the selected source zoom.
        world = 256 * 2 ** decision["zoom"]
        cx, cy = _world(*scope["center"], world)
        west, north = _lonlat(cx - window_pixels / 2, cy - window_pixels / 2, world)
        east, south = _lonlat(cx + window_pixels / 2, cy + window_pixels / 2, world)
        bounds = [west, south, east, north]
    required, windows = _coverage_frames(bounds, decision["zoom"], window_pixels, overlap, max_windows)
    if not windows and automatic_pixels and scope.get("working_bbox") is not None:
        # Increase only the delivered pixel dimensions. The source's chosen
        # zoom, user's working region and overlap are unchanged, so a wide AOI
        # is never squeezed into a low-resolution centre screenshot.
        window_pixels = 2048
        required, windows = _coverage_frames(bounds, decision["zoom"], window_pixels, overlap, max_windows)
        base.update(window_pixels=window_pixels, window_pixels_selection="auto_larger_frame_to_fit_budget")
    else:
        base["window_pixels_selection"] = "automatic_1024" if automatic_pixels else "caller_selected"
    base["working_bbox"] = scope.get("working_bbox") or bounds
    if not windows:
        return base | {"status": "over_budget", "required_windows": required, "requires_confirmation": True,
                       "next_action": "最高声明层级所需窗口超出预算；请明确缩小范围、采用2048图幅或提高预算。没有降低层级或截断覆盖。"}
    for view in windows:
        view["coordinate_system"] = task.coordinate_system
        view["coordinate_assumption"] = ("provisional_wgs84_for_context_only" if task.coordinate_system == "unknown"
                                         else "declared_wgs84")
    return base | {"status": "ready", "windows": windows, "required_windows": required,
                   "requires_confirmation": False, "unrequested_expansion": False,
                   "render_zoom_is_native_resolution": False}
