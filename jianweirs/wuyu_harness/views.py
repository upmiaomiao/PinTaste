"""Bounded, deterministic image scales; render scale is never native sensor GSD."""

from __future__ import annotations

import math

from .schemas import TaskRequest


def _bbox(lon: float, lat: float, width_m: float, height_m: float) -> list[float]:
    dy = height_m / (2 * 111_320)
    dx = width_m / (2 * 111_320 * math.cos(math.radians(lat)))
    bounds = [lon - dx, lat - dy, lon + dx, lat + dy]
    if not (-180 <= bounds[0] < bounds[2] <= 180 and -85 <= bounds[1] < bounds[3] <= 85):
        raise ValueError("自动图幅跨越经度或纬度边界，请提供边界内 bbox")
    return bounds


def plan_views(task: TaskRequest) -> list[dict]:
    """Preserve an explicit AOI; center-only requests get three reproducible scales.

    These are requested footprints. Actual provider bounds and registration remain
    unverified until returned by a geospatial renderer; no pixel-to-ground claim
    follows from these dimensions.
    """
    scan = task.complaint_scan
    if scan and scan.window_id and scan.window_bbox:
        # Only the confirmed server coordinator generates these windows. Keep
        # its Web Mercator pixel grid unchanged; recomputing zoom via a metric
        # approximation can silently halve coverage or change the budget.
        if tuple(task.bbox or ()) != tuple(scan.window_bbox) or not scan.region_confirmed:
            raise ValueError("扫描窗口未确认或与任务范围不一致")
        west, south, east, north = scan.window_bbox
        latitude = (south + north) / 2
        return [{"role": "target_crop", "bbox": list(scan.window_bbox), "zoom": scan.zoom,
                 "width": scan.window_pixels, "height": scan.window_pixels,
                 "requested_width_m": round((east - west) * 111320 * math.cos(math.radians(latitude)), 2),
                 "requested_height_m": round((north - south) * 111320, 2),
                 "basis": "confirmed_complaint_scan_window", "window_id": scan.window_id,
                 "coordinate_system": "wgs84", "coordinate_assumption": "declared_wgs84",
                 "native_resolution_m": None, "actual_coverage_verified": False}]
    if task.bbox:
        west, south, east, north = task.bbox
        lon, lat = (west + east) / 2, (south + north) / 2
        width = (east - west) * 111_320 * math.cos(math.radians(lat))
        height = (north - south) * 111_320
        if max(width, height) > 11_900:
            raise ValueError("分析范围超过单幅12公里接口限制，需要先分块，不能只取中心冒充全域覆盖")
        initial = list(task.bbox)
        basis = "user_bbox"
    elif task.center:
        lon, lat = task.center
        # Only explicit m² can drive this estimate; ambiguous workbook units cannot.
        width = height = min(2000., max(300., math.sqrt(task.site_area_m2 or 0) * 3))
        if task.coordinate_system == "unknown":
            width = height = max(800., width)
        initial = _bbox(lon, lat, width, height)
        basis = "center_and_declared_m2" if task.site_area_m2 else "center_default"
    else:
        return []
    spans = [("target_crop", width, height, initial)]
    if task.view_mode == "auto":
        local_width = min(11_900., max(900., width * 3))
        local_height = min(11_900., max(900., height * 3))
        full_width = min(11_900., max(2400., local_width * 2))
        full_height = min(11_900., max(2400., local_height * 2))
        for role, w, h in (("local_context", local_width, local_height), ("full_scene", full_width, full_height)):
            if w <= spans[-1][1] and h <= spans[-1][2]:
                continue
            spans.append((role, w, h, _bbox(lon, lat, w, h)))
    result = []
    for role, w, h, bounds in spans:
        zoom = max(1, min(20, math.floor(math.log2(156543.033928 * math.cos(math.radians(lat)) * 1024 / max(w, h)))))
        result.append({"role": role, "bbox": bounds, "zoom": zoom, "width": 1024, "height": 1024,
                       "requested_width_m": round(w, 2), "requested_height_m": round(h, 2), "basis": basis,
                       "coordinate_system": task.coordinate_system,
                       "coordinate_assumption": "provisional_wgs84_for_context_only" if task.coordinate_system == "unknown" else "declared_wgs84",
                       "native_resolution_m": None, "actual_coverage_verified": False})
    return result


def initial_views(task: TaskRequest, strategy: dict) -> list[dict]:
    """Every initial source requests the same single footprint before review."""
    return [{**view,"acquisition_purpose":"same_aoi_multisource_baseline"} for view in plan_views(task)[:1]]


def expanded_view(task: TaskRequest,base_view: dict | None=None) -> dict | None:
    """One bounded context expansion, never an inferred candidate geolocation."""
    if task.view_mode=="single":return None
    if base_view and base_view.get('coverage_window_id'):
        west,south,east,north=base_view['bbox']
        lon,lat=(west+east)/2,(south+north)/2
        # A wider context view follows this window's centre. It does not locate
        # the model's pixel candidate or prove that adjacent ground was reviewed.
        task=task.model_copy(update={'bbox':None,'center':(lon,lat)})
    views=plan_views(task)
    if len(views)<2:return None
    view=views[1]
    return {**view,"acquisition_purpose":"coverage_driven_context_expansion",
            "candidate_pixel_to_geographic_conversion":False}


def detail_view(task: TaskRequest,base_view: dict) -> dict | None:
    """Request a smaller display window; never claim an improved sensor GSD."""
    if task.view_mode=="single" or min(base_view['requested_width_m'],base_view['requested_height_m'])<=100:return None
    west,south,east,north=base_view['bbox'];lon,lat=(west+east)/2,(south+north)/2
    return {**base_view,'bbox':[lon-(east-west)/4,lat-(north-south)/4,lon+(east-west)/4,lat+(north-south)/4],
            'zoom':min(20,base_view['zoom']+1),'requested_width_m':base_view['requested_width_m']/2,
            'requested_height_m':base_view['requested_height_m']/2,'role':'target_crop',
            'acquisition_purpose':'quality_driven_smaller_display_window','native_resolution_m':None,
            'actual_coverage_verified':False,'full_task_aoi_covered':False}


def adaptive_views(task: TaskRequest, strategy: dict) -> list[dict]:
    """A target view from an independent source, with context only when useful."""
    views = plan_views(task)
    targets = [view for view in views if view["role"] == "target_crop"]
    if any(kind in {"environmental_context", "surrounding_relationship"} for kind in task.task_types):
        targets += [view for view in views if view["role"] == "local_context"][:1]
    return targets[:2]


def crop_window(width: int, height: int, window: tuple | list, padding: float = .35) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = window
    dx, dy = max(16, round((x2-x1)*padding)), max(16, round((y2-y1)*padding))
    return max(0, x1-dx), max(0, y1-dy), min(width, x2+dx), min(height, y2+dy)
