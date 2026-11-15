"""Offline, explicitly manual spatial audit of existing ArcGIS and S2 rasters.

No image or metadata downloads. Control points are approximate human readings,
not a surveyed ground-control network or automatic feature matches.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.warp import Resampling, reproject, transform


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs/portal-smoke/engine/runs/0c3c045d728f44d2bc34c7826e6ce5bb"
OUT = ROOT / "outputs/point-timeseries-202511-now/spatial"
ASSET_ID = "4a4a79ef492c4f1faa0ce0323058634d"
S2 = ROOT / "outputs/march-2026-check/S2-20260311-RGB.tif"
S2_PNG = ROOT / "outputs/imagery-preview-2026/S2-20260311-RGB-native.png"

# Pixel coordinates are continuous distances from the upper-left raster corner.
# Points describe broad, visible feature centres, not subpixel corner estimates.
CONTROL_POINTS = [
    ("C1", "主干道交叉口中心", [253, 354], [31.5, 43.3], 12),
    ("C2", "校园北路与西侧主路交汇", [232, 646], [28.9, 80.3], 12),
    ("C3", "田径场绿色内场中心", [355, 779], [44.8, 96.6], 10),
    ("C4", "西侧蓝色球场组中心", [481, 849], [60.0, 105.5], 10),
    ("C5", "东侧蓝色球场组中心", [589, 864], [73.1, 106.7], 12),
    ("C6", "浅色条状厂房屋面组中心", [327, 466], [41.2, 57.8], 15),
]

# These are manually traced contextual analysis windows. They are deliberately
# not represented as authoritative waste polygons or the workbook's red line.
REGIONS = [
    ("candidate_ground", "浅色不规则地表候选区", "#ff4040", [
        [475, 425], [494, 421], [525, 437], [540, 457], [541, 484],
        [542, 509], [532, 535], [519, 555], [504, 543], [489, 525],
        [481, 501], [474, 473], [463, 451],
    ]),
    ("candidate_material_patch", "较小斑杂浅色片区（人工变化观察窗）", "#ff00ff", [
        [496, 476], [513, 477], [533, 490], [538, 510], [529, 531],
        [514, 537], [500, 526], [490, 508], [488, 491],
    ]),
    ("blue_roof", "蓝色屋顶对照", "#00aaff", [
        [310, 510], [433, 452], [447, 478], [320, 538],
    ]),
    ("gray_roof", "灰色屋顶对照", "#eeeeee", [
        [416, 350], [439, 339], [468, 393], [446, 405],
    ]),
    ("farmland", "耕地背景对照", "#66ff66", [
        [582, 490], [610, 477], [634, 528], [602, 541],
    ]),
]


def xy_transform(src: str, dst: str, xy: list[list[float]]) -> list[list[float]]:
    x, y = transform(src, dst, [p[0] for p in xy], [p[1] for p in xy])
    return [list(p) for p in zip(x, y)]


def inside(point: list[float], ring: list[list[float]]) -> bool:
    x, y = point
    result = False
    for p, q in zip(ring, ring[1:] + ring[:1]):
        if (p[1] > y) != (q[1] > y):
            if x < (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]) + p[0]:
                result = not result
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    package = json.loads((RUN / "finding-package.json").read_text(encoding="utf-8-sig"))
    asset = next(a for a in package["assets"] if a["id"] == ASSET_ID)
    source = RUN / f"source-{ASSET_ID}.png"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == asset["sha256"]
    metadata = asset["data_api_metadata"]
    gt = metadata["affine_gdal"]

    def arc_xy(points):
        return [[gt[0] + p[0] * gt[1] + p[1] * gt[2],
                 gt[3] + p[0] * gt[4] + p[1] * gt[5]] for p in points]

    with rasterio.open(S2) as ds:
        s2_transform, s2_crs = ds.transform, str(ds.crs)
        s2_info = {"path": str(S2), "sha256": hashlib.sha256(S2.read_bytes()).hexdigest(),
                   "crs": s2_crs, "bounds": list(ds.bounds), "width": ds.width,
                   "height": ds.height, "transform": list(ds.transform), "tags": ds.tags(),
                   "native_band_resolution_m": 10,
                   "export_grid_sampling_m": list(ds.res),
                   "warning": "128-pixel nearest export is resampled; 6.25m grid is not native resolution."}

    def on_s2(points):
        projected = xy_transform("EPSG:3857", s2_crs, arc_xy(points))
        return [list((~s2_transform) * tuple(p)) for p in projected]

    readings = []
    for identifier, label, arc_pixel, s2_pixel, uncertainty in CONTROL_POINTS:
        arc_utm = xy_transform("EPSG:3857", s2_crs, arc_xy([arc_pixel]))[0]
        s2_utm = list(s2_transform * tuple(s2_pixel))
        delta = np.subtract(s2_utm, arc_utm)
        readings.append({"id": identifier, "feature": label,
                         "arcgis_pixel": arc_pixel, "s2_manual_pixel": s2_pixel,
                         "s2_predicted_from_arcgis_pixel": on_s2([arc_pixel])[0],
                         "delta_east_m": float(delta[0]), "delta_north_m": float(delta[1]),
                         "raw_distance_m": float(np.linalg.norm(delta)),
                         "manual_feature_location_uncertainty_m": uncertainty})
    deltas = np.array([[r["delta_east_m"], r["delta_north_m"]] for r in readings])
    mean = deltas.mean(axis=0)
    residual = deltas - mean
    for row, v in zip(readings, residual):
        row["translation_fit_residual_m"] = float(np.linalg.norm(v))
    fit = {"method": "Approximate manually selected feature centres; translation-only diagnostic in EPSG:32648. No warp applied.",
           "point_count": len(readings), "mean_east_offset_m": float(mean[0]),
           "mean_north_offset_m": float(mean[1]),
           "raw_rmse_m": float(np.sqrt(np.mean(np.sum(deltas**2, axis=1)))),
           "translation_fit_rmse_m": float(np.sqrt(np.mean(np.sum(residual**2, axis=1)))),
           "max_raw_distance_m": float(np.max(np.linalg.norm(deltas, axis=1))),
           "independent_accuracy_validation": False,
           "independent_check_point_count": 0,
           "sub_10m_offset_resolvable_from_these_points": False,
           "practical_interpretation": "Visible landmarks support the same site at approximately 10–20m scale; fitted 3.7m shift is below manual/native-pixel uncertainty and is not applied as a correction.",
           "limitation": "S2 native pixels are 10m and mixed. Approximate manual centre selections have 10–15m uncertainty. Fitted residuals are not surveyed positional accuracy or proof of pixel-level registration."}

    features = []
    for identifier, label, color, pixels in REGIONS:
        ring = pixels + [pixels[0]]
        coords = xy_transform("EPSG:3857", "EPSG:4326", arc_xy(ring))
        features.append({"type": "Feature", "id": identifier,
                         "properties": {"label": label, "source_asset_id": ASSET_ID,
                                        "source_sha256": asset["sha256"], "source_pixel_ring": ring,
                                        "digitization_method": "manual_visual_trace_on_existing_arcgis_export",
                                        "geometry_role": "context_analysis_roi_not_authoritative_waste_boundary",
                                        "interpretation": "Artificial observation window drawn from visible texture/color; material identity and exact physical boundary are not established by this polygon.",
                                        "approximate_spatial_uncertainty_m": 15,
                                        "native_resolution_m_for_entire_roi": None,
                                        "whole_roi_acquisition_time": None,
                                        "date_point_checks": "arcgis-date-point-checks.json",
                                        "not_workbook_red_line": True,
                                        "color": color},
                         "geometry": {"type": "Polygon", "coordinates": [coords]}})
    request = [104.461117, 30.858473]
    req3857 = xy_transform("EPSG:4326", "EPSG:3857", [request])[0]
    req_pixel = [(req3857[0] - gt[0]) / gt[1], (req3857[1] - gt[3]) / gt[5]]
    features.append({"type": "Feature", "id": "requested_point", "properties": {
        "label": "原表/请求中心", "crs_declaration": "workbook location QR links declare WGS84",
        "arcgis_pixel": req_pixel,
        "inside_hand_digitized_candidate_roi": inside(req_pixel, REGIONS[0][3]),
        "inside_smaller_material_observation_window": inside(req_pixel, REGIONS[1][3]),
    }, "geometry": {"type": "Point", "coordinates": request}})
    geojson = {"type": "FeatureCollection", "name": "manual_context_rois",
               "features": features,
               "note": "No authoritative area or waste classification inferred; no registration of workbook T/U polygon."}
    (OUT / "candidate-and-controls.geojson").write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")

    arc_im = Image.open(source).convert("RGB")
    # Common georeferenced display grids, centred on the exact requested point.
    # Reprojecting this existing RGB export does not create native 1m imagery.
    center_utm = xy_transform("EPSG:4326", s2_crs, [request])[0]
    grids = []
    rgb_array = np.asarray(arc_im).transpose(2, 0, 1)
    for size in (400, 200):
        left, top = center_utm[0] - size / 2, center_utm[1] + size / 2
        dst_transform = rasterio.transform.from_origin(left, top, 1, 1)
        dst_array = np.zeros((3, size, size), dtype=np.uint8)
        for band in range(3):
            reproject(rgb_array[band], dst_array[band], src_transform=rasterio.Affine.from_gdal(*gt),
                      src_crs="EPSG:3857", dst_transform=dst_transform, dst_crs=s2_crs,
                      resampling=Resampling.nearest)
        path = OUT / f"arcgis-reference-{size}m.tif"
        with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=3,
                           dtype="uint8", transform=dst_transform, crs=s2_crs, compress="deflate") as ds:
            ds.write(dst_array)
            ds.update_tags(source_asset_id=ASSET_ID, source_sha256=asset["sha256"],
                           georeferencing_basis=metadata["georeferencing_basis"],
                           raster_role="display_reference_grid_not_native_satellite_product",
                           acquisition_time="unknown_for_entire_window",
                           point_metadata_date="2025-01-09; 5 individual query points only",
                           native_resolution_m="unknown_for_entire_window", resampling="nearest")
        grid = {"path": str(path), "crs": s2_crs, "width": size, "height": size,
                "bounds": [left, top-size, left+size, top], "transform": list(dst_transform),
                "display_pixel_spacing_m": 1, "native_resolution_m": None,
                "center_wgs84": request, "purpose": "Common geometry for display-only comparisons; S2 remains native 10m."}
        grids.append(grid)
    (OUT / "reference-grids.json").write_text(json.dumps(grids, ensure_ascii=False, indent=2), encoding="utf-8")
    s2_im = Image.open(S2_PNG).convert("RGB").resize((1024, 1024), Image.Resampling.NEAREST)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 21)
    for im, s2 in [(arc_im, False), (s2_im, True)]:
        draw = ImageDraw.Draw(im)
        for identifier, label, color, pixels in REGIONS:
            ring = on_s2(pixels) if s2 else pixels
            ring = [(p[0] * 8, p[1] * 8) if s2 else tuple(p) for p in ring]
            draw.line(ring + [ring[0]], fill=color, width=3)
        pt = on_s2([req_pixel])[0] if s2 else req_pixel
        if s2:
            pt = [v * 8 for v in pt]
        x, y = pt
        draw.line((x-14, y, x+14, y), fill="black", width=6)
        draw.line((x, y-14, x, y+14), fill="black", width=6)
        draw.line((x-12, y, x+12, y), fill="white", width=2)
        draw.line((x, y-12, x, y+12), fill="white", width=2)
        for row in readings:
            p = row["s2_manual_pixel"] if s2 else row["arcgis_pixel"]
            x, y = [v * 8 for v in p] if s2 else p
            draw.ellipse((x-7, y-7, x+7, y+7), outline="yellow", width=3)
            draw.text((x+10, y-21), row["id"], font=font, fill="yellow", stroke_width=2, stroke_fill="black")
            if s2:
                qx, qy = [v*8 for v in row["s2_predicted_from_arcgis_pixel"]]
                draw.rectangle((qx-5, qy-5, qx+5, qy+5), outline="white", width=2)
        im.save(OUT / ("arcgis-marked.png" if not s2 else "s2-20260311-marked.png"))

    canvas = Image.new("RGB", (2108, 1260), "white")
    canvas.paste(arc_im, (20, 115)); canvas.paste(s2_im, (1064, 115))
    draw = ImageDraw.Draw(canvas)
    lines = [
        "同一点位空间核查｜人工可见控制点；不是自动精配准",
        "左：ArcGIS现发底图（5处来源日期元数据均为2025-01-09；不扩充为整景保证）　右：2026-03-11 Sentinel-2C RGB",
        "红：较大浅色场地　紫：较小斑杂片区　蓝/白：屋顶　绿：耕地；均为手绘观察窗；十字：原表点",
    ]
    for y, line in zip([8, 43, 78], lines):
        draw.text((20, y), line, font=font if y == 8 else small, fill="black")
    draw.text((20, 1150), f"黄圈C1–C6：人工选取的同类地物中心；右图白方框：原坐标预测位置。平移拟合残差RMS {fit['translation_fit_rmse_m']:.1f}m。", font=font, fill="black")
    draw.text((20, 1187), "S2原生10m，128格导出约6.25m系重采样。人工读点不确定约10–15m；不能将图中放大格或拟合残差当定位精度。", font=small, fill="black")
    draw.text((20, 1220), "红线是本次高分底图候选ROI，不是原表T13/U13红线；原表红线没有可独立配准的地标或地理坐标。", font=small, fill="black")
    canvas.save(OUT / "spatial-correspondence.png")
    result = {"source_asset": asset, "s2": s2_info, "control_points": readings,
              "translation_diagnostic": fit, "requested_point": features[-1],
              "regions_geojson": str(OUT / "candidate-and-controls.geojson"),
              "reference_grids": grids,
              "workbook_red_line_registration": "not_demonstrated_no_matching_landmarks_or_georeferencing",
              "temporal_conflict": "Current ArcGIS source metadata at 5 points is 2025-01-09; cannot interpret the whole visible site as first appearing in 2026. Workbook March-April 'new' could refer to a local change, but its exact polygon is unregistered."}
    (OUT / "spatial-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "fit": fit, "requested_point": features[-1]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
