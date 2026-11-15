"""Render existing native TCI/SCL windows for a manual early-period review.

This script performs no API calls and modifies only its own spatial review
artifacts. TCI pixels are reprojected with nearest for display, without stretch.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject, transform_geom


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "outputs/point-timeseries-202511-now"
OUT = BASE / "spatial"
DATES = {"2025-11-01", "2025-12-01", "2025-12-16", "2026-01-10",
         "2026-01-12", "2026-02-19", "2026-03-11"}
SCL_NAMES = {0: "无数据", 1: "坏像元", 2: "暗像元", 3: "云影", 4: "植被",
             5: "非植被", 6: "水", 7: "未分类", 8: "中概率云", 9: "高概率云",
             10: "卷云", 11: "雪冰"}

# Independent assistant visual inspection of the rendered original TCI pixels,
# not a claim that the material identity can be determined at native 10m.
OBSERVATIONS = {
    "2025-12-01": {"visibility": "limited_by_gray_haze_low_contrast",
                   "large_site": "同位置有低对比度浅色痕迹；灰雾使边界不清，只作弱支持。",
                   "small_patch": "与周围差别较弱，不能判断物料存在与否。",
                   "note": "SCL未标云不代表无薄雾或大气影响。"},
    "2025-12-16": {"visibility": "surface_pattern_visible_at_10m",
                   "large_site": "同位置已有较清楚的纵长浅色地块，近似轮廓与后期观察窗一致。",
                   "small_patch": "小窗位置已有浅褐/灰色非植被地表；10m纹理不足以确认为堆料或同类弃料。"},
    "2026-01-10": {"visibility": "surface_pattern_visible_at_10m",
                   "large_site": "较大浅色场地持续可见，未表现为三月才整体出现。",
                   "small_patch": "北部较浅、南部灰褐的粗尺度斑块已存在；不作材质判定。"},
    "2026-01-12": {"visibility": "surface_pattern_visible_at_10m",
                   "large_site": "与1月10日场地位置和大体格局相近。",
                   "small_patch": "与1月10日粗尺度格局相近，不能据此证明无小范围堆存活动。"},
    "2026-02-19": {"visibility": "surface_pattern_visible_at_10m",
                   "large_site": "场地中北部较一月明显更亮，变化已出现在三月之前。",
                   "small_patch": "小窗北侧部分更亮，南侧仍有灰褐混合像元；未见可可靠勾画的新边界。",
                   "note": "TCI亮度变化不能排除大气、湿度、光照和显示处理差异。"},
    "2026-03-11": {"visibility": "surface_pattern_visible_at_10m",
                   "large_site": "较强浅色片延续二月状态；局部显得更白，不是此前完全不存在的整块新场地。",
                   "small_patch": "小窗内较多亮像元，但与二月相比没有能排除混合像元/地表状态影响的独有新增轮廓。",
                   "note": "不能仅凭这组TCI将首次堆放锁定在三月，亦不能据此否定用户三月现场核查。"},
}

CONCLUSIONS = {
    "large_site": "至少2025-12-16已可辨认同位置较大浅色地块，1月持续；12月1日仅弱支持。不是2026年3月才整体出现。",
    "small_patch": "2025末/2026年1月同一小窗已有非植被浅褐/灰色片；原生10m不能确认当时已存在与现场核查相同的拆除弃料，也不能界定首次堆放日。",
    "march_change": "二月19日已较一月变亮，三月11日延续；未发现可排除大气、季节/湿度、光照、显示处理和混合像元解释的三月独有突变。",
    "not_excluded": "不能排除较小范围的增量堆放、清理或不同物料替换；用户三月现场核查与场地此前存在并不矛盾。",
    "not_a_first_occurrence_date": True,
    "waste_identity_inferred_from_tci": False,
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rois = read_json(OUT / "candidate-and-controls.geojson")
    with rasterio.open(OUT / "arcgis-reference-200m.tif") as ds:
        grid = {"crs": str(ds.crs), "transform": ds.transform, "width": ds.width, "height": ds.height}
        reference = ds.read()[:3].transpose(1, 2, 0)
    scenes, pending, excluded = [], [], []
    for path in sorted((BASE / "s2-native").glob("*/metadata.json")):
        try:
            meta = read_json(path)
        except (OSError, ValueError):
            continue
        day = meta.get("acquired_at", "")[:10]
        if day not in DATES:
            continue
        visual = meta.get("assets", {}).get("visual", {})
        file = path.parent / "visual.tif"
        if visual.get("status") != "completed" or not file.is_file():
            pending.append({"scene_id": meta.get("scene_id"), "date": day, "status": meta.get("status")})
            continue
        assert hashlib.sha256(file.read_bytes()).hexdigest() == visual["sha256"]
        with rasterio.open(file) as ds:
            assert ds.count == 3 and ds.dtypes[0] == "uint8" and ds.res == (10, 10)
            dst = np.zeros((3, grid["height"], grid["width"]), dtype=np.uint8)
            for band in range(3):
                reproject(ds.read(band+1), dst[band], src_transform=ds.transform,
                          src_crs=ds.crs, dst_transform=grid["transform"],
                          dst_crs=grid["crs"], resampling=Resampling.nearest)
            display = dst.transpose(1, 2, 0)
        row = {"scene_id": meta["scene_id"], "date": day,
               "acquired_at": meta["acquired_at"], "visual_file": str(file),
               "visual_sha256": visual["sha256"], "metadata_file": str(path),
               "native_visual_gsd_m": 10, "scene_cloud_cover_percent": meta.get("scene_cloud_cover"),
               "whole_scene_cloud_cover_not_used_as_target_gate": True,
               "scl_status": "pending", "roi_scl": {},
               "assistant_visual_review": OBSERVATIONS.get(day),
               "image": display}
        scl_asset = meta.get("assets", {}).get("SCL", {})
        scl_file = path.parent / "SCL.tif"
        if scl_asset.get("status") == "completed" and scl_file.is_file():
            assert hashlib.sha256(scl_file.read_bytes()).hexdigest() == scl_asset["sha256"]
            with rasterio.open(scl_file) as ds:
                values = ds.read(1)
                for roi in rois["features"]:
                    if roi["geometry"]["type"] != "Polygon":
                        continue
                    geometry = transform_geom("EPSG:4326", ds.crs, roi["geometry"])
                    mask = geometry_mask([geometry], out_shape=values.shape,
                                         transform=ds.transform, invert=True, all_touched=True)
                    classes, counts = np.unique(values[mask], return_counts=True)
                    count = int(mask.sum())
                    stats = {"scl_native_resolution_m": list(ds.res), "all_touched": True,
                             "pixel_count": count,
                             "class_counts": {str(c): int(n) for c, n in zip(classes, counts)},
                             "cloud_shadow_snow_or_invalid_fraction": float(np.isin(values[mask], [0, 1, 2, 3, 8, 9, 10, 11]).sum()/count) if count else None,
                             "unclassified_fraction": float((values[mask] == 7).sum()/count) if count else None}
                    row["roi_scl"][roi["id"]] = stats
            row.update(scl_status="completed", scl_file=str(scl_file), scl_sha256=scl_asset["sha256"])
        if day == "2025-11-01":
            excluded.append({k: v for k, v in row.items() if k != "image"})
            continue
        scenes.append(row)
    scenes.sort(key=lambda x: x["date"])
    panels = [{"date": "ArcGIS位置参照", "image": reference,
               "label": "5点来源日期元数据2025-01-09；非2026"}] + scenes
    panel_w, panel_h, cols = 460, 530, 3
    rows = math.ceil(len(panels) / cols)
    canvas = Image.new("RGB", (cols*panel_w, 125+rows*panel_h+145), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 23)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 21)
    draw.text((16, 8), "早期时序独立目视核查｜相同中心、相同200m窗口", font=title_font, fill="black")
    draw.text((16, 48), "红：较大浅色场地；紫：较小斑杂片；蓝/白：屋顶；绿：耕地；均为人工观察窗", font=font, fill="black")
    draw.text((16, 82), "原生TCI 10m，最近邻显示，无另加拉伸；SCL原生20m。原表红线未配准，未强行套合。", font=small, fill="black")
    inv = ~grid["transform"]
    colors = {f["id"]: f["properties"].get("color", "white") for f in rois["features"]}
    for i, row in enumerate(panels):
        x, y = (i % cols)*panel_w, 125+(i//cols)*panel_h
        draw.text((x+16, y), row["date"], font=title_font, fill="black")
        tile = Image.fromarray(row["image"]).resize((420, 420), Image.Resampling.NEAREST)
        td = ImageDraw.Draw(tile)
        for feature in rois["features"]:
            if feature["geometry"]["type"] != "Polygon":
                continue
            geometry = transform_geom("EPSG:4326", grid["crs"], feature["geometry"])
            points = [inv*tuple(p) for p in geometry["coordinates"][0]]
            points = [(p[0]*2.1, p[1]*2.1) for p in points]
            td.line(points, fill=colors[feature["id"]], width=2)
        td.line((199,210,221,210), fill="black", width=4)
        td.line((210,199,210,221), fill="black", width=4)
        td.line((200,210,220,210), fill="white", width=1)
        td.line((210,200,210,220), fill="white", width=1)
        canvas.paste(tile, (x+16,y+44))
        label = row.get("label", "SCL尚未完整，暂不作清晰度定论")
        if row.get("scl_status") == "completed":
            c = row["roi_scl"].get("candidate_ground", {})
            fraction = c.get("cloud_shadow_snow_or_invalid_fraction")
            label = f"场地SCL云影/无效 {fraction:.0%}（20m筛查）" if fraction is not None else "SCL范围内无采样像元"
        if row["date"] == "2025-12-01":
            label = "目视有灰雾/低对比；SCL未标云仍有限制"
        draw.text((x+16,y+474), label, font=small, fill="black")
    if len(panels) == 7:
        x, y = panel_w+16, 125+2*panel_h+10
        summary_lines = [
            "独立目视结论（同窗比较）",
            "大场地：至少2025-12-16已可辨认，1月持续，",
            "不是2026年3月才整体出现。12/1受灰雾影响。",
            "小紫框：此前已有非植被浅色/灰褐地表；10m",
            "无法确认当时是否为相同弃料或具体堆放日。",
            "2/19已经变亮；3/11延续。未发现能排除大气、",
            "湿度、光照等影响的三月独有新增轮廓。",
            "不能排除局部增量、清理或物料替换；场地先前",
            "存在，与用户三月现场核查不矛盾。",
            "仅TCI目视，不据此计算反射率变化或违法结论。",
        ]
        for i, line in enumerate(summary_lines):
            draw.text((x,y+i*40), line, font=font if i else title_font, fill="black")
    y = 125+rows*panel_h
    draw.text((16,y), "11月1日：目标SCL为云，不作地表是否存在的证据。TCI色调差异不等于物料/面积变化。", font=font, fill="black")
    draw.text((16,y+36), "每个10m像元仍可能混合地表、屋顶、植被和阴影；200m图放大不会增加纹理，不能认定具体废物类型。", font=small, fill="black")
    draw.text((16,y+70), "较小紫框仅为高分底图手绘观察窗，不是原表857.22范围；不可据此确定首次堆存日期。", font=small, fill="black")
    canvas.save(OUT / "early-sequence-review.png")
    for row in scenes:
        row.pop("image")
    result = {"created_at": datetime.now(timezone.utc).isoformat(),
              "method": "Assistant visual review, same 200m UTM geometry; existing native TCI reprojected nearest without additional stretch. No recognition API calls.",
              "grid": {**grid, "transform": list(grid["transform"]), "display_pixel_spacing_m": 1},
              "roi_file": str(OUT/"candidate-and-controls.geojson"),
              "scenes": scenes, "pending": pending, "excluded": excluded,
              "manual_conclusions_status": "completed_for_six_dates",
              "assistant_visual_conclusions": CONCLUSIONS,
              "limits": ["TCI display is not calibrated reflectance change", "SCL 20m is a screening classification, not proof of surface visibility", "Hand drawn ROIs are not workbook red-line registration or authoritative waste boundaries", "Native TCI 10m; 1m common grid is display resampling only"]}
    (OUT / "early-sequence-review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dates": [s["date"] for s in scenes], "pending": pending, "out": str(OUT)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
