"""Pure conversational preflight: ask one necessary question, then adapt to the reply.

This module neither plans a fixed inspection sequence nor calls models, APIs or
the database. The HTTP adapter supplies already owner-checked parent context and
attachment records. Attachment text and previous model conclusions are not input
instructions and are never mined here for coordinates or permissions.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

from .schemas import TaskRequest


def _record(task) -> tuple[dict, set]:
    if isinstance(task, TaskRequest):
        return task.model_dump(mode="json"), set(task.model_fields_set)
    if isinstance(task, Mapping):
        return dict(task), set(task)
    raise ValueError("任务应为请求对象")


def _parent(parent_task) -> dict:
    if parent_task is None:
        return {}
    parent, _ = _record(parent_task)
    task = parent.get("task")
    return {**parent, **task} if isinstance(task, Mapping) else parent


def _question(kind: str, question: str, options: list[tuple[str, str, str]], **metadata) -> dict:
    return {"id": kind, "kind": kind, "question": question,
            "options": [{"value": value, "label": label, "description": detail} for value, label, detail in options],
            "allow_free_text": True, "free_text": True, **metadata}


def _pure_information(task: dict) -> bool:
    from .task_modes import pure_information
    return pure_information(task)


def _location(task: dict, provided: set, parent: dict) -> tuple[bool, str, bool]:
    """Only structured request/context counts, never model-written narrative text."""
    own = task.get("center") or task.get("bbox")
    explicit_scope = bool(provided & {"center", "bbox"})
    old = parent.get("center") or parent.get("bbox")
    inherited = not explicit_scope and not own and bool(old)
    location = own or (old if inherited else None)
    basis = task.get("coordinate_system") if "coordinate_system" in provided else None
    if not basis and (inherited or (own and old and list(own) == list(old))):
        basis = parent.get("coordinate_system")
    if not basis and re.search(r"WGS\s*[-_]?\s*84|EPSG\s*:\s*4326", str(task.get("objective", "")), re.I):
        basis = "wgs84"
    geographic_basis = task.get("geographic_basis")
    if not basis and isinstance(geographic_basis, dict) and geographic_basis.get("user_confirmed_search_area") is True:
        basis = "wgs84" if geographic_basis.get("center_wgs84") else None
    return bool(location), basis or "unknown", inherited


def _is_image(record: dict) -> bool:
    return record.get("kind") == "image" or str(record.get("mime", "")).startswith("image/") or record.get("metadata", {}).get("modality") in {"optical", "multispectral", "hyperspectral", "sar"}


def _uploaded_images(task: dict, attachments, parent: dict) -> list[dict]:
    if attachments is not None:
        return [record for record in attachments if isinstance(record, dict) and _is_image(record)
                and record.get("metadata", {}).get("source", "user_upload") in {"user_upload", "user_workbook_embedded"}]
    inherited = {image.get("id") for image in parent.get("images", []) if isinstance(image, dict)
                 and image.get("metadata", {}).get("source") not in {None, "user_upload", "user_workbook_embedded"}}
    return [{"id": asset_id} for asset_id in task.get("asset_ids", []) if asset_id not in inherited]


def _period_given(task: dict, provided: set, parent: dict, attachments) -> bool:
    # An explicitly cleared/new bound must not be completed with an unrelated
    # previous period. The adapter can later normalize natural-language dates.
    changing_period = bool(provided & {"date_start", "date_end"})
    start = task.get("date_start") if changing_period else task.get("date_start") or parent.get("date_start")
    end = task.get("date_end") if changing_period else task.get("date_end") or parent.get("date_end")
    if start and end:
        return True
    text = str(task.get("objective", ""))
    # A declared relative window is resolved later against the task creation
    # date; an unbounded "recently" alone is insufficient for a comparison.
    if len(re.findall(r"(?:19|20)\d{2}", text)) >= 2 or re.search(r"去年.{0,8}今年|今年.{0,8}去年|(?:近|最近)\s*(?:\d+|[一二两三四五六七八九十]+)\s*(?:天|个?月|年|周|星期)", text):
        return True
    dates = {str(row.get("metadata", {}).get("acquisition_time", ""))[:10]
             for row in attachments or [] if isinstance(row, dict) and _is_image(row)
             and row.get("metadata", {}).get("acquisition_time")}
    return len(dates) >= 2


def next_question(task: TaskRequest | dict, *, parent_task: TaskRequest | dict | None = None,
                  attachments: list[dict] | None = None, answers: dict | None = None) -> dict | None:
    """Return at most one missing prerequisite, with selectable and free-form replies.

    ``answers`` is an adapter-owned normalized answer map, not arbitrary model
    output. Accepted values are the question's enum options. Free text should be
    parsed by the harness into a revised task and passed through preflight again.
    In particular, a free-text date or address must never set a boolean "answered"
    flag that bypasses validation.
    """
    current, provided = _record(task)
    parent, answers = _parent(parent_task), answers or {}
    if _pure_information(current):
        return None
    uploads = _uploaded_images(current, attachments, parent)
    supplement = current.get("online_supplement", "auto")
    if answers.get("online_supplement") in {"allowed", "uploaded_only"}:
        supplement = answers["online_supplement"]
    if supplement in {"auto", "ask"} and uploads:
        previous = parent.get("online_supplement")
        if "online_supplement" not in provided and previous in {"allowed", "uploaded_only"}:
            supplement = previous
        else:
            return _question("online_supplement", "这次先分析你上传的影像，还是允许在需要时用平台影像辅助核查？", [
                ("uploaded_only", "只分析上传影像", "保留现有影像证据，不申请平台卫星数据。"),
                ("allowed", "允许平台影像辅助", "见微先判读上传影像，有明确证据缺口时再补充相关图源。"),
            ], scope="current_task", permission_required=True)
    location, coordinate_basis, inherited = _location(current, provided, parent)
    if supplement == "uploaded_only" and not uploads and not current.get("asset_ids") and not parent.get("images"):
        return _question("location", "这轮仅分析上传资料，请先添加影像；也可以改为允许平台按位置取图。", [
            ("upload_images", "上传自己的影像", "添加需要判读的影像或影像资料包。"),
            ("allow_platform", "改用平台影像", "先确认位置，再按问题选择合适图源。"),
        ], action="provide_analysis_material")
    # Uploaded imagery can be interpreted without a declared geographic point.
    # Only actual platform acquisition needs a structured, confirmed search area.
    needs_location = supplement != "uploaded_only" and (not uploads or supplement == "allowed")
    if needs_location and not location:
        return _question("location", "你想核查哪里？可以搜索地名选择地点，也可以直接给经纬度或上传影像。", [
            ("search_place", "搜索地名", "输入城市、园区、企业或详细地址，核对候选地点后选择检索区域。"),
            ("coordinates", "输入经纬度或范围", "提供坐标及来源，便于避免高德等地图坐标偏移。"),
            ("upload_images", "上传自己的影像", "支持先看上传资料，再决定是否需要平台辅助。"),
        ], action="resolve_location", geocoding_required_for_place_name=True)
    if needs_location and location and coordinate_basis != "wgs84":
        if answers.get("coordinate_basis") != "wgs84":
            return _question("coordinate_basis", "这组经纬度来自哪里？不同坐标体系会让同一地点产生偏移。", [
                ("wgs84", "GPS / WGS84", "按地理经纬度检索，仍需检查实际影像配准。"),
                ("gcj02", "高德或腾讯地图", "先转换并保留原始坐标与转换记录，再确认检索区域。"),
                ("unknown", "暂不确定", "可以改用地名搜索，或继续仅作未核实位置的影像解读。"),
            ], action="confirm_coordinate_basis", inherited_location=inherited)
    research = current.get("research") or {}
    from .task_modes import TEMPORAL
    temporal = research.get("mode") == "timeseries" or bool(re.search(TEMPORAL, str(current.get("objective", ""))))
    if answers.get("comparison_period") == "current_only":
        temporal = False
    if temporal and not _period_given(current, provided, parent, attachments):
        return _question("comparison_period", "希望比较哪两个日期或哪个起止时段？目前底图的更新年份不能代替实际拍摄日期。", [
            ("since2021", "从2021年到最近可用影像", "先查询历史日期，再按真实成像时相排列过去与最近的证据。"),
            ("provide_period", "填写比较时段", "例如2025年1月至2026年10月，随后查询实际可用时相。"),
            ("upload_dated", "上传已知日期的影像", "注明拍摄日期；比较前仍需核验同一区域和配准。"),
            ("current_only", "先分析当前可见情况", "这一轮保留单期判读，不据此认定清理、复倒或时序变化。"),
        ], action="define_comparison_period")
    return None


def preflight(task: TaskRequest | dict, **context) -> dict:
    """Compact HTTP adapter result; it does not certify a model's scientific answer."""
    current, _ = _record(task)
    question = next_question(task, **context)
    return {"ready": question is None, "question": question,
            "mode": "information_search" if _pure_information(current) else "remote_sensing_harness",
            "interaction": "one_question_at_a_time", "free_text_allowed": True}
