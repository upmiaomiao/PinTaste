"""Adapt the deployed RS model's trained XML answer to screened image candidates.

The schema and prompt come from the 2026-10-12 deployment's clients/xml_prompt.txt
and clients/rs_client.py. Only the final answer is adopted; raw model responses
remain the caller's audit material. No image coordinates become geographic data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any
import xml.etree.ElementTree as ET

from .schemas import Candidate, Observation, Recognition


NATIVE_XML_PROMPT = """请对整幅影像作固废监管判读，不预设目标存在，不预设类别。可以在<thinking>中保留按影像需要组织的分析，但最终只能给出一个<answer>，评测只读取该块。必须使用以下字段：<decision>存在固废堆存、未发现固废堆存或无法判定</decision><risk_level>低、中、高或无法判定</risk_level><regions>目标区域列表</regions><confidence>0到1之间的数值</confidence><conclusion>简洁描述可见证据、主要混淆物、结论与边界</conclusion>。每个目标区域使用<region id="R1"><label>建筑垃圾、生活垃圾或一般工业固废中的类别名称</label><bbox coord="normalized_1000">[left,top,right,bottom]</bbox></region>；坐标为整图0—1000归一化值。未发现目标或无法判定时regions为空，不输出虚构目标框。只依据可见证据，不推断不可见的化学成分、拍摄日期或精确米制距离。"""

TRAINED_LABELS = {"建筑垃圾", "生活垃圾", "一般工业固废"}
DECISIONS = {"存在固废堆存", "未发现固废堆存", "无法判定"}
RISK_LEVELS = {"低", "中", "高", "无法判定"}
ANSWER_FIELDS = {"decision", "risk_level", "regions", "confidence", "conclusion"}


class RSXMLValidationError(ValueError):
    """The model answer does not satisfy its deployed native XML schema."""


@dataclass(frozen=True)
class NativeRSResult:
    """A typed screening result alongside the original final-answer field values."""

    recognition: Recognition
    native: dict[str, Any]


def _plain(node: ET.Element) -> str:
    if node.attrib or len(node):
        raise RSXMLValidationError("原生 XML 标量字段必须是无属性的纯文本")
    return (node.text or "").strip()


def _no_mixed_text(node: ET.Element) -> None:
    if (node.text or "").strip() or any((child.tail or "").strip() for child in node):
        raise RSXMLValidationError("原生 XML 包含字段之外的文本")


def parse_rs_xml(content: str, *, asset_id: str, width: int, height: int) -> NativeRSResult:
    """Validate one trained answer and convert its 0..1000 boxes to image pixels.

    Left/top use floor and right/bottom use ceil to retain the candidate extent.
    The optional thinking block is never promoted to an image observation.
    Confidence and risk remain explicitly identified as model self-reports.
    """
    if not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
        raise RSXMLValidationError("原生 XML 为空、类型错误或超过大小上限")
    if not asset_id or type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise RSXMLValidationError("必须提供真实影像标识和正整数像素尺寸")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", content, flags=re.IGNORECASE):
        raise RSXMLValidationError("原生 XML 不允许 DTD 或实体声明")
    matches = list(re.finditer(r"<answer(?:\s[^>]*)?>[\s\S]*?</answer\s*>", content))
    if len(re.findall(r"<answer(?:\s[^>]*)?>", content)) != 1 or len(matches) != 1:
        raise RSXMLValidationError("原生 XML 必须包含且仅包含一个完整 answer")
    match = matches[0]
    before, after = content[:match.start()], content[match.end():]
    if (re.fullmatch(r"\s*(?:<thinking>[\s\S]*?</thinking>\s*)?", before) is None
            or len(re.findall(r"<thinking>", before)) > 1 or after.strip()):
        raise RSXMLValidationError("原生 XML 仅允许可选 thinking 和唯一 answer")
    try:
        answer = ET.fromstring(match.group(0))
    except ET.ParseError as error:
        raise RSXMLValidationError("原生 answer XML 无法解析") from error
    if answer.tag != "answer" or answer.attrib:
        raise RSXMLValidationError("原生 answer 元素无效")
    _no_mixed_text(answer)
    if len(answer) != len(ANSWER_FIELDS) or {child.tag for child in answer} != ANSWER_FIELDS:
        raise RSXMLValidationError("原生 answer 字段缺失、重复或不受支持")
    fields = {child.tag: child for child in answer}
    decision = _plain(fields["decision"])
    risk = _plain(fields["risk_level"])
    conclusion = _plain(fields["conclusion"])
    if decision not in DECISIONS or risk not in RISK_LEVELS or not conclusion:
        raise RSXMLValidationError("原生判定、风险枚举或结论无效")
    if re.search(r"</?(?:thinking|think|analysis)\b", conclusion, flags=re.IGNORECASE):
        raise RSXMLValidationError("最终结论不得包含内部推理标记")
    try:
        confidence = float(_plain(fields["confidence"]))
    except ValueError as error:
        raise RSXMLValidationError("原生 confidence 必须为 0..1 有限数值") from error
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise RSXMLValidationError("原生 confidence 必须为 0..1 有限数值")
    regions_node = fields["regions"]
    if regions_node.attrib:
        raise RSXMLValidationError("原生 regions 不能包含属性")
    _no_mixed_text(regions_node)
    if len(regions_node) > 30:
        raise RSXMLValidationError("原生目标区域超过当前识别结果上限")
    region_ids: set[str] = set()
    regions: list[dict[str, Any]] = []
    for node in regions_node:
        region_id = node.attrib.get("id", "")
        if (node.tag != "region" or set(node.attrib) != {"id"}
                or re.fullmatch(r"R[1-9][0-9]*", region_id) is None or region_id in region_ids):
            raise RSXMLValidationError("原生 region 必须有唯一 R1/R2 格式标识")
        region_ids.add(region_id)
        _no_mixed_text(node)
        if len(node) != 2 or {child.tag for child in node} != {"label", "bbox"}:
            raise RSXMLValidationError("原生 region 必须仅包含唯一 label 和 bbox")
        label_node, box_node = node.find("label"), node.find("bbox")
        label = _plain(label_node)
        if label not in TRAINED_LABELS:
            raise RSXMLValidationError("原生 label 超出已训练的三个固废类别")
        if box_node.attrib != {"coord": "normalized_1000"} or len(box_node):
            raise RSXMLValidationError("原生 bbox 必须使用 normalized_1000 坐标")
        try:
            box = json.loads((box_node.text or "").strip())
        except (ValueError, TypeError) as error:
            raise RSXMLValidationError("原生 bbox 不是四元素数值列表") from error
        if (not isinstance(box, list) or len(box) != 4
                or any(type(value) not in (int, float) or not 0 <= value <= 1000
                       or not math.isfinite(value) for value in box)
                or not (box[0] < box[2] and box[1] < box[3])):
            raise RSXMLValidationError("原生 bbox 数值越界或左右上下顺序无效")
        pixel_window = (
            math.floor(box[0] * width / 1000), math.floor(box[1] * height / 1000),
            math.ceil(box[2] * width / 1000), math.ceil(box[3] * height / 1000),
        )
        regions.append({"id": region_id, "label": label, "bbox": box,
                        "coord": "normalized_1000", "pixel_window": pixel_window, "asset_id": asset_id})
    if (decision == "存在固废堆存") != bool(regions):
        raise RSXMLValidationError("原生 decision 与目标框数量不一致")

    observations = [Observation(asset_id=asset_id,
        statement=f"原生模型最终判读（待核验）：{decision}。{conclusion[:1850]}")]
    candidates: list[Candidate] = []
    for region in regions:
        evidence_index = len(observations)
        observations.append(Observation(asset_id=asset_id, pixel_window=region["pixel_window"],
            statement=f"原生模型标注候选 {region['id']}，模型类别为{region['label']}；像素框由整图 normalized_1000 坐标换算。"))
        candidates.append(Candidate(label=f"疑似{region['label']}", confidence=confidence,
            evidence_indices=[evidence_index],
            alternative_explanations=["正常施工或物料堆场等是通用待排查假设，尚未据此影像排除"],
            verification=["对照原始影像核验框内纹理、形态及场景关系，并通过现场核查确认材料性质和管理状态"]))
    unknowns = ["confidence 是整图模型自报值，未经校准；各候选继承该值，不代表逐框独立置信度",
                "原生 risk_level 是模型筛查风险判断，不能视为污染检测、危险废物鉴别或法定风险等级",
                "像素候选框仅表示整图中的位置，未产生面积、距离、地理坐标或变化测量"]
    if len(conclusion) > 1850:
        unknowns.append("原生 conclusion 超过观测字段上限，观测文字已截短；完整结论保存在 native.conclusion")
    if decision == "未发现固废堆存":
        unknowns.append("未发现目标仅限本幅影像及当前可见条件，不能证明不存在固废")
    elif decision == "无法判定":
        unknowns.append("原生模型无法判定，不能将空目标列表解释为未发现固废")
    recognition = Recognition(observations=observations, candidates=candidates, unknowns=unknowns)
    native = {"format": "wuyu_rs_xml_v1", "decision": decision, "risk_level": risk,
              "risk_scope": "model_screening_self_report", "confidence": confidence,
              "confidence_scope": "whole_image_uncalibrated_self_report",
              "conclusion": conclusion, "regions": regions, "asset_id": asset_id,
              "image_size": {"width": width, "height": height}}
    native["adopted_evidence"] = "validated_final_answer_and_registered_image_only"
    return NativeRSResult(recognition=recognition, native=native)
