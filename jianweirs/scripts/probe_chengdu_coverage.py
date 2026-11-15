"""Bounded catalogue-footprint coverage for every Chengdu workbook point.

Only scene searches are issued: no raster, preview, model or full-scene download.
Workbook coordinates are provisionally queried as WGS84, never certified here.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import sys

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from wuyu_harness.data import DataClient, DataClientError

PERIODS = {
    "before": {"label": "前期", "start": "2026-01-01T00:00:00Z", "end": "2026-02-28T23:59:59Z"},
    "after": {"label": "后期", "start": "2026-03-01T00:00:00Z", "end": "2026-04-30T23:59:59Z"},
}
LABELS = {"catalog_footprint_hit": "目录足迹命中", "no_catalog_hit_in_query": "本次目录未命中",
          "unknown": "未确定", "unsupported": "未接入检索"}


def point_in_ring(x: float, y: float, ring: list) -> int:
    """Return 0 outside, 1 inside or 2 on the boundary (GeoJSON lon/lat)."""
    if not isinstance(ring, list) or len(ring) < 4 or ring[0][:2] != ring[-1][:2]:
        raise ValueError("invalid polygon ring")
    inside = False
    for left, right in zip(ring, ring[1:]):
        if len(left) < 2 or len(right) < 2:
            raise ValueError("invalid polygon coordinates")
        x1, y1, x2, y2 = map(float, (*left[:2], *right[:2]))
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            raise ValueError("nonfinite geometry")
        if max(abs(x1), abs(x2)) > 180 or max(abs(y1), abs(y2)) > 90 or abs(x1-x2) > 180:
            raise ValueError("unsupported geographic ring")
        cross = (x-x1)*(y2-y1)-(y-y1)*(x2-x1)
        if abs(cross) <= 1e-10 and min(x1,x2)-1e-10 <= x <= max(x1,x2)+1e-10 and min(y1,y2)-1e-10 <= y <= max(y1,y2)+1e-10:
            return 2
        if (y1 > y) != (y2 > y) and x < x1+(y-y1)*(x2-x1)/(y2-y1):
            inside = not inside
    return int(inside)


def geometry_covers(geometry: object, longitude: float, latitude: float) -> bool:
    """Use actual polygons and holes; a scene bbox is deliberately never used."""
    if not isinstance(geometry, dict):
        raise ValueError("missing geometry")
    kind = geometry.get("type")
    if kind == "MultiPolygon":
        polygons = geometry.get("coordinates")
        if not isinstance(polygons, list) or not polygons:
            raise ValueError("missing polygons")
        return any(geometry_covers({"type":"Polygon", "coordinates":polygon}, longitude, latitude) for polygon in polygons)
    if kind == "GeometryCollection":
        geometries = geometry.get("geometries")
        if not isinstance(geometries, list) or not geometries:
            raise ValueError("missing geometries")
        return any(geometry_covers(item, longitude, latitude) for item in geometries)
    if kind != "Polygon":
        raise ValueError("unsupported geometry type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("missing polygon")
    shell = point_in_ring(longitude, latitude, coordinates[0])
    if shell == 0:
        return False
    if shell == 2:
        return True
    for hole in coordinates[1:]:
        state = point_in_ring(longitude, latitude, hole)
        if state == 2:
            return True
        if state == 1:
            return False
    return True


def period_contains(value: object, period: dict) -> bool:
    if not isinstance(value, str):
        raise ValueError("missing acquisition date")
    acquired = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(period["start"].replace("Z", "+00:00")) <= acquired <= datetime.fromisoformat(period["end"].replace("Z", "+00:00"))


def classify_point(point: dict, query: dict) -> dict:
    if query["status"] == "unsupported":
        return {"status":"unsupported", "reason":query["reason"], "scene_count":0, "scene_ids":[]}
    if query["status"] != "completed":
        return {"status":"unknown", "reason":query.get("reason", "query_not_completed"), "scene_count":0, "scene_ids":[]}
    coordinates = point["coordinates"]
    x, y = coordinates["longitude"], coordinates["latitude"]
    hits, unknown_geometry, unknown_time, excluded_time = [], 0, 0, 0
    for scene in query["scenes"]:
        try:
            if not period_contains(scene.get("acquired_at"), PERIODS[query["period"]]):
                excluded_time += 1
                continue
        except (ValueError, TypeError):
            unknown_time += 1
            continue
        try:
            if geometry_covers(scene.get("geometry"), x, y):
                hits.append({"id":scene["id"], "acquired_at":scene["acquired_at"], "cloud_cover_scene":scene.get("cloud_cover")})
        except (ValueError, TypeError, IndexError, OverflowError):
            unknown_geometry += 1
    if hits:
        status, reason = "catalog_footprint_hit", "point_intersects_actual_scene_polygon"
    elif query["possibly_truncated"] or unknown_geometry or unknown_time or excluded_time:
        status, reason = "unknown", "truncated_or_incomplete_scene_evidence"
    else:
        status, reason = "no_catalog_hit_in_query", "no_point_intersection_in_complete_returned_page"
    return {"status":status, "reason":reason, "scene_count":len(hits), "scene_ids":[scene["id"] for scene in hits],
            "scenes":hits, "query_id":query["id"], "possibly_truncated":query["possibly_truncated"],
            "invalid_or_missing_geometry_count":unknown_geometry, "missing_acquisition_time_count":unknown_time,
            "out_of_period_scene_count":excluded_time, "cloud_free_pixels_verified":False, "science_data_downloaded":False}


def render_html(report: dict, target: Path) -> None:
    def esc(value):
        return html.escape(str(value), quote=True)
    parts = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>成都36点科学影像目录覆盖</title>",
        "<style>body{font:15px/1.65 system-ui,'Microsoft YaHei';margin:28px;color:#203c42}h1,h2{color:#12655a}table{border-collapse:collapse;font-size:12px}th,td{border:1px solid #ccd8d8;padding:7px;vertical-align:top}th{background:#edf4f2;position:sticky;top:0}td.hit{background:#def0e2}td.unknown{background:#fff3d9}.scroll{overflow:auto}small{color:#566}.note{background:#f0f4f6;padding:16px}a{color:#176c86}</style>",
        "<h1>成都36点科学影像目录覆盖矩阵</h1>",
        f"<p>检索时间：{esc(report['checked_at'])}；点位 {report['point_count']}，科学源 {len(report['sources'])}，分前后两期；每源每期最多50景。</p>",
        "<div class='note'>原表坐标系未注明，G/H重复经度表头按数值推断为经度/纬度，本轮暂按WGS84检索，未核验地理配准。<br>命中仅表示点与目录真实足迹相交；不证明ROI无云、像元有效、已下载或可识别固废。没有下载全景、预览或调用模型。<br>结果达到limit、提供商报分页、查询失败、缺失几何或日期时，没有命中的点显示“未确定”，不能解释为没有影像覆盖。</div>",
        "<p>前期：2026-01-01—2026-02-28；后期：2026-03-01—2026-04-30。绿色为目录足迹命中，黄色为未知；“本次目录未命中”仅限本次完整返回页及指定时段。</p>",
        "<h2>各源检索证据</h2><table><tr><th>来源</th><th>前期</th><th>后期</th></tr>"]
    by_id = {query["id"]:query for query in report["queries"]}
    for source in report["sources"]:
        parts.append(f"<tr><td>{esc(source['source'])}<br><small>{esc(source['group'])}</small></td>")
        for period in PERIODS:
            query = by_id[source["source"]+"-"+period]
            parts.append(f"<td><a href='{esc(query['evidence_file'])}'>{esc(query['status'])}</a>；返回 {query.get('returned_scene_count',0)} 景<br>可能截断：{esc(query.get('possibly_truncated','未知'))}<br><small>{esc(query.get('reason',''))}</small></td>")
        parts.append("</tr>")
    parts.append("</table><h2>全部点位</h2><div class='scroll'><table><tr><th>点位与来源行</th>")
    for source in report["sources"]:
        parts.append(f"<th>{esc(source['source'])}</th>")
    parts.append("</tr>")
    for row in report["rows"]:
        parts.append(f"<tr><td>{esc(row['site_id'])}<br>{esc(row['administrative_area'])}，第{row['source']['row']}行<br><small>{esc(row['coordinates']['longitude'])}, {esc(row['coordinates']['latitude'])}</small></td>")
        for source in report["sources"]:
            pair = row["coverage"][source["source"]]
            css = "hit" if any(cell["status"]=="catalog_footprint_hit" for cell in pair.values()) else "unknown" if any(cell["status"]=="unknown" for cell in pair.values()) else ""
            parts.append(f"<td class='{css}'>")
            for period, cell in pair.items():
                detail = f"{PERIODS[period]['label']}：{LABELS[cell['status']]}"
                if cell["scene_count"]:
                    detail += f" {cell['scene_count']}景"
                parts.append(f"<div title='{esc(', '.join(cell['scene_ids'][:5]))}'>{esc(detail)}</div>")
            parts.append("</td>")
        parts.append("</tr>")
    parts.append("</table></div><p>逐点场景ID、拍摄时间、几何和截断标记见 <a href='coverage.json'>结构化矩阵</a> 与各源查询证据。完整性不等于供应商归档的绝对完整性。小样本科学数据下载的独立结果见 <a href='../source-audit/source-results.json'>图源实测记录</a>。</p></html>")
    target.write_text("\n".join(parts), encoding="utf-8")


async def main(options) -> None:
    load_dotenv(PROJECT / ".env.wuyu", override=False)
    body = json.loads(options.points.read_text(encoding="utf-8"))
    points = body["points"]
    if not points or len(points) > 100:
        raise ValueError("输入点位为空或超过本次100点探测上限")
    for point in points:
        coords = point["coordinates"]
        if not coords.get("numeric_valid"):
            raise ValueError("输入含无效数值坐标，请先核查原表")
    xs = [point["coordinates"]["longitude"] for point in points]
    ys = [point["coordinates"]["latitude"] for point in points]
    bbox = [min(xs)-.001, min(ys)-.001, max(xs)+.001, max(ys)+.001]
    if bbox[2]-bbox[0] > 5 or bbox[3]-bbox[1] > 5:
        raise ValueError("城市bbox超过科学目录5度限制，本脚本不会擅自扩大批量请求")
    folder = options.output.resolve()
    (folder / "queries").mkdir(parents=True, exist_ok=True)
    query_results = {}
    semaphore = asyncio.Semaphore(3)
    async with DataClient(max_wait_seconds=75) as data:
        catalog = await data.catalog()
        sources = []
        for group in ("multispectral", "hyperspectral", "sar"):
            for source in catalog.get("groups", {}).get(group, {}).get("sources", []):
                sources.append({"group":group, "source":source.get("source", source.get("id")),
                                "query_enabled":source.get("query_enabled", True),
                                "download_access":source.get("data_access", source.get("authentication")),
                                "spec":source})
        if not sources:
            raise ValueError("本机未返回科学源目录，不生成虚假覆盖矩阵")

        async def run_query(source: dict, period_id: str):
            period, source_id = PERIODS[period_id], source["source"]
            query_id = source_id+"-"+period_id
            relative = "queries/"+query_id+".json"
            target = folder / relative
            parameters = {"source":source_id, "bbox":bbox, "limit":50}
            if source["group"] == "sar":
                parameters.update(start=period["start"], end=period["end"])
            else:
                parameters["datetime"] = period["start"]+"/"+period["end"]
            fingerprint = hashlib.sha256(json.dumps({"endpoint":data.configuration_fingerprint,
                "query":parameters, "workbook_hash":body["source"]["sha256"]},sort_keys=True).encode()).hexdigest()
            if target.exists() and not options.force:
                previous = json.loads(target.read_text(encoding="utf-8"))
                if previous.get("fingerprint") == fingerprint:
                    query_results[query_id] = previous
                    print(query_id+" cached", flush=True)
                    return
            result = {"id":query_id, "source":source_id, "group":source["group"], "period":period_id,
                      "parameters":parameters, "fingerprint":fingerprint, "evidence_file":relative,
                      "checked_at":datetime.now(timezone.utc).isoformat(), "status":"unknown", "scenes":[],
                      "coordinate_assumption":"unverified_workbook_numeric_coordinates_as_wgs84", "science_data_downloaded":False}
            if source["query_enabled"] is False:
                result.update(status="unsupported", reason="catalog_search_not_implemented_for_source")
            else:
                async with semaphore:
                    try:
                        response = await data.execute(source["group"]+".search", parameters)
                        scene_key = "items" if source["group"] == "sar" else "scenes"
                        raw_scenes = response.get(scene_key)
                        if not isinstance(raw_scenes, list):
                            raise ValueError("required scene array absent")
                        scenes = [{key:scene.get(key) for key in ("id", "acquired_at", "geometry", "cloud_cover")}
                                  for scene in raw_scenes if isinstance(scene, dict)]
                        limited = bool(response.get("more_available") or response.get("results_limited") or len(raw_scenes)>=50)
                        result.update(status="completed", scenes=scenes, returned_scene_count=len(raw_scenes),
                                      malformed_scene_count=len(raw_scenes)-len(scenes),
                                      possibly_truncated=limited or len(scenes)!=len(raw_scenes),
                                      provider_more_available=response.get("more_available", response.get("results_limited")),
                                      reason="single_bounded_page_actual_scene_geometry", retrieved_at=response.get("retrieved_at"))
                    except DataClientError as error:
                        result.update(status="unknown", reason=error.code, http_status=error.status_code)
                    except (ValueError, TypeError):
                        result.update(status="unknown", reason="invalid_scene_catalog_response")
            target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            query_results[query_id] = result
            print(f"{query_id} {result['status']} scenes={result.get('returned_scene_count',0)} truncated={result.get('possibly_truncated','unknown')}",flush=True)

        await asyncio.gather(*(run_query(source, period) for source in sources for period in PERIODS))
        rows = []
        for point in points:
            rows.append({key:point[key] for key in ("record_id","site_id","source","coordinates","administrative_area")} |
                        {"coverage":{source["source"]:{period:classify_point(point,query_results[source["source"]+"-"+period])
                                                      for period in PERIODS} for source in sources}})
        report = {"schema_version":"chengdu_catalog_coverage_v1", "checked_at":datetime.now(timezone.utc).isoformat(),
                  "endpoint_fingerprint":data.configuration_fingerprint,
                  "source_registry_config_binding":"API origin and credential fingerprint must match; workbook coordinates and catalogue hits remain unverified for other configurations",
                  "point_count":len(points), "source_workbook":body["source"]["filename"], "source_workbook_sha256":body["source"]["sha256"],
                  "query_bbox":bbox, "query_limit_per_source_period":50, "periods":PERIODS,
                  "sources":[{key:value for key,value in source.items() if key!="spec"} for source in sources],
                  "queries":[query_results[source["source"]+"-"+period] for source in sources for period in PERIODS],
                  "rows":rows, "status_counts":dict(Counter(cell["status"] for row in rows for pair in row["coverage"].values() for cell in pair.values())),
                  "catalog_errors":catalog.get("errors",[]), "boundaries":[
                      "All workbook coordinates are provisional WGS84 assumptions, not verified geolocation",
                      "Only actual GeoJSON scene polygons including holes are tested; scene bboxes are never substitutes",
                      "Truncated/failed/missing geometry or acquisition time cannot establish absence of coverage",
                      "Scene footprint intersection does not establish cloud-free valid ROI pixels or complete target coverage",
                      "No imagery, preview, science files or model inference was downloaded/called"]}
        (folder / "coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        render_html(report, folder / "coverage.html")
        print(json.dumps({"point_count":len(points),"source_count":len(sources),"query_count":len(query_results),
                          "status_counts":report["status_counts"],"report":str(folder/"coverage.html")},ensure_ascii=True),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points",type=Path,default=PROJECT/"outputs/chengdu-points.json")
    parser.add_argument("--output",type=Path,default=PROJECT/"outputs/chengdu-coverage")
    parser.add_argument("--force",action="store_true")
    asyncio.run(main(parser.parse_args()))
