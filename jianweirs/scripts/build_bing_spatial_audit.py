"""Local audit of an existing unregistered Bing export against ArcGIS.

Reads a previously calculated classical SIFT exploratory fit, then reports its
scope and visually checkable landmark crops. No downloads or app modifications.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from rasterio.warp import transform


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/point-timeseries-202511-now/spatial"
RUN = ROOT / "outputs/portal-smoke/engine/runs/0c3c045d728f44d2bc34c7826e6ce5bb"
ARC_ID = "4a4a79ef492c4f1faa0ce0323058634d"
BING_ID = "5d813d42cbfb41b9af7849d66172cd4b"


def main():
    package = json.loads((RUN / "finding-package.json").read_text(encoding="utf-8-sig"))
    assets = {a["id"]: a for a in package["assets"]}
    images = {}
    for key in (ARC_ID, BING_ID):
        path = RUN / f"source-{key}.png"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == assets[key]["sha256"]
        images[key] = Image.open(path).convert("RGB")
    fit = json.loads((OUT / "bing-exploratory-feature-fit.json").read_text(encoding="utf-8"))
    matrix = np.array(fit["matrix_arcgis_to_bing"])
    gt = assets[ARC_ID]["data_api_metadata"]["affine_gdal"]

    def nominal_utm(p):
        x, y = gt[0] + p[0]*gt[1], gt[3] + p[1]*gt[5]
        xs, ys = transform("EPSG:3857", "EPSG:32648", [x], [y])
        return np.array([xs[0], ys[0]])

    def fit_pixel(p):
        return matrix[:, :2] @ np.array(p) + matrix[:, 2]

    # Three spatially distinct, visibly rechecked features from the classical
    # inlier set. Coordinates retain image-matching precision only.
    selected = [
        ("B1", "路口西侧设施铺装/花坛角", fit["inliers"][5]),
        ("B2", "路口东南小红顶建筑角", fit["inliers"][0]),
        ("B3", "田径场北侧道路铺装边", fit["inliers"][9]),
    ]
    controls = []
    for identifier, feature, pair in selected:
        a, b = pair["arcgis"], pair["bing"]
        pixel_delta = np.subtract(b, a)
        nominal_delta = nominal_utm(b) - nominal_utm(a)
        controls.append({"id": identifier, "feature": feature,
                         "arcgis_pixel": a, "bing_pixel": b,
                         "pixel_shift_bing_minus_arcgis": pixel_delta.tolist(),
                         "if_same_request_bbox_naively_assigned_to_bing_utm_shift_m": nominal_delta.tolist(),
                         "nominal_shift_distance_m": float(np.linalg.norm(nominal_delta)),
                         "selection": "Classical feature match; visually checked paired image crops, not a surveyed point."})
    target_arc = [512.0, 512.009592786274]
    target_bing = fit_pixel(target_arc)
    nominal_target_delta = nominal_utm(target_bing) - nominal_utm(target_arc)
    unique = {tuple(round(v, 2) for v in r["arcgis"]) for r in fit["inliers"]}
    result = {
        "arcgis_asset_id": ARC_ID, "bing_asset_id": BING_ID,
        "arcgis_sha256": assets[ARC_ID]["sha256"], "bing_sha256": assets[BING_ID]["sha256"],
        "source_package": str(RUN / "finding-package.json"),
        "bing_metadata": assets[BING_ID]["metadata"],
        "bing_data_api_metadata": assets[BING_ID]["data_api_metadata"],
        "bing_requested_view": assets[BING_ID]["requested_view"],
        "same_numeric_request_bbox": assets[BING_ID]["requested_view"]["bbox"] == assets[ARC_ID]["requested_view"]["bbox"],
        "bing_actual_bbox_returned": False, "bing_embedded_georeferencing": False,
        "bing_pixel_to_map_transform_returned": False,
        "image_relative_fit": {**{k: v for k, v in fit.items() if k != "inliers"},
                               "unique_inlier_locations": len(unique),
                               "scale_factor": float(np.hypot(matrix[0, 0], matrix[1, 0])),
                               "rotation_degrees": float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0]))),
                               "absolute_ground_accuracy_established": False},
        "visible_landmarks": controls,
        "requested_point": {"wgs84": [104.461117, 30.858473],
                            "arcgis_pixel": target_arc,
                            "inferred_bing_pixel_from_image_fit": target_bing.tolist(),
                            "bing_raster_center_pixel": [512, 512],
                            "distance_from_bing_raster_center_pixels": float(np.linalg.norm(target_bing-[512, 512])),
                            "nominal_offset_utm_east_north_m": nominal_target_delta.tolist(),
                            "nominal_offset_distance_m": float(np.linalg.norm(nominal_target_delta)),
                            "offset_scope": "Approximate displacement at ArcGIS ground display scale. Bing actual georeferencing was not returned."},
        "code_review": [
            {"file": str(ROOT/"omap_api/models.py"), "symbol": "SnapshotRequest.coord_type / capture_arguments",
             "observation": "Code describes 0 as CGCS2000 and 1 as GCJ-02; no automatic conversion."},
            {"file": str(ROOT/"omap_api/protocol.py"), "symbol": "OmapBridge.capture",
             "observation": "Request bbox coordinates are passed unchanged to 20302 x_lng1/y_lat1/x_lng2/y_lat2 and CoordType. Optional 20011 prepares the numeric bbox centre using the same CoordType."},
            {"file": str(ROOT/"omap_api/app.py"), "symbol": "snapshot result metadata",
             "observation": "Metadata intentionally marks bounds_are=requested_area_only, georeferenced=false, coordinate_system_verified=false, pixel_to_map_transform=null."},
            {"file": str(ROOT/"omap_api/optical.py"), "symbol": "latest",
             "observation": "ArcGIS branch requires numeric WGS84 and uses actual server extent; desktop Bing branch does not establish an actual geographic extent."},
        ],
        "causal_conclusion": "Verified hundreds-of-metres image-relative displacement. Exact cause is not established from existing metadata. Coordinate-code interpretation, custom map coordinate policy or desktop export origin are plausible; GCJ-02/WGS84 mixing is a hypothesis, not a proven diagnosis.",
        "routing_decision": "exclude_this_uncorrected_bing_asset_from_same_point_time_series_or_change_evidence",
        "must_not_infer": ["No cleanup/new dumping from the ArcGIS-vs-Bing difference", "No exact acquisition date for Bing", "No automatic global correction or known actual_bbox from this one local fit", "Fit residuals are not absolute ground accuracy"],
    }
    (OUT / "bing-spatial-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 25)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    patches = Image.new("RGB", (1000, 1100), "white")
    draw = ImageDraw.Draw(patches)
    draw.text((18, 8), "Bing相对位移：3处共同地标贴片复核", font=font, fill="black")
    draw.text((18, 45), "左ArcGIS，右Bing；贴片按各自真实像素取出，无配准变形", font=small, fill="black")
    for i, row in enumerate(controls):
        y = 90+i*310
        draw.text((18, y), f"{row['id']} {row['feature']}", font=font, fill="black")
        for j, (image_id, key) in enumerate([(ARC_ID, "arcgis_pixel"), (BING_ID, "bing_pixel")]):
            px, py = row[key]
            box = (round(px)-40, round(py)-40, round(px)+40, round(py)+40)
            crop = images[image_id].crop(box).resize((240,240),Image.Resampling.NEAREST)
            cd = ImageDraw.Draw(crop)
            cd.line((106,120,134,120), fill="yellow", width=2)
            cd.line((120,106,120,134), fill="yellow", width=2)
            x = 18+j*485
            patches.paste(crop, (x,y+40))
            draw.text((x+250,y+70), f"[{px:.1f},", font=small, fill="black")
            draw.text((x+250,y+102), f" {py:.1f}]", font=small, fill="black")
        dx, dy = row["pixel_shift_bing_minus_arcgis"]
        draw.text((270,y+165), f"东移约 {dx:.0f} px", font=small, fill="black")
        draw.text((270,y+200), f"南移约 {dy:.0f} px", font=small, fill="black")
    draw.text((18,1030), "约320px东、337px南，按ArcGIS显示尺度约360m；不是同点清理/新增。", font=small, fill="black")
    draw.text((18,1065), "缺Bing实际范围/空间变换；坐标模式问题是待查原因，不伪造已确认GCJ-02。", font=small, fill="black")
    patches.save(OUT / "bing-landmark-patches.png")
    context = Image.new("RGB", (2088, 1190), "white")
    cd = ImageDraw.Draw(context)
    cd.text((16,8), "未校正Bing不能作为同点时序｜共享地标与请求点位置核验", font=font, fill="black")
    cd.text((16,45), "左ArcGIS，右Bing；黄色B1–B3为共同地标。白十字为原表点对应位置，橙十字为各图中心。", font=small, fill="black")
    for image_id, offset in [(ARC_ID,0),(BING_ID,1064)]:
        tile = images[image_id].copy(); td=ImageDraw.Draw(tile)
        for row in controls:
            p = row["arcgis_pixel" if image_id==ARC_ID else "bing_pixel"]
            x,y=p;td.ellipse((x-9,y-9,x+9,y+9),outline="yellow",width=3)
            td.text((x+11,y-18),row["id"],font=font,fill="yellow",stroke_width=2,stroke_fill="black")
        for point,color in [([512,512],"orange"),(target_arc if image_id==ARC_ID else target_bing,"white")]:
            x,y=point;td.line((x-15,y,x+15,y),fill="black",width=7);td.line((x,y-15,x,y+15),fill="black",width=7)
            td.line((x-13,y,x+13,y),fill=color,width=3);td.line((x,y-13,x,y+13),fill=color,width=3)
        context.paste(tile,(offset,90))
    cd.text((16,1125), f"原表点：ArcGIS约[512,512] → Bing约[{target_bing[0]:.0f},{target_bing[1]:.0f}]；Bing图中心不对应原表点。", font=font, fill="black")
    cd.text((16,1160), "传统局部影像匹配+贴片目视复核；无新增取图、无识别模型调用。未校正Bing排除同点变化证据。", font=small, fill="black")
    context.save(OUT / "bing-spatial-comparison.png")
    print(json.dumps({"audit": str(OUT/"bing-spatial-audit.json"), "target": result["requested_point"], "unique_inliers": len(unique)},ensure_ascii=True))


if __name__ == "__main__":
    main()
