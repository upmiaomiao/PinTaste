"""Bounded complaint-area search planning and auditable coverage accounting.

No network, geocoding or image interpretation occurs here.  The service owns
the persisted plan and derives evidence rows from its own analysis runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date

from .schemas import ComplaintScan, TaskRequest

_R = 6378137.0
_LIMIT_Y = 85.05112878
_EPS = 1e-12


def _valid_bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in value):
        return None
    w, s, e, n = map(float, value)
    return (w, s, e, n) if -180 <= w < e <= 180 and -85 <= s < n <= 85 else None


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d):
    # Inclusive: touching non-neighbouring edges makes a ring non-simple too.
    def between(a, b, p):
        return (min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
                and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS)
    ab1, ab2, cd1, cd2 = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if ab1 * ab2 < 0 and cd1 * cd2 < 0:
        return True
    return any(abs(k) < _EPS and between(p, q, t) for k, p, q, t in
               ((ab1, a, b, c), (ab2, a, b, d), (cd1, c, d, a), (cd2, c, d, b)))


def _region(spec: ComplaintScan):
    if spec.region_polygon is not None:
        obj = spec.region_polygon
        coords = obj.get("coordinates")
        if obj.get("type") != "Polygon" or not isinstance(coords, list) or len(coords) != 1:
            raise ValueError("扫描范围支持单一无孔洞的GeoJSON Polygon，复杂区域请拆分确认")
        ring = coords[0]
        if not isinstance(ring, list) or not 4 <= len(ring) <= 257:
            raise ValueError("Polygon需4至257个坐标点，包含闭合点")
        points = []
        for point in ring:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Polygon坐标必须是[经度,纬度]")
            x, y = point
            if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (x, y)):
                raise ValueError("Polygon坐标必须是有限数值")
            if not (-180 <= x <= 180 and -85 <= y <= 85):
                raise ValueError("Polygon坐标超出WGS84扫描范围")
            points.append((float(x), float(y)))
        if points[0] != points[-1] or len(set(points[:-1])) != len(points) - 1:
            raise ValueError("Polygon必须闭合且没有重复顶点")
        count = len(points) - 1
        for i in range(count):
            for j in range(i + 1, count):
                if j == i + 1 or (i == 0 and j == count - 1):
                    continue
                if _segments_intersect(points[i], points[i + 1], points[j], points[j + 1]):
                    raise ValueError("Polygon存在自交，请重新选择有效扫描区域")
        bbox = (min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points))
        if _valid_bbox(bbox) is None or bbox[2] - bbox[0] > 30:
            raise ValueError("扫描区域无效或跨越过宽，请缩小区域")
    elif spec.region_bbox is not None:
        bbox = tuple(spec.region_bbox)
        w, s, e, n = bbox
        points = [(w, s), (e, s), (e, n), (w, n), (w, s)]
    else:
        return None, None
    if _polygon_area(points) <= 0:
        raise ValueError("扫描区域面积必须大于零")
    return points, bbox


def _area_scale(points):
    latitude = (min(p[1] for p in points) + max(p[1] for p in points)) / 2
    return (_R * math.pi / 180) ** 2 * math.cos(math.radians(latitude))


def _polygon_area(points):
    # Translate before shoelace to avoid subtracting large nearly equal values.
    ox, oy = points[0]
    translated = [(x - ox, y - oy) for x, y in points]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in
                   zip(translated, translated[1:]))) / 2 * _area_scale(points)


def _intervals(points, x):
    crossings = []
    for (a, b), (c, d) in zip(points, points[1:]):
        if min(a, c) <= x < max(a, c):
            crossings.append(b + (d - b) * (x - a) / (c - a))
    crossings.sort()
    return list(zip(crossings[0::2], crossings[1::2]))


def _merge(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1] + _EPS:
            result[-1] = (result[-1][0], max(result[-1][1], b))
        else:
            result.append((a, b))
    return result


def _union_area(points, rectangles):
    """Area of polygon ∩ rectangle union, without overlap double counting.

    Polygon and horizontal rectangle-edge crossings partition the plane into
    slabs whose clipped vertical lengths are linear. Midpoint integration is
    exact in local equirectangular coordinates; m² is explicitly approximate.
    """
    rectangles = [r for r in rectangles if _valid_bbox(r)]
    if not rectangles:
        return 0.0
    xs = {p[0] for p in points}
    for w, s, e, n in rectangles:
        xs.update((w, e))
        for (a, b), (c, d) in zip(points, points[1:]):
            if d != b:
                for y in (s, n):
                    t = (y - b) / (d - b)
                    if 0 < t < 1:
                        xs.add(a + (c - a) * t)
    xs = sorted(xs)
    total = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        middle = (left + right) / 2
        rect_intervals = _merge([(s, n) for w, s, e, n in rectangles if w <= middle < e])
        vertical_length = sum(max(0, min(b, d) - max(a, c))
                              for a, b in _intervals(points, middle) for c, d in rect_intervals)
        total += (right - left) * vertical_length
    return total * _area_scale(points)


def _bbox_inside_region(points, bbox):
    """Only a wholly contained image can attribute all pixel candidates.

    Boundary frames deliberately extend beyond the selected region. Without
    a validated candidate-to-ground transform, overlap alone cannot attribute
    a visible object to the confirmed region.
    """
    bbox = _valid_bbox(bbox)
    if bbox is None:
        return False
    w, s, e, n = bbox
    rectangle_area = (e - w) * (n - s) * _area_scale(points)
    return _union_area(points, [bbox]) >= rectangle_area * (1 - 1e-8)


def _world(lon, lat, world):
    lat = min(_LIMIT_Y, max(-_LIMIT_Y, lat))
    sine = math.sin(math.radians(lat))
    return (lon + 180) / 360 * world, (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * world


def _lonlat(x, y, world):
    return x / world * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / world))))


def _axis_starts(low, high, size, stride):
    if high - low <= size:
        return [(low + high - size) / 2]
    count = math.ceil((high - low - size) / stride) + 1
    if count > 100_000:
        return None
    # Last frame anchors to outer boundary; no hidden tail gaps.
    return [min(low + i * stride, high - size) for i in range(count)]


def plan_scan(spec: ComplaintScan | dict) -> dict:
    spec = spec if isinstance(spec, ComplaintScan) else ComplaintScan.model_validate(spec)
    points, bbox = _region(spec)
    if points is None:
        return {"status": "needs_location", "windows": [], "request": spec.model_dump(mode="json"),
                "next_action": "先根据举报地名检索候选地点，再让用户确认扫描边界；不能扫描整个省市。"}
    area = _polygon_area(points)
    base = {"version": "complaint-scan-v1", "region_bbox": list(bbox),
            "region_polygon": {"type": "Polygon", "coordinates": [[list(p) for p in points]]},
            "region_confirmed": spec.region_confirmed, "area_m2_approx": round(area, 2),
            "area_method": "local_equirectangular_polygon_union_approximate",
            "zoom": spec.zoom, "window_pixels": spec.window_pixels, "overlap": spec.overlap,
            "max_windows": spec.max_windows, "request": spec.model_dump(mode="json"), "windows": []}
    if area / 1_000_000 > spec.max_area_km2:
        return base | {"status": "over_budget", "required_area_km2": round(area / 1_000_000, 3),
                       "next_action": "区域超过本次面积预算，请缩小并确认边界或明确提高预算。"}
    world = 256 * (2 ** spec.zoom)
    w, s, e, n = bbox
    x0, y1 = _world(w, s, world)
    x1, y0 = _world(e, n, world)
    stride = spec.window_pixels * (1 - spec.overlap)
    xs = _axis_starts(x0, x1, spec.window_pixels, stride)
    ys = _axis_starts(y0, y1, spec.window_pixels, stride)
    if xs is None or ys is None or len(xs) * len(ys) > 100_000:
        return base | {"status": "over_budget", "required_windows": None,
                       "next_action": "高清窗口数量过大，请缩小扫描区域；不会截断后冒充覆盖完成。"}
    frames = []
    for row, y in enumerate(ys):
        for column, x in enumerate(xs):
            west, north = _lonlat(x, y, world)
            east, south = _lonlat(x + spec.window_pixels, y + spec.window_pixels, world)
            frame_bbox = [max(-180, west), max(-85, south), min(180, east), min(85, north)]
            if _union_area(points, [frame_bbox]) <= 0.01:
                continue
            lon, lat = _lonlat(x + spec.window_pixels / 2, y + spec.window_pixels / 2, world)
            frames.append({"window_id": f"r{row + 1:04d}c{column + 1:04d}", "bbox": frame_bbox,
                           "center": [lon, lat], "zoom": spec.zoom,
                           "width": spec.window_pixels, "height": spec.window_pixels})
    base["required_windows"] = len(frames)
    if len(frames) > spec.max_windows:
        return base | {"status": "over_budget", "next_action": "所需窗口超过预算，请缩小区域、降低级别或明确提高窗口预算；未执行取图。"}
    base["windows"] = frames
    base["status"] = "ready" if spec.region_confirmed else "needs_region_confirmation"
    base["next_action"] = "按确认区域逐窗取图，并对清晰度、坐标和模型完成情况逐项记账。" if spec.region_confirmed else "请先在地图上确认此扫描区域及窗口数量。"
    digest_payload = {k: base[k] for k in ("region_polygon", "zoom", "window_pixels", "overlap", "windows")}
    base["plan_id"] = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return base


def plan_window_tasks(base_task: TaskRequest | dict, plan: dict) -> list[TaskRequest]:
    """Build ordinary pipeline tasks only after the plan's region is confirmed.

    The server must look up its persisted plan; do not use arbitrary client
    plan dictionaries as authorization. This function does not submit jobs.
    """
    if plan.get("status") != "ready" or plan.get("region_confirmed") is not True:
        raise ValueError("需先确认扫描范围及预算，再提交逐窗任务")
    base = base_task if isinstance(base_task, TaskRequest) else TaskRequest.model_validate(base_task)
    windows = plan.get("windows", [])
    if not windows or len(windows) > plan.get("max_windows", 0):
        raise ValueError("扫描计划窗口无效或超出预算")
    result = []
    for frame in windows:
        task = base.model_dump(mode="json")
        scan_window = {**plan["request"], "region_confirmed": True,
                       "window_id": frame["window_id"], "window_bbox": frame["bbox"]}
        overrides = {"bbox": frame["bbox"], "center": None, "coordinate_system": "wgs84",
                     "acquire_optical": True, "complaint_scan": scan_window, "view_mode": "single",
                     "site_area_m2": None, "asset_ids": [],
                     "objective": f"{base.objective}\n本次仅核查已确认区域的窗口{frame['window_id']}，不能将本窗结果外推到未覆盖区域。"}
        task.update(overrides)
        window_task = TaskRequest.model_validate(task)
        # Full serialization is a storage format, not evidence that every
        # default value was explicitly requested by the user. Keep the actual
        # input contract plus immutable server-generated window constraints.
        window_task.model_fields_set.intersection_update(set(base.model_fields_set) | set(overrides))
        result.append(window_task)
    return result


def _date_values(row):
    values = row.get("actual_dates") or ([row["actual_date"]] if row.get("actual_date") else [])
    if not isinstance(values, (list, tuple)) or not values or len(values) > 100:
        return []
    try:
        return [date.fromisoformat(str(value)[:10]) for value in values]
    except (ValueError, TypeError):
        return []


def scan_status(plan: dict, ledger: list[dict], period_start=None, period_end=None, *, trusted=False) -> dict:
    """Account only server-derived footprints and actual completed analysis.

    trusted=False keeps MCP/ad-hoc input as a planning rehearsal and never
    certifies negative evidence. The HTTP controller uses persisted own runs.
    """
    points, _ = _region(ComplaintScan(region_polygon=plan.get("region_polygon")))
    if points is None:
        raise ValueError("扫描计划缺少有效范围")
    if len(ledger) > 4096:
        raise ValueError("扫描账本超过4096条，请分区域审计")
    start = date.fromisoformat(str(period_start)[:10]) if period_start else None
    end = date.fromisoformat(str(period_end)[:10]) if period_end else None
    if start and end and start > end:
        raise ValueError("核查期间开始不得晚于结束")
    frames = {frame["window_id"]: frame for frame in plan.get("windows", [])}
    planned_rects, reviewed, time_qualified, downloaded, negative_reviewed = [], [], [], [], []
    validated, issues, findings, conditional_findings = [], [], [], []
    reviewed_ids, time_ids = set(), set()
    for row in ledger:
        window_id = row.get("window_id")
        frame = frames.get(window_id)
        if frame is None:
            issues.append({"window_id": window_id, "issue": "unknown_window"})
            continue
        if not isinstance(row.get("source"), str) or not row.get("source"):
            issues.append({"window_id": window_id, "issue": "missing_source"})
            continue
        if row.get("requested_bbox") is not None:
            requested = _valid_bbox(row["requested_bbox"])
            if requested is None or any(abs(a - b) > 1e-7 for a, b in zip(requested, frame["bbox"])):
                issues.append({"window_id": window_id, "issue": "requested_region_mismatch"})
                continue
        if row.get("status") in {"succeeded", "completed"} and row.get("downloaded") is True:
            downloaded.append(frame["bbox"])
        footprint = _valid_bbox(row.get("footprint_bbox"))
        image_wholly_in_region = _bbox_inside_region(points, footprint)
        reasons = []
        if row.get("status") not in {"succeeded", "completed"}:
            reasons.append("task_not_completed")
        if not row.get("run_id") or not row.get("asset_id"):
            reasons.append("missing_evidence_reference")
        if row.get("downloaded") is not True:
            reasons.append("image_not_received")
        if row.get("spatial_verified") is not True or footprint is None:
            reasons.append("footprint_not_verified")
        if row.get("quality_passed") is not True:
            reasons.append("quality_not_passed")
        if row.get("analyzed") is not True:
            reasons.append("model_not_completed")
        if footprint:
            # Crediting at most the task's own planned window prevents a
            # malformed result from claiming unrequested surrounding land.
            fw, fs, fe, fn = frame["bbox"]
            w, s, e, n = footprint
            footprint = _valid_bbox((max(fw, w), max(fs, s), min(fe, e), min(fn, n)))
            if footprint is None:
                reasons.append("footprint_outside_window")
        dates = _date_values(row)
        dated = bool(dates) and row.get("actual_date_scope") == "full_footprint"
        if dated and ((start and any(v < start for v in dates)) or (end and any(v > end for v in dates))):
            dated = False
        usable = not reasons
        candidate_reported = row.get("reviewable_finding") is True
        evidence_reference_present = bool(row.get("run_id") and row.get("asset_id"))
        finding_relevance = None
        if candidate_reported and evidence_reference_present and row.get("downloaded") is True and row.get("analyzed") is True:
            reference = {"window_id": window_id, "run_id": row["run_id"],
                         "asset_id": row["asset_id"], "source": row["source"]}
            if usable and image_wholly_in_region:
                finding_relevance = "inside_confirmed_region"
                findings.append({**reference, "region_relevance": finding_relevance})
            else:
                finding_relevance = "in_window_unlocalized"
                conditional_findings.append({**reference, "region_relevance": finding_relevance,
                    "reason": "image_extends_outside_confirmed_region" if usable else "quality_or_spatial_support_unverified",
                    "limitation": "疑点见于已取得的扫描窗口，尚未验证对象是否落在用户确认范围；不能把窗口与范围相交当作对象定位。"})
        if usable:
            reviewed.append(footprint)
            reviewed_ids.add(window_id)
            if row.get("negative_review_complete") is True and not candidate_reported:
                negative_reviewed.append(footprint)
            if dated:
                time_qualified.append(footprint)
                time_ids.add(window_id)
        validated.append({"window_id": window_id, "source": row["source"], "run_id": row.get("run_id"),
                          "asset_id": row.get("asset_id"), "visual_reviewed": usable,
                          "time_qualified": usable and dated, "actual_dates": [d.isoformat() for d in dates],
                          "finding_region_relevance": finding_relevance,
                          "exclusions": reasons, "date_issue": None if dated else "actual_date_unknown_outside_period_or_partial_scope"})
    for frame in frames.values():
        planned_rects.append(frame["bbox"])
    area = _polygon_area(points)
    metric = lambda rects: {"area_m2_approx": round(_union_area(points, rects), 2),
                          "fraction": round(min(1.0, _union_area(points, rects) / area), 6)}
    planned, retrieved, visual, temporal, negative = map(metric, (planned_rects, downloaded, reviewed, time_qualified, negative_reviewed))
    complete_visual = visual["fraction"] >= .999999
    complete_time = temporal["fraction"] >= .999999
    complete_negative = negative["fraction"] >= .999999
    if not trusted:
        state, conclusion = "unverified_ledger", "账本仅作计划演练，尚未从服务端任务和证据记录核实，不能生成已核查结论。"
    elif findings:
        state, conclusion = "findings_pending_review", "已核查影像中发现可复核疑似线索；其余范围、时相与盲区分别列示，需结合原图和现场核查。"
    elif conditional_findings:
        state, conclusion = "needs_spatial_review", "取得的扫描窗口有疑点，需复核是否落在确认区域；边界外上下文或位置支持未核实的线索，不能直接归到本次举报范围，也不能据此称为全区未发现。"
    elif complete_visual and not complete_negative:
        state, conclusion = "inconclusive_review", "确认区域影像已审阅，但部分区域没有形成清晰的有效排除或可复核疑点，仍需补充影像或现场线索，不能称为全区未发现。"
    elif complete_visual and complete_time:
        state, conclusion = "no_reviewable_finding_in_scope", "在本次确认区域、有效覆盖影像及其已知成像时点内，未发现可复核的露天堆存疑点；不等于举报不实或当前不存在倾倒。"
    elif complete_visual:
        state, conclusion = "visual_review_complete_time_unqualified", "确认区域的有效影像已逐窗审阅，未形成可复核疑点；实际成像时点未完全满足举报期间，不能据此排除该期间或当前情况。"
    else:
        state = "incomplete_coverage"
        conclusion = ("尚未完成可用于区域结论的有效影像核查，仍需保留未取图、质量未满足、模型分析未完成或位置未核实的缺口，不能给出全区未发现结论。"
                      if visual["fraction"] == 0 else
                      "已完成部分影像核查，仍有未取到、不可判读、未完成模型分析或位置未核实的范围，不能给出全区未发现结论。")
    return {"status": state, "trusted_ledger": trusted, "plan_id": plan.get("plan_id"),
            "region_bbox": plan.get("region_bbox"), "region_polygon": plan.get("region_polygon"),
            "complaint_text": (plan.get("request") or {}).get("text", ""),
            "period_start": start.isoformat() if start else None, "period_end": end.isoformat() if end else None,
            "conclusion": conclusion, "region_area_m2_approx": round(area, 2), "area_method": "local_equirectangular_polygon_union_approximate",
            "planned_coverage": planned, "downloaded_requested_coverage": retrieved,
            "visual_reviewed_coverage": visual, "time_qualified_coverage": temporal,
            "qualified_negative_coverage": negative,
            "planned_windows": len(frames), "visual_reviewed_windows": len(reviewed_ids), "time_qualified_windows": len(time_ids),
            "missing_visual_window_ids": sorted(set(frames) - reviewed_ids), "time_unqualified_window_ids": sorted(set(frames) - time_ids),
            "findings": findings, "conditional_findings": conditional_findings, "ledger": validated, "ledger_issues": issues,
            "limitations": ["覆盖面积为近似投影计算，重叠区按几何并集计一次，不作为测绘面积。",
                            "可见地表审阅不能核实室内堆物、短时液体排放、转移备案、许可证、化学组成或危险属性。",
                            "多图源可能同源、异时或有偏移；未经有效配准不能当作同点变化证据。"]}


def draft_complaint(text: str) -> dict:
    """Extract grounded phrases, not guessed coordinates or company identities."""
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError("举报文本应为1至12000字")
    # Only spans present in supplied material are returned. Named-entity and
    # address resolution belongs to the configured geocoder/main model.
    locations = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,24}(?:省|自治州|市|县|镇|乡|村委会|村|园区|工业园|路|沟|箐)", text):
        phrase = match.group(0)
        if phrase not in locations:
            locations.append(phrase)
    dates = list(dict.fromkeys(re.findall(r"20\d{2}年\d{1,2}月(?:\d{1,2}日)?|20\d{2}-\d{2}-\d{2}", text)))
    limitations = []
    if re.search(r"某|匿名|附近|一带|周边|大概|不清楚|不知道", text):
        limitations.append("位置存在匿名或模糊描述，候选地名不能当成已定位的倾倒点。")
    if re.search(r"废水|尾液|液体|渗漏|污水|夜间|洒水车", text):
        limitations.append("液体、短时或夜间行为可能不留可见卫星痕迹，影像未检出不能排除。")
    if re.search(r"仓库|室内|焚烧房|厂房内", text):
        limitations.append("室内和遮挡区无法用常规光学卫星确认堆物，需现场或台账补充。")
    if re.search(r"跨省|备案|联单|资质|许可证|合同", text):
        limitations.append("转移合规、许可、合同和联单属于非影像证据，不由卫星判断。")
    return {"status": "needs_location_and_region_confirmation", "complaint_text": text,
            "grounded_place_phrases": locations[:20], "grounded_time_phrases": dates[:20],
            "coordinates": None, "region_confirmed": False, "limitations": limitations,
            "next_action": "检索原文地名的候选位置；若仍不能唯一定位，询问附近道路、村组或可在地图上圈定的区域，再确认扫描预算。",
            "untrusted_material": True, "execution_instructions_from_text": "ignored"}


def complaint_examples() -> dict:
    """User-authorized case adaptation, never a labelled benchmark answer key."""
    cases = [
        ("yunnan-01", "玉溪一处选矿企业附近有散状物料外运，听说运输车辆去往外省。企业名称和厂址不详，请先帮我明确还需哪些位置或运输线索；有位置后核查露天堆场变化。", "needs_location", ["堆场外观及变化"], ["跨省运输及手续不能由影像核实"]),
        ("yunnan-02", "开远市有一家植物开发企业，听说生产后清出的杂料由运煤车辆带走，怀疑在外面堆放。企业名和卸料点还不清楚，请帮我整理需要补充的线索。", "needs_location", ["若得到卸料点可排查露天异常堆物"], ["不可凭企业类别或历史案件猜坐标", "小规模堆物可能低于分辨率"]),
        ("yunnan-03", "玉溪一家新材料企业厂区西北面，污水处理站旁挡墙的低洼处，可能出现了堆物；另有人反映厂内焚烧房有异常。具体企业还不清楚，希望先定位后核查。", "needs_location", ["厂区西北角露天低洼处"], ["匿名企业无法可靠定位", "室内焚烧和物料危险属性不可由影像确认"]),
        ("yunnan-04", "鲁甸县某企业清运出来的物料，可能被堆到县内几处地方；有一处挨着沟渠和鱼塘。我不知道村名，请先告诉我如何缩小范围，后续想看各处堆场和周边水体关系。", "needs_location", ["露天堆场及沟渠鱼塘关系"], ["不能把全县盲扫作为已核查", "影像不能确认鱼类死亡原因"]),
        ("yunnan-05", "通海县里山乡五山村老下寨坡头箐沟和香胡路附近，有人反映2026年2月25日晚至26日凌晨有洒水车卸液体。请先给出候选范围让我圈选，再核查道路周边和沟谷是否有可见异常。", "candidate_place_then_confirm", ["道路旁地表扰动、沟谷周边异常"], ["液体和夜间短时排放可能不可见", "未发现痕迹不能反证举报"]),
        ("yunnan-06", "文山市某环保企业产生的炉渣，听说被另一家矿渣销售企业拉走。两家企业名称地址暂不清楚，想核查卸料去向和有无露天堆放，请先说明需要补充什么。", "needs_location", ["若补充地点，可核查露天场地变化"], ["转移手续、炉渣成分和危险属性需非影像证据"]),
        ("yunnan-07", "金平县一处矿业企业选厂东侧排土场，近期好像出现新增露天堆料，据说从尾矿库运来。企业名称暂时不详，想核查堆场变化及周边沟谷。", "needs_location", ["排土场范围、露天堆体变化"], ["物料来源及成分不能凭外观确认", "同类矿业场地不能替代具体定位"]),
        ("yunnan-08", "瑞丽市某村民小组闲置地块，2025年1月至5月间有人从混凝土企业拉料来堆，后来可能清理和覆土了。村名和地块还不清楚，想补充定位后做清理前后核查。", "needs_location", ["闲置地堆物、覆土和恢复变化"], ["不能由清理后底图判断历史无堆放", "版次年份不能当实际拍摄日"]),
        ("yunnan-09", "楚雄高新区庄甸医药园区一间仓库，有人反映里面放了湿的杂料并有液体渗出。请先确定园区候选范围，说明影像能看见什么、还需哪些现场资料。", "candidate_place_then_confirm", ["仓库位置及周边可见地表"], ["仓库内部堆存不可见", "液体成分和来源需现场检测"]),
        ("yunnan-10", "富源县墨红镇玉麦村委会朵把朵村周边，有人反映矿业企业在露天堆了大片深色碎石状物料。具体地块不清楚，请先让我确认扫描范围，再检查各窗口、道路及沟谷周边。", "candidate_place_then_confirm", ["村周边露天堆料及范围", "道路、坡地和沟谷关系"], ["煤矸石等具体物料和危险属性不能仅凭颜色确认", "需用户确认村界或圈选范围"]),
    ]
    return {"status": "drafts_only_not_investigated", "source": "user_provided_anonymized_yunnan_case_text",
            "adaptation": "原文仅用于生成模拟举报；去除处罚结果、数量、已确认废物/危险属性与违法认定，不作为模型答案或核查事实。",
            "cases": [{"case_id": ident, "complaint": text, "expected_routing": route,
                       "observable_targets": targets, "limitations": limitations,
                       "requires_region_confirmation": True, "live_scan_executed": False}
                      for ident, text, route, targets, limitations in cases]}
