"""Read the supplied Chengdu workbook and build provenance-aware evaluation cases.

No network, model invocation, workbook mutation or inferred ground truth is used.
Run with the project Python from the repository root; output is deterministic.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wuyu_harness.workbook import parse_workbook

SOURCE = Path("D:/博士论文/成都数据/生态委/1-6原表/第五批-成都市全域疑似固废清单（新增&面积增加）.xlsx")
OUT = Path("outputs/dataset-evaluation-20261018")
AS_OF = "2026-10-18"
KINDS = [
    ("suspected_site_discovery", "疑似点位发现", "倾倒核查"),
    ("waste_type_screening", "固废类型识别", "倾倒核查"),
    ("stockpile_extent", "堆存范围判定", "持续堆存"),
    ("stockpile_change", "堆存变化监测", "持续堆存"),
    ("cleanup_progress", "清理进度核查", "清理整改"),
    ("residual_stockpile", "残留堆体识别", "清理整改"),
    ("repeated_dumping", "重复倾倒排查", "后续复查"),
    ("site_tracking", "点位持续跟踪", "后续复查"),
    ("environmental_context", "环境判读", "贯穿全程"),
    ("surrounding_relationship", "周边关系判读", "贯穿全程"),
]


def field(record, column):
    return next((c["value"] for c in record["cells"] if c["column"] == column), None)


def location(record):
    c = record["coordinates"]
    return f"{c['longitude']:.6f}, {c['latitude']:.6f}"


def source_cells(record, columns="BCDEFGHIJKLMNOPQRSTU"):
    row, sheet = record["source"]["row"], record["source"]["sheet"]
    return [{"sheet": sheet, "cell": f"{col}{row}", "column_label": next((c["header"] for c in record["cells"] if c["column"] == col), next((im["column_label"] for im in record["embedded_images"] if im["column"] == col), None))} for col in columns]


COMMON_FORBIDDEN = [
    "把表内疑似类别、变化类型或备注当作独立盲测真值或新的影像观测",
    "把目录命中、像元读取失败、云遮挡、未检索混写成该时段无卫星数据",
    "未校核坐标、图像日期与配准就给精确跨期变化或地理面积",
    "用RGB颜色、NDVI或单次SAR亮暗确认具体成分、危险废物、污染超标或违法事实",
    "把表内未注明单位的面积和变化面积直接标成平方米或体积",
]

CONTRACTS = {
    "suspected_site_discovery": {
        "required_data": ["覆盖目标点的高分光学近景与周边场景", "坐标系及各图源偏置/覆盖核验", "必要时另一独立图源和目标局部放大"],
        "required_actions": ["先定位真实目标而非只看整景", "用wuyu-jianwei-rs读取实际高分像素", "疑点不完整或来源冲突时定向补近景/周边", "按问题给出有无可复核堆存线索及图号"],
        "required_response_fields": ["实际图源、日期是否已知、尺度", "具体位置与可见证据", "主要竞争解释", "是否还需补证及原因"],
        "additional_forbidden": ["以点位附近存在裸地就确认目标堆存", "未读取影像却描述目标纹理"],
    },
    "waste_type_screening": {
        "required_data": ["足够分辨率的目标裁剪、同源场景", "用户现场资料并与独立影像结论分开", "仅在能减少具体疑点时补光谱/SAR"],
        "required_actions": ["先描述可见堆体特征再给候选类别", "比较施工土方、原料堆场与固废混堆解释", "标清用户资料、专业模型、科学波段各自贡献"],
        "required_response_fields": ["候选类别及支持/反证", "可见特征", "需现场确认的成分"],
        "additional_forbidden": ["将现场备注当成模型独立识别命中", "把Sentinel-2当作高光谱或用10m像元鉴别小型塑料/砖块"],
    },
    "stockpile_extent": {
        "required_data": ["地理参考高分影像或可靠GSD", "目标边界及定位控制", "表内面积原值和未注明单位的说明"],
        "required_actions": ["区分堆体与硬化地面/阴影/道路", "合格地理参考下才测面并报告边界误差", "缺元数据时给可复核像素框而非伪精确平方米"],
        "required_response_fields": ["边界依据与图号", "面积是否可测及单位", "遗漏/遮挡范围"],
        "additional_forbidden": ["把表内变化面积当作本次新测值", "单幅RGB面积推体积或吨数"],
    },
    "stockpile_change": {
        "required_data": ["基准期、中间期、后期的有日期同点位影像", "真实科学波段及局部质量掩膜（需要时）", "配准、季节、农事和尺度一致性"],
        "required_actions": ["按用户区间先查真实目录再选景", "补清晰基线而非反复调当前底图", "把裸地/植被变化与堆存变化区分", "按日期组织趋势与未覆盖时段"],
        "required_response_fields": ["实际成像日期序列", "各期观测与变化方向", "出现时段区间而非虚构具体倾倒日", "局部云雾与混合像元"],
        "additional_forbidden": ["用整景云量代替目标局部质量", "只看前后两张不明日期截图就确认持续扩大"],
    },
    "cleanup_progress": {
        "required_data": ["清理前、有日期清理中/后影像", "用户整改时间或验收材料（若有）", "原堆体范围与邻近可能搬移区"],
        "required_actions": ["优先比较基准堆体与整改后相同位置", "区分清运、遮盖、平整和移到旁边", "没有整改时间或合格边界时明确不能算完成率"],
        "required_response_fields": ["可见清理变化", "是否仍有残留/转移", "清理比例是否可算", "需要补的时间或验收证据"],
        "additional_forbidden": ["表格只有新增/增加也宣称已整改", "从表面变平或变绿直接认定全部清运"],
    },
    "residual_stockpile": {
        "required_data": ["整改前后目标近景及场景", "边角、树下、围挡与出入口局部图", "可靠分辨率、时间和遮挡记录"],
        "required_actions": ["复查原边界和周边小堆", "目标像素不足时补高分近景", "把裸地、残留候选与遮盖未知区域分开"],
        "required_response_fields": ["残留候选位置", "支持图块和像素框", "未能看清的区域"],
        "additional_forbidden": ["因为主堆消失就认定无残留", "用10m影像确认很小边角堆已全部清除"],
    },
    "repeated_dumping": {
        "required_data": ["原有堆存、已清理、再次出现三个可比较阶段", "同点位定位核验", "表内复发备注及其来源"],
        "required_actions": ["先寻找已清理的中间基线", "区分未清完、暂时遮盖和清后复发", "缺第三阶段时说明只能验证哪段"],
        "required_response_fields": ["三阶段时间证据", "复发假设与竞争解释", "资料先验与独立影像支持程度"],
        "additional_forbidden": ["仅两幅前后图或一句备注就独立确认再次倾倒", "未经授权建立定时跟踪或执法任务"],
    },
    "site_tracking": {
        "required_data": ["用户指定起止日期与截至日期", "有日期档案目录和代表时相", "每期覆盖/读取/质量状态"],
        "required_actions": ["覆盖早、中、近期而不是只取最近一次", "保留稳定目标标识与时间轴", "缺月份明确原因与可补来源", "本次回溯与未来自动监测分开"],
        "required_response_fields": ["时间轴", "持续、扩大、清理或复发的证据状态", "目录完整性与像元分析抽样范围"],
        "additional_forbidden": ["将检索到全目录称为分析完所有历史影像", "未授权安排未来定时任务"],
    },
    "environmental_context": {
        "required_data": ["目标及周边水体、耕地、居民点的影像或GIS", "必要时地形/排水资料", "距离量测需要可靠地理参考"],
        "required_actions": ["围绕源-路径-受体解释待核查风险", "查看点位周边而非仅堆体特写", "把表内环境描述与当前地物核验分开"],
        "required_response_fields": ["敏感目标", "可能迁移路径", "可核实的空间依据", "现场优先核实项"],
        "additional_forbidden": ["邻近水体直接写已污染", "依据未配准截图给精确距离或法定风险等级"],
    },
    "surrounding_relationship": {
        "required_data": ["目标、道路、厂界、水体及地块边界同范围场景", "用户地籍/河道/耕地边界（若需法律边界）", "可靠空间参考"],
        "required_actions": ["先确定目标与同名地物再查周边关系", "需要时扩大场景且保留目标位置", "关系线索与运输/归属/违法因果区分"],
        "required_response_fields": ["与道路/厂区/农田/水体的相对位置", "尺度与依据", "需要补的边界资料"],
        "additional_forbidden": ["仅凭路相连认定污染来源或责任单位", "影像可见耕地就判定永久基本农田违法占用"],
    },
}


def question(record, kind, objective, question_id, tags, **extra):
    contract = CONTRACTS[kind]
    c = record["coordinates"]
    return {
        "question_id": question_id, "case_id": question_id, "title": objective[:65],
        "objective": objective, "prompt": objective, "task_types": [kind],
        "capability_tags": tags, "source_record_ids": [record["record_id"]],
        "source_site_ids": [record["site_id"]], "source_cells": source_cells(record),
        "center": [c["longitude"], c["latitude"]], "coordinates": c,
        "input_context": {"record": record["site_id"], "source_is_user_prior": True,
                          "label_exposed_in_prompt": str(field(record, "I")) in objective,
                          "ground_truth_status": "not_independently_adjudicated",
                          "coordinate_declaration": "unknown; H列重复经度表头按数值范围推为纬度"},
        "expected": {"task_types": [kind], **contract,
                     "forbidden_claims": COMMON_FORBIDDEN + contract["additional_forbidden"]},
        **extra,
    }


def make_live(by_id):
    live = []
    def add(sid, kind, ident, objective, tags, expected, attachments=None, followups=None):
        q = question(by_id[sid], kind, objective, ident, tags)
        q["expected"].update(expected)
        q["attachments"] = attachments or []
        q["followups"] = followups or []
        q["live_recommended"] = True
        live.append(q)
    r = by_id["XD-GTFW-6"]
    add(r["site_id"], "site_tracking", "LIVE-01", f"{location(r)} 这个点，2026年1月和前后两个月的Sentinel-2都有影像吗？先查全目录，按月列真实日期、数量和云量；这次只回答数据有没有，不用调当前高分底图，也不用给固废风险结论。", ["catalog_availability", "adjacent_months", "intent_following"], {"research_mode": "availability", "sources": ["sentinel2"], "date_start": "2025-11-01", "date_end": "2026-03-31", "required_actions": ["真实检索5个月Sentinel-2目录", "返回实际日期与数量", "不获取高分底图，不调用识别/科学视觉模型", "命中数与逐月计数一致"]})
    r = by_id["PD-GTFW-26"]
    add(r["site_id"], "stockpile_change", "LIVE-02", f"只查 {location(r)} 从2026年1月1日到4月30日的卫星存档：Sentinel-2、Landsat、Sentinel-1 RTC，以及EnMAP各有哪些可覆盖点位的景？按来源分别列日期、目录有没有、像元是否可读、还缺什么访问条件，不要把目录命中写成已经分析。", ["catalog_availability", "multisource", "hyperspectral_access", "failure_semantics"], {"research_mode": "availability", "sources": ["sentinel2", "landsat", "sentinel1-rtc", "enmap"], "date_start": "2026-01-01", "date_end": "2026-04-30", "required_actions": ["逐源查真实目录", "目录命中与读取状态分开", "报告授权或适配器缺口", "不虚构高光谱材料分析"]})
    r = by_id["JN-GTFW-19"]
    add(r["site_id"], "stockpile_change", "LIVE-03", f"请看 {location(r)} 在2026年1月至4月的Sentinel-2多光谱变化。先取能看清目标的前、中、后期，局部有雾就换景；重点判断裸露/堆积面是持续、扩大还是收缩，NDVI只用来辅助排除农作物变化，不要把周边整块农田指数当堆体指数。", ["science_timeseries", "local_quality", "adaptive_replanning", "roi_specificity"], {"research_mode": "timeseries", "sources": ["sentinel2"], "date_start": "2026-01-01", "date_end": "2026-04-30", "required_actions": ["真实目录后选有日期原生波段窗口", "局部质量检查和必要换景", "调用Gemini分析实际科学窗口", "按实际像元结果报告指数，区分ROI和目标", "保留未分析月份和混合像元限制"]})
    r = by_id["DJY-GTFW-26"]
    add(r["site_id"], "stockpile_change", "LIVE-04", f"{location(r)} 表里备注是施工工地、无法进入，不能直接认定工业固废。请比较2026年1月至4月的Sentinel-2和同轨同极化Sentinel-1 RTC，看是否有持续地表扰动以及光学、雷达是否相互支持；光学受云影响时让SAR补时间线，别从雷达亮暗猜物料。", ["science_timeseries", "sar", "counter_hypothesis", "user_prior_separation"], {"research_mode": "timeseries", "sources": ["sentinel2", "sentinel1-rtc"], "date_start": "2026-01-01", "date_end": "2026-04-30", "required_actions": ["检索光学与SAR实际目录", "SAR比较统一轨道方向/相对轨道/极化与定标", "实际读取窗口并分别记录传感器观测", "资料中的施工解释参与比较而非强行确认固废"]})
    r = by_id["JT-GTFW-13"]
    add(r["site_id"], "suspected_site_discovery", "LIVE-05", f"帮我核查 {location(r)} 有没有疑似堆存、看起来更像哪类东西，并说说它和道路、居民区、耕地的关系。先看同点位多个高分图源，定位不准就校正、看不全再补周边；只有确实有助于分清疑点才加多光谱或SAR。直接围绕这个点回答。", ["hybrid_highres", "waste_candidates", "environment", "spatial_context"], {"task_types": ["suspected_site_discovery", "waste_type_screening", "environmental_context", "surrounding_relationship"], "research_mode": "auto", "required_actions": ["同点多源高分获取及空间检查", "wuyu-jianwei-rs实际读取高分图", "按缺口追加近景或场景", "具体候选解释与道路/居民区/耕地关系"]})
    r = by_id["DY-GTFW-10"]
    attachments = [{"kind": "workbook_embedded", **im, "source_record_id": r["record_id"]} for im in r["embedded_images"] if im["column"] in ("T", "U")]
    add(r["site_id"], "repeated_dumping", "LIVE-06", f"请比较我上传表中 {r['site_id']} 的变化前T{r['source']['row']}和变化后U{r['source']['row']}两张图。表内备注说第四期已清理、第五期又出现；这是用户现场资料先验，请独立看图：原堆体清理了多少、角落是否有残留、还是清后又堆？图头只给2026年1—2月与3—4月范围，没有精确拍摄日，不能凭这两张把复发时间说死。需要第三阶段证据时具体说明。", ["workbook_embedded_images", "cleanup", "residual", "recurrence", "user_prior_separation"], {"task_types": ["cleanup_progress", "residual_stockpile", "repeated_dumping"], "required_actions": ["解出并实际读取T3/U3两张表内原图", "以图头期间作为未核验范围标签", "区分原堆存/清理/复发三阶段", "缺精确时间、GSD和基准边界时不报清理率或独立确认复发"]}, attachments)
    r = by_id["JN-GTFW-7"]
    add(r["site_id"], "stockpile_extent", "LIVE-07", f"{location(r)} 这处请先核实堆体在哪里，再尽量圈出实际堆存范围，把道路、厂房和空地排除。清单面积原值3962.54、变化面积3045.28都没有写单位，别直接照抄成平方米；如果图像无可靠地理参考就给像素范围和缺什么数据。顺带判断它像废料还是正常原料堆场。", ["hybrid_highres", "extent", "unit_ambiguity", "counter_hypothesis"], {"task_types": ["stockpile_extent", "waste_type_screening"], "research_mode": "auto", "required_actions": ["实际高分定位与边界证据", "有合格GSD/地理参考才报面积", "单位不明保留原值", "比较废料与合法原料堆场解释"]})
    r = by_id["PD-GTFW-26"]
    add(r["site_id"], "surrounding_relationship", "LIVE-08", f"请回溯 {location(r)} 在2026年1月至4月的变化，并结合高分影像看看它和周边鱼塘、水体、工地是什么关系。清单备注提到鱼塘填埋，这只是待核验线索；先把目标与水面边界对上，再决定哪些多光谱日期有帮助，不能把影像关联直接写成污染或责任认定。", ["hybrid_history", "site_tracking", "waterbody_relationship", "followup_context"], {"task_types": ["site_tracking", "stockpile_change", "environmental_context", "surrounding_relationship"], "research_mode": "timeseries", "date_start": "2026-01-01", "date_end": "2026-04-30", "required_actions": ["高分空间场景与有日期档案分别获取", "真实时间轴与水体关系证据", "目录、窗口、支持结论严格区分"]}, followups=[{"case_id": "LIVE-08-F1", "objective": "那这次先别再调高分底图。保持刚才同一点、2026年1月至4月，改查Sentinel-1 RTC，挑两到三期同轨同极化的SAR窗口，看看鱼塘边缘的变化能否补上光学云遮挡的时间段。实际取得什么日期就回答什么，不要只给建议。", "parent_case_id": "LIVE-08", "expected": {"task_types": ["stockpile_change", "surrounding_relationship"], "research_mode": "timeseries", "sources": ["sentinel1-rtc"], "date_start": "2026-01-01", "date_end": "2026-04-30", "required_actions": ["继承点位与日期但切换SAR来源", "实际查SAR目录与窗口", "同轨同极化可比性检查", "不再追加当前高分底图"], "forbidden_claims": COMMON_FORBIDDEN}}])
    for case in live:
        assumption = "源表未声明坐标系。本次仅为检索测试暂按WGS84经纬度输入，尚待底图或现场定位核实。"
        case["objective"] += " " + assumption
        case["prompt"] = case["objective"]
        case["coordinate_system"] = "wgs84"
        case["provenance"] = {"assumptions": [assumption], "source_coordinate_system": "unknown", "coordinate_verified": False, "test_input_coordinate_system": "wgs84"}
        case["task_types"] = case["expected"]["task_types"]
        for followup in case["followups"]:
            followup["provenance"] = case["provenance"]
    return live


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    content = args.source.read_bytes()
    parsed = parse_workbook(content, args.source.name)
    records, profile = parsed["records"], parsed["profile"]
    by_id = {r["site_id"]: r for r in records}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        (args.output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    save("records.json", {"source_path": str(args.source.resolve()), "source_sha256": profile["sha256"], "records": records})
    live = make_live(by_id)
    envelope = {"schema_version": "wuyu_dataset_evaluation_v1", "as_of": AS_OF, "source_workbook": str(args.source.resolve()), "source_sha256": profile["sha256"], "assessment_scope": "工具执行、任务跟随和证据一致性的带先验场景测试，不是已裁决检测准确率"}
    save("live-cases.json", {**envelope, "cases": live})
    profile.update({"source_path": str(args.source.resolve()), "as_of": AS_OF,
                    "live_catalog_coverage": {"output_directory": "catalog-coverage", "execution_owner": "root", "meaning": "36点按源表前后期实时检索12科学源的目录足迹，仅目录层，不等于云下目标可见或像元已分析", "status": "see_runtime_outputs"},
                    "administrative_area_counts": dict(Counter(field(r,"E") for r in records)),
                    "change_type_counts": dict(Counter(r["change_type"] for r in records)),
                    "user_label_counts": dict(Counter(field(r,"I") for r in records)),
                    "environment_counts": dict(Counter(field(r,"P") for r in records)),
                    "period_counts": dict(Counter(r["period_label"] for r in records)),
                    "unreachable_or_inaccessible_records": [{"record_id":r["record_id"],"site_id":r["site_id"],"cell":f"Q{r['source']['row']}","note":field(r,"Q")} for r in records if any(k in str(field(r,"Q")) for k in ("无法进入","无法到达"))],
                    "recurrence_prior_records": [{"record_id":r["record_id"],"site_id":r["site_id"],"cell":f"Q{r['source']['row']}","note":field(r,"Q")} for r in records if "又出现" in str(field(r,"Q"))],
                    "area_summary": {"unit":"unknown","min_raw":min(r["area"]["value"] for r in records),"max_raw":max(r["area"]["value"] for r in records),"sum_raw":round(sum(r["area"]["value"] for r in records),2),"sum_is_area_m2":False},
                    "measurement_cautions": ["G/H列同名中心经度，H值约30-31按数值范围推为纬度，尚非用户确认", "坐标系未声明", "L/M/N/O无面积/体积/长度单位", "M/N所有0是表内数值，不能证明零体积或零长度", "T/U列为2026年1-2月及3-4月期间标签，不能当逐图拍摄日", "Q备注含现场观察描述，但核查人、时间、方法、原始记录链未给齐", "表格同时有新增和面积增加，缺少完整已清理/无变化/负例总体，不能算召回、精确率或完成率"]})
    save("profile.json", profile)
    print(json.dumps({"phase":"cases_ready","records":len(records),"images":profile["embedded_image_count"],"live_cases":len(live),"path":str(args.output_dir.resolve())},ensure_ascii=False), flush=True)
    # Image integrity is checked from original archive bytes; no image is modified.
    from PIL import Image
    images=[]
    with ZipFile(io.BytesIO(content)) as z:
        for r in records:
            for im in r["embedded_images"]:
                raw=z.read(im["archive_member"])
                item={**im,"record_id":r["record_id"],"site_id":r["site_id"],"sha256":hashlib.sha256(raw).hexdigest()}
                try:
                    with Image.open(io.BytesIO(raw)) as pic:
                        item.update({"width":pic.width,"height":pic.height,"format":pic.format})
                        pic.verify()
                    item["status"]="verified_original_image"
                except Exception as exc:
                    item.update({"status":"invalid_image","error_type":type(exc).__name__})
                images.append(item)
    profile["image_integrity"]={"checked":len(images),"valid":sum(im["status"]=="verified_original_image" for im in images),"invalid":sum(im["status"]!="verified_original_image" for im in images),"by_column":dict(Counter(im["column"] for im in images)),"unique_image_hashes":len({im["sha256"] for im in images})}
    save("image-manifest.json", {**envelope,"images":images})
    save("profile.json", profile)
    # The question bank is filled below by task-specific language, not label copying.
    questions = build_questions(records, live)
    save("question-bank.json", {**envelope, "task_contracts":[{"task_type":kind,"task_label":label,"regulatory_stage":stage,**CONTRACTS[kind]} for kind,label,stage in KINDS], "question_count":len(questions),"task_type_counts":dict(Counter(k for q in questions for k in q["task_types"])),"capability_counts":dict(Counter(t for q in questions for t in q["capability_tags"])),"questions":questions})
    print(json.dumps({"phase":"complete","questions":len(questions),"valid_images":profile["image_integrity"]["valid"],"covered_records":len({i for q in questions for i in q["source_record_ids"]}),"task_types":len({k for q in questions for k in q["task_types"]})},ensure_ascii=False), flush=True)


def build_questions(records, live):
    templates = {
        "suspected_site_discovery": [
            "{loc} 这里现在有没有值得核查的堆存？先把点位对准，给我能看清目标的图。",
            "帮我看看清单{sid}是不是只是裸地。若发现堆积线索，再补一个能真正对照的图源。",
            "请核查{loc}附近，目标在画面边缘时自行补周边，别只分析中心点之外的地块。",
            "{sid}这个疑点有几处独立堆体？图像不够细就放大目标，别把厂房和道路一起算。",
            "请在{loc}核验疑点是否仍可见，直接列关键图和所见事实，不需要每一步都套报告模板。",
            "这个点{loc}，一个底图看不清时换来源；能看清就停止，说明为什么足够。",
            "表里{sid}标为{change}，你先别沿用这个结论，独立核对影像中有没有对应的异常堆积面。",
            "我只想知道{loc}有没有堆存线索以及在哪里，不用现在查法规或写整份监管报告。",
        ],
        "waste_type_screening": [
            "{loc} 的东西更像拆除料、堆土还是正常施工物料？用可见证据比较，不要只报一个类别。",
            "{sid}的用户备注是：‘{note}’。请把现场资料与影像能独立看出的特征分开说。",
            "请核查{loc}的堆体颜色、块状纹理、袋状物和运输痕迹，哪些看得到，哪些只是猜测？",
            "表中{sid}分类是{label}，你能从高分图支持到哪个层次？不支持的部分请明确。",
            "{loc}看着像灰白碎块，有没有可能是商品砂石或施工原料？给最有区分力的近景。",
            "{sid}可能混合多类物料，不要强制单类；说明哪些候选有支持、哪些需现场看。",
            "假如{loc}高分还分不清，Sentinel-2能补什么、不能补什么？值得取景才实际调用。",
            "请看{loc}，若树枝、农膜、裸土混杂，别用NDVI下降直接推为建筑垃圾。",
        ],
        "stockpile_extent": [
            "帮{sid}圈实际堆体边界，排除道路和厂房；能量面积再量，不能量就给像素框。",
            "{loc}这处堆存是否超出原场地？需要周边边界时扩大视野并保留同一点标记。",
            "表里{sid}面积{area}、增加{delta}都未标单位。先核实可比性，不要直接把数字当平方米。",
            "{loc}有几块互不相连的堆区？分别圈出，遮挡部分保持未知。",
            "请检查{sid}，阴影、裸地、硬化场地和真正堆体各占哪里，别把整片都圈进去。",
            "{loc}的堆存范围请给可复核图，不需要凭面积推体积，更别估多少吨。",
            "请比较{sid}两期表图的边界变化，元数据不足时只谈可见范围，别做伪精确面积差。",
            "{loc}如果目标只占Sentinel-2几个像元，换高分图画边界，解释尺度限制。",
        ],
        "stockpile_change": [
            "查{loc}从2026年1月至4月怎么变，至少找前中后期，不够清楚就换景而不是猜。",
            "{sid}表称{change}，请把2026年1—2月与3—4月的真实日期影像对上，看独立证据是否支持。",
            "{loc}大概2026年3月开始堆的，往前追到2025年11月，给最早可见与此前未见的时间区间。",
            "{sid}附近农田季节变化很明显，请用局部多光谱辅助区分农事、裸地和堆积，不用整景NDVI代替目标。",
            "{loc} 1月有云的话扩到2025年12月和2026年2月，实际找替代景并说明时间差。",
            "{sid}是否持续增加？把支持扩大、稳定或清理的各期图并列，没有合格日期别硬给趋势。",
            "请对{loc}比较2026年1月至4月同轨同极化SAR变化，降雨和耕作可能干扰也要考虑。",
            "{loc}仅TCI外观变化够不够？若需要NIR/红波段就取原生波段，列实际尺度和局部云雾。",
        ],
        "cleanup_progress": [
            "{sid}如果已经开展清理，请用清理前后图判断还有多少可见堆体；资料没给整改时间就别编。",
            "{loc}请核查是否真的清运，不是把物料摊平、覆土或盖网，列支持不同解释的图。",
            "比较{sid}表里的前后图，清理了哪些区、还留哪些区？没有可靠面积就别报完成百分比。",
            "{loc}若主堆减少，请同时查附近是否出现转移堆，不要把搬到边上当全部完成。",
            "{sid}只有第五期新增/增加记录，这份表能否评估整改进度？说明缺哪一阶段并继续查可用历史。",
            "{loc}最新看起来变绿了，能说明清干净吗？对比更早日期排除季节植被和遮盖。",
            "请核查{sid}是否只移除了大堆、边角仍残留，先给关键近景，不用泛泛整改建议。",
            "{loc}请把表内变化面积和你新观察到的清理变化分开，用户原值不能当本次测量。",
        ],
        "residual_stockpile": [
            "{sid}主堆若已消失，边角、树下和道路旁还有小堆吗？看不清的地方别说没有。",
            "请放大{loc}原堆边界和出入口，找残留候选并标位置，不要把裸土地面全当垃圾。",
            "{sid}整改后是否有覆网或覆土遮住的区域？区分明确残留和被遮挡未知。",
            "{loc}请检查清理后的散落碎块，小于分辨率的对象保持不确定，必要时请求高分。",
            "比较{sid}表图的角落，看残留是在原位还是移到了旁边，保留全景位置关系。",
            "{loc}只有10m多光谱时能否确认没有残留？不足就找高分图，不用指数证明清零。",
            "请给{sid}残留疑点的局部图与主要反例，农膜、石块和正常物料堆不要混为一谈。",
            "{loc}不要求整体报告，只列仍可能存在残留的区域、图号和看不清的原因。",
        ],
        "repeated_dumping": [
            "{sid}是否存在清后又堆？找原堆存、已清理、再次出现三阶段，只有两期就别强行认定复发。",
            "请追查{loc}从2026年1月至今，判断是一直没清完还是清过又出现，给实际时间证据。",
            "{sid}用户说清理过，请先核实中间的清空基线，再谈重复倾倒，不要只比较最新两张。",
            "{loc}出现新堆是在原位置还是旁边迁移？先同点配准再比较，不明日期底图只作上下文。",
            "{sid}的两张变化图能说明复发吗？请明确目前支持到哪一步，缺第三阶段就定向补。",
            "{loc}如果遮盖层后来去掉，看起来又出现不代表新倾倒，帮我比较这一替代解释。",
            "核查{sid}反复扰动的时间线，分辨正常施工堆料周转与疑似清后回堆。",
            "{loc}若多光谱只见农田周期变化，请别据此判复发，回到实际目标高分证据。",
        ],
        "site_tracking": [
            "{loc}从2025年11月到2026年10月18日逐月查目录，再选关键时相画变化时间线。",
            "{sid}请覆盖2026年1月至4月的早中晚阶段，不要全用同一个月或只展示最先下载的几张。",
            "{loc}哪些月份有数据、哪些云遮挡、哪些还没读取？把三种状态分开回答。",
            "{sid}这次做历史回溯即可，不要自动设未来监控；先给实际取得的日期和变化证据。",
            "{loc}希望持续跟踪堆存，有高分就看形态，缺时相再考虑S2/SAR，按已有证据决定补什么。",
            "请给{sid}完整检索区间与实际分析抽样区间，列出没看像元的月份，别把查目录当逐景分析。",
            "{loc}只查2026年1月附近两个月的Sentinel-2目录即可，不用生成固废风险结论。",
            "{sid}假如一个来源无覆盖，逐项说明没景、无权限还是读取失败，再选择真正能补缺口的来源。",
        ],
        "environmental_context": [
            "{loc}周围有哪些水体、耕地或居民点？只讲能核对的关系和可能路径，不把邻近写成已污染。",
            "{sid}表里写周边是{env}，请用场景影像核对；必要时扩大范围，不仅看堆体特写。",
            "{loc}的雨水可能往哪走？没有地形和排水资料就说明不能可靠推流向，先给影像可见沟渠。",
            "帮{sid}判断现场先查什么，按扬尘、径流、直接接触各自证据说，不需要虚构风险概率。",
            "{loc}附近是否有鱼塘或河沟，若有给位置依据，可靠地理参考不足时别报精确距离。",
            "{sid}若紧邻居民区，影像能看到的遮盖、防护和裸露情况是什么，噪声和污染浓度不要臆测。",
            "{loc}请核查地表扰动对周边农田的关系，NDVI下降不能直接写重金属污染。",
            "{sid}工业区背景会不会让你过度判断固废？请同时检查正常生产堆料解释。",
        ],
        "surrounding_relationship": [
            "{loc}堆体、道路、厂界和耕地分别在哪里？给同一场景上的相对关系，不要猜责任单位。",
            "{sid}与附近运输便道是否相连？道路可达性是线索，不足以认定是谁倾倒。",
            "请看{loc}目标是否侵入可见水面或沟渠，法律河道边界需资料再核，不要混为一谈。",
            "{sid}影像里的地块像耕地，是否属于永久基本农田需要什么边界资料？先回答影像关系。",
            "{loc}若目标偏到图边，先确认同名地物和坐标偏置，别扩大场景后把另一个堆体当原目标。",
            "{sid}邻近厂房还是居民区？把表内{env}描述与新图核验分开。",
            "{loc}请看是否有从堆区连到水体的排水沟，没看到就保持未知，不按地势印象编路径。",
            "{sid}需要周边约500米环境对照，先保留目标中心；如无可靠比例尺不要报精确距离。",
        ],
    }
    questions=[]
    for ti,(kind,label,stage) in enumerate(KINDS):
        for vi,template in enumerate(templates[kind]):
            r=records[(ti*5+vi*3)%len(records)]
            objective=template.format(loc=location(r),sid=r["site_id"],note=field(r,"Q"),label=field(r,"I"),change=r["change_type"],area=r["area"]["raw"],delta=r["change_area"]["raw"],env=field(r,"P"))
            q=question(r,kind,objective,f"Q-{ti+1:02d}-{vi+1:02d}",[kind,"natural_language", "user_prior_separation" if "表" in objective or "备注" in objective else "unlabelled_prompt"])
            q["regulatory_stage"]=stage
            q["task_label"]=label
            q["evaluation_mode"]="contextual_scenario_not_blind_accuracy" if q["input_context"]["label_exposed_in_prompt"] or "表" in objective else "unlabelled_question_on_prior_selected_sites_not_population_accuracy"
            questions.append(q)
    for c in live:
        questions.append(c)
        for f in c["followups"]:
            parent=dict(c)
            parent.update({"question_id":f["case_id"],"case_id":f["case_id"],"objective":f["objective"],"prompt":f["objective"],"parent_case_id":c["case_id"],"expected":f["expected"],"followups":[],"attachments":[],"capability_tags":["followup_sensor_switch","sar","context_inheritance"]})
            questions.append(parent)
    extras=[
        ("DATA-01","XD-GTFW-6","site_tracking","{loc}，1月附近几个月Sentinel都没有吗哈哈？这回真的查存档日期再说。",["colloquial_followup","catalog_availability"],{"parent_case_id":"LIVE-01","expected_research_mode":"availability"}),
        ("DATA-02","PD-GTFW-26","stockpile_change","{loc} 若EMIT或EnMAP只有目录、原始波段需登录，请如实报告访问缺口，不要拿RGB说做了高光谱分析。",["hyperspectral","access_control"],{}),
        ("DATA-03","PZ-GTFW-26","stockpile_extent","{loc}表中面积原值91.55没有单位。请判断S2的10m像元是否足够圈这个目标，别把原值直接当平方米。",["small_target","unit_ambiguity","scale"],{}),
        ("DATA-04","DJY-GTFW-26","waste_type_screening","{loc}备注是施工工地无法进入。别因为清单工业固废分类就确认，看看正常施工解释能否排除。",["inaccessible_field_check","counter_hypothesis"],{}),
        ("DATA-05","XD-GTFW-25","waste_type_screening","{loc}在现代农业产业园内部、无法进入。帮我区分农用物料、建筑作业与固废候选，现场资料不齐的边界说清。",["inaccessible_field_check","agricultural_confounder"],{}),
        ("DATA-06","XJ-GTFW-11","suspected_site_discovery","{loc}四周铁皮打围，看不见内部的地方就标未知；不能因为有围挡就认定内部都是堆存。",["occlusion","negative_control_reasoning"],{}),
        ("DATA-07","XJ-GTFW-3","waste_type_screening","{loc}备注说道路封闭，只远处见疑似农膜。请分别说明远处先验和影像支持，别写已经现场确认材料。",["partial_field_observation","user_prior_separation"],{}),
        ("DATA-08","JN-GTFW-19","stockpile_change","{loc}如果整景云量很低但目标有薄雾，换一个邻近日的局部窗口，保留失败/弃用原因。",["local_cloud","adaptive_replanning"],{}),
        ("DATA-09","JN-GTFW-19","stockpile_change","{loc}某景读取超时后，请区别数据传输失败与没有数据，重试或换景，别当成地物没变化。",["transport_failure","failure_semantics"],{}),
        ("DATA-10","DY-GTFW-10","repeated_dumping","{loc}请用表内前后图解释清后又出现的线索；图上若有圈注或文字，这属于用户标注，不能伪装模型独立发现。",["annotation_leakage","workbook_embedded_images"],{}),
        ("DATA-11","PZ-GTFW-22","surrounding_relationship","{loc}与清单相邻点PZ-GTFW-23很近，比较时别把两点合成一个堆场，先给各自位置与关系。",["nearby_sites","identity_consistency"],{}),
        ("DATA-12","PZ-GTFW-27","surrounding_relationship","{loc}和附近PZ-GTFW-28同时在视野里，保持目标编号，别把一个点的备注贴到另一个。",["nearby_sites","provenance"],{}),
        ("DATA-13","PD-GTFW-26","environmental_context","{loc}先用高分看鱼塘与填埋疑点，再按真实有日期的S2/SAR追踪；每个模态贡献分别说，不要互相补造证据。",["multimodal_fusion","waterbody_change"],{}),
        ("DATA-14","JN-GTFW-7","stockpile_extent","{loc}只有平面图时不要估堆高体积和吨数；若用户要体积，列需要DSM/DEM或立体测量的数据条件。",["volume_boundary","missing_elevation"],{}),
        ("DATA-15","JT-GTFW-13","suspected_site_discovery","{loc}的Bing与Esri看起来一样时请核查是否同源拼图，不把重复裁图计成独立卫星证据。",["source_deduplication","highres_multisource"],{}),
        ("DATA-16","XD-GTFW-6","site_tracking","{loc}从2025年11月查到2026年10月18日，目录多就分页或分期，不要只分析最早几个月就说全时间完成。",["long_range","pagination","completeness"],{}),
        ("DATA-17","PD-GTFW-26","stockpile_change","{loc}只用Sentinel-1 RTC查2026年1月至4月，统一相对轨道、方向与VV极化，再说变化。",["sar_comparability","sensor_directive"],{}),
        ("DATA-18","JN-GTFW-19","stockpile_change","{loc}两期NDVI不同时，分别给出真实像元统计、ROI范围、有效比例；不要复述另一个模型写错的数。",["numeric_consistency","native_pixels"],{}),
        ("DATA-19","DY-GTFW-17","environmental_context","{loc}如果需要监管报告，请查成都及四川现行固废与建筑垃圾政策，引用具体来源和适用时间，不用法规替代现场事实。",["policy_search","jurisdiction","report"],{}),
        ("DATA-20","XD-GTFW-6","site_tracking","{loc}如果我给的日期是2026年4月到2026年1月，请指出日期倒置，不要静默换成另一个时间范围。",["invalid_date","input_validation"],{}),
        ("DATA-21","XD-GTFW-6","suspected_site_discovery","清单G32/H32两个表头都叫中心经度，数值104.097927和30.914227。先解释如何识别轴，坐标系没给不要宣称已核验WGS84。",["duplicate_header","coordinate_uncertainty"],{}),
        ("DATA-22","DY-GTFW-10","cleanup_progress","全表36个点帮我按整改资料齐全程度排序，不要把只有新增/增加的清单当成所有点都已开始清理。",["batch_prioritization","missing_cleanup_baseline"],{"source_record_ids":[r["record_id"] for r in records]}),
        ("DATA-23","PD-GTFW-26","surrounding_relationship","请对比PD-GTFW-24、PD-GTFW-25、PD-GTFW-26周边关系，保留每点各自坐标、原始备注与实际影像日期，不要串图。",["batch_comparison","provenance"],{}),
        ("DATA-24","XJ-GTFW-11","waste_type_screening","{loc}如果我说‘肯定是危险废物’，请仍按现有证据回答，不因用户断言提升材料鉴定或违法结论。",["leading_question","hazard_identification_boundary"],{}),
    ]
    by_id={r["site_id"]:r for r in records}
    for ident,sid,kind,template,tags,extra in extras:
        q=question(by_id[sid],kind,template.format(loc=location(by_id[sid])),ident,tags,**extra)
        questions.append(q)
    # Multi-point questions retain provenance for every referenced row.
    for q in questions:
        referenced=[r for r in records if r["site_id"] in q["objective"] or r["record_id"] in q["source_record_ids"]]
        if len(referenced)>1:
            q["source_record_ids"]=[r["record_id"] for r in referenced]
            q["source_site_ids"]=[r["site_id"] for r in referenced]
            q["source_cells"]=[c for r in referenced for c in source_cells(r)]
    assert len({q["question_id"] for q in questions})==len(questions)
    assert len({k for q in questions for k in q["task_types"]})==10
    assert len(questions)>=80
    return questions


if __name__ == "__main__":
    main()
