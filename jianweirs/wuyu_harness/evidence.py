"""Deterministic data gates and pixel annotation checks, independent of model scores."""

from __future__ import annotations

import re

from .schemas import Recognition, TaskKind, TaskRequest

CHANGE_TASKS = {TaskKind.CHANGE, TaskKind.CLEANUP, TaskKind.RECURRENCE, TaskKind.TRACKING}


def _visible_water_adjacency(text: str) -> bool:
    """A recorded visible relation is a review priority, never a GIS distance."""
    for clause in re.split(r'[。；;\n]', text):
        if not re.search(r'水体|河流|河道|河岸|沟渠|湖泊|水库|池塘|临水|河边|湖边|岸边', clause):
            continue
        if re.search(r'(?:未|不|无法|不能|尚未|未能).{0,10}(?:临水|邻近|紧邻|靠近|毗邻|临近|邻接|沿河)', clause):
            continue
        if re.search(r'临水|紧邻|毗邻|邻近|临近|靠近|邻接|水体边|河岸边|河边|湖边|沟渠边|沿河|岸边', clause):
            return True
    return False


def candidate_presence_evidence(assets, assessments, recognition=None, requested_period=None, visual_review=None, *, task=None):
    """Keep candidate existence, date applicability and negative coverage separate.

    An uncertain or spatially unverified candidate remains an *image* clue. A
    completed, effective exclusion wins over its raw detector label. Neither a
    date gap nor another image's negative can remove a retained candidate.
    """
    from .presentation import same_point_eligible, spatial_state

    recognition = recognition.model_dump(mode='json') if hasattr(recognition, 'model_dump') else (recognition or {})
    period = requested_period or {}
    period_rows = {row.get('asset_id'): row for row in period.get('assets', [])}
    assessed = {row.get('asset_id'): row for row in assessments}
    observations = recognition.get('observations') or []
    recognition_by_asset = {}
    for candidate in recognition.get('candidates') or []:
        ids = {observations[i].get('asset_id') for i in candidate.get('evidence_indices', [])
               if type(i) is int and 0 <= i < len(observations)}
        if not ids and len(assets) == 1:
            ids = {assets[0]['id']}
        for identity in ids:
            recognition_by_asset.setdefault(identity, []).append(candidate)

    def count(value):
        return value if type(value) is int and value > 0 else 0

    def bbox(value):
        if (not isinstance(value, (tuple, list)) or len(value) != 4
                or any(type(v) not in (float, int) for v in value)):
            return None
        a, b, c, d = value
        return list(value) if -180 <= a < c <= 180 and -90 <= b < d <= 90 else None

    task_values = task.model_dump(mode='json') if hasattr(task, 'model_dump') else (task or {})
    target_bbox = bbox(task_values.get('bbox'))
    center = task_values.get('center')
    rows, priorities, negative_scope = [], [], []
    cross = visual_review or {}
    cross_negative_ids = (set(cross.get('evidence_asset_ids') or []) if cross.get('status') == 'completed'
                          and cross.get('alternative_explanations_reviewed') is True else set())
    for asset in assets:
        identity = asset['id']; meta = asset.get('metadata') or {}
        if meta.get('modality', 'optical') != 'optical':
            continue
        row = assessed.get(identity, {})
        review = row.get('optical_false_positive_review') or {}
        completed = review.get('status') == 'completed'
        reviewed = review.get('candidates') or [] if completed else []
        excluded = max(sum(item.get('effective_status') == 'excluded_obvious_false_positive' for item in reviewed),
                       count(review.get('excluded_candidate_count')) if completed else 0)
        supported = max(sum(item.get('effective_status') == 'supported_candidate' for item in reviewed),
                        count(review.get('supported_candidate_count')) if completed else 0)
        unresolved = max(sum(item.get('effective_status') not in {'excluded_obvious_false_positive', 'supported_candidate'} for item in reviewed),
                         count(review.get('uncertain_candidate_count')) if completed else 0)
        explicit_review_decisions = bool(reviewed or supported or unresolved or excluded)
        explicit_all_excluded = (completed and excluded > 0 and not supported and not unresolved
            and not count(row.get('candidate_count')) and count(row.get('jianwei_raw_candidate_count')) <= excluded)
        effective_candidates = recognition_by_asset.get(identity, []) if not explicit_all_excluded else []
        retained = max(supported + unresolved, count(row.get('candidate_count')), len(effective_candidates))
        if explicit_all_excluded:
            # Raw counts/labels belong only in the audit after an exclusion.
            retained = 0
        elif row.get('decision') == 'positive' or row.get('image_only_decision') == 'positive':
            retained = max(retained, 1)
        elif not completed and (row.get('jianwei_raw_decision') == 'positive' or count(row.get('jianwei_raw_candidate_count'))):
            retained = max(retained, count(row.get('jianwei_raw_candidate_count')), 1)
        if completed and review.get('additional_suspected_regions') is True:
            retained = max(retained, supported + unresolved + 1)
        unresolved = max(unresolved, retained - supported)
        localized = (row.get('same_point_eligible') is not False and same_point_eligible(asset))
        qualified_quality = row.get('quality_passed') is True and not row.get('quality_issues')
        effective_decision = row.get('decision', 'unknown')
        if completed and explicit_review_decisions:
            # Reviewer uncertainty keeps a detector clue positive; it is not a
            # negative and must not demote the image to "insufficient".
            effective_decision = ('positive' if supported or unresolved else
                                  'negative' if review.get('qualified_scene_negative') is True else 'unknown')
            if not localized:
                effective_decision = 'unknown'
        scene_negative = False
        if not retained and effective_decision == 'negative' and qualified_quality and row.get('target_relevant') is True and localized:
            if completed:
                scene_negative = review.get('qualified_scene_negative') is True
            else:
                native_negative = (row.get('jianwei_raw_decision') == 'negative'
                    and row.get('jianwei_raw_candidate_count') == 0 and row.get('candidate_count') == 0
                    and bool(row.get('analysis_summary')))
                scene_negative = native_negative or identity in cross_negative_ids
        period_row = period_rows.get(identity, {})
        period_status = ('not_required' if not period.get('required') else 'qualified' if period_row.get('eligible') is True
                         else 'outside_requested_period' if period_row.get('actual_dates_verified') is True else 'date_unverified')
        api = asset.get('data_api_metadata') or {}; spatial = spatial_state(asset)
        actual_bbox = bbox(api.get('actual_bbox_wgs84')) or bbox((spatial.get('reference_extent') or {}).get('actual_bbox_wgs84'))
        # Fetched online for the task coordinates with a provider-reported
        # footprint that covers the task point: nominally the same place, but
        # not control-point verified. Enough to keep a clue at "medium", never
        # enough for "high"/"low".
        covers_target = bool(actual_bbox) and (
            (center and actual_bbox[0] <= center[0] <= actual_bbox[2] and actual_bbox[1] <= center[1] <= actual_bbox[3])
            or (target_bbox and actual_bbox[0] <= target_bbox[2] and target_bbox[0] <= actual_bbox[2]
                and actual_bbox[1] <= target_bbox[3] and target_bbox[1] <= actual_bbox[3]))
        nominal_location = (not localized and covers_target and spatial.get('status') not in {'misaligned', 'failed'}
                            and asset.get('acquisition_phase') not in (None, 'user_upload')
                            and not asset.get('derived_from_asset_id'))
        footprint_verified = bool(actual_bbox and spatial.get('strict_same_point_eligible') is True
            and localized and api.get('full_requested_bbox_covered', spatial.get('full_requested_bbox_covered')) is not False)
        entry = {'asset_id': identity, 'pending_candidate_count': retained,
            'supported_candidate_count': supported, 'unresolved_candidate_count': unresolved,
            'excluded_candidate_count': excluded, 'candidate_status': 'pending_verification' if retained else
                'all_candidates_excluded' if excluded else 'none_identified' if scene_negative else 'unknown',
            'same_point_eligible': localized, 'nominal_task_location': nominal_location,
            'candidate_scope': 'task_location' if footprint_verified else 'image_only',
            'effective_decision': effective_decision,
            'quality_passed': qualified_quality, 'qualified_scene_negative': scene_negative,
            'actual_bbox_wgs84': actual_bbox if footprint_verified else None,
            'spatial_footprint_verified': footprint_verified,
            'derived_from_asset_id': asset.get('derived_from_asset_id'),
            'requested_period_status': period_status, 'requested_period_eligible': period_row.get('eligible') if period.get('required') else None}
        rows.append(entry)
        if (not asset.get('derived_from_asset_id') and meta.get('role') not in {'full_scene', 'local_context'}
                and asset.get('analysis_scope') != 'background_only'):
            negative_scope.append(entry)
        if not retained:
            continue
        visible = []
        for item in reviewed:
            if item.get('effective_status') == 'excluded_obvious_false_positive':
                continue
            values = item.get('visible_evidence') or []
            visible.extend(values if isinstance(values, list) else [values])
        for candidate in effective_candidates:
            visible.extend(observations[i].get('statement', '') for i in candidate.get('evidence_indices', [])
                           if type(i) is int and 0 <= i < len(observations) and observations[i].get('asset_id') == identity)
        # A whole-scene summary must explicitly link a candidate to the water.
        visible.extend(value for value in row.get('analysis_summary', []) if isinstance(value, str)
                       and re.search(r'候选|疑点|堆体|堆物|堆存|物料|废物|垃圾', value))
        cues = list(dict.fromkeys(str(value) for value in visible if _visible_water_adjacency(str(value))))
        if cues:
            priorities.append({'asset_id': identity, 'factor': 'visible_water_adjacency', 'basis': cues[:3],
                'scope': 'qualitative_image_relation', 'priority': 'prioritize_field_verification',
                'distance_measured': False, 'pollution_confirmed': False,
                'verification': '优先核查临水候选的位置、物料性质、岸边排水及防护；可见邻接不证明污染已发生'})
    pending = [row['asset_id'] for row in rows if row['pending_candidate_count']]
    exclusions = sum(row['excluded_candidate_count'] for row in rows)
    negative_complete = bool(negative_scope) and not pending and all(row['qualified_scene_negative'] for row in negative_scope)
    scope_complete = negative_complete
    # A user's own uploaded image is the evidence they ask about; it has no
    # provider footprint, so footprint coverage cannot be required of it.
    by_asset = {a['id']: a for a in assets}
    upload_scope = bool(negative_scope) and all(
        (by_asset[row['asset_id']].get('metadata') or {}).get('source') == 'user_upload'
        and not by_asset[row['asset_id']].get('data_api_metadata')
        for row in negative_scope)
    if (target_bbox or center) and not upload_scope:
        qualified_extents = [row['actual_bbox_wgs84'] for row in negative_scope
                             if row['qualified_scene_negative'] and row['spatial_footprint_verified']]
        if target_bbox:
            scope_complete = negative_complete and any(a <= target_bbox[0] and b <= target_bbox[1]
                and c >= target_bbox[2] and d >= target_bbox[3] for a, b, c, d in qualified_extents)
        else:
            scope_complete = negative_complete and any(a <= center[0] <= c and b <= center[1] <= d
                for a, b, c, d in qualified_extents)
    conditional = [row['asset_id'] for row in rows if row['pending_candidate_count'] and row['requested_period_status'] not in {'qualified', 'not_required'}]
    return {'schema_version': 'candidate-presence-v1',
        'status': 'pending_verification' if pending else 'all_candidates_excluded' if exclusions else 'none_identified' if negative_complete else 'unknown',
        'has_pending_candidates': bool(pending), 'pending_asset_ids': pending,
        'image_only_pending_asset_ids': [row['asset_id'] for row in rows if row['pending_candidate_count'] and row['candidate_scope'] == 'image_only'],
        'localized_pending_asset_ids': [row['asset_id'] for row in rows if row['pending_candidate_count'] and row['same_point_eligible']],
        'nominal_location_pending_asset_ids': [row['asset_id'] for row in rows if row['pending_candidate_count']
                                               and not row['same_point_eligible'] and row['nominal_task_location']],
        'supported_asset_ids': [row['asset_id'] for row in rows if row['supported_candidate_count']],
        'unresolved_asset_ids': [row['asset_id'] for row in rows if row['unresolved_candidate_count']],
        'excluded_candidate_count': exclusions, 'all_observed_candidates_effectively_excluded': bool(exclusions and not pending),
        'negative_review_complete': negative_complete,
        'negative_task_scope_complete': scope_complete,
        'negative_review_asset_ids': [row['asset_id'] for row in negative_scope if row['qualified_scene_negative']],
        'incomplete_review_asset_ids': [row['asset_id'] for row in negative_scope if not row['qualified_scene_negative']],
        'requested_period_required': bool(period.get('required')),
        'requested_period_status': 'not_required' if not period.get('required') else 'conditional_pending' if conditional else
            'qualified_pending' if pending else 'qualified_negative' if negative_complete and period.get('ready') else 'incomplete',
        'requested_period_qualified_pending_asset_ids': [row['asset_id'] for row in rows if row['pending_candidate_count'] and row['requested_period_status'] == 'qualified'],
        'conditional_pending_asset_ids': conditional, 'priority_factors': priorities, 'assets': rows,
        'negative_scope': '本轮已审查影像的可判读范围，不是区域或现实中不存在固废的证明'}


def effective_image_decision(row) -> str:
    """One image's effective decision after same-image review, for any renderer.

    Mirrors candidate_presence_evidence: reviewer uncertainty keeps a positive
    clue; only a qualified scene negative counts as negative; an unverified
    spatial correspondence stays unknown.
    """
    row = row if isinstance(row, dict) else {}
    if row.get('decision') == 'unknown' and (row.get('same_point_eligible') is False
            or row.get('reason') == 'spatial_registration_unverified'
            or row.get('spatial_status') in ('unknown', 'misaligned')):
        return 'unknown'
    review = row.get('optical_false_positive_review') or {}
    if review.get('status') == 'completed':
        decision = review.get('effective_decision')
        if decision == 'negative':
            return 'negative' if review.get('qualified_scene_negative') is True else 'unknown'
        if decision in ('positive', 'unknown'):
            return decision
        statuses = {item.get('effective_status') for item in review.get('candidates') or []}
        if statuses & {'supported_candidate', 'uncertain'}:
            return 'positive'
        if statuses == {'excluded_obvious_false_positive'}:
            return 'negative' if review.get('qualified_scene_negative') is True else 'unknown'
    return row.get('decision') if row.get('decision') in ('positive', 'negative') else 'unknown'


def candidate_presence_judgment(presence, *, historical_only=False):
    """Lead with the unresolved clue while preserving time and location limits."""
    if not presence.get('has_pending_candidates'):
        return None
    if historical_only:
        return '历史影像存在待核查风险点，后期状态与清理或复发情况尚不能确定'
    image_only = set(presence.get('pending_asset_ids') or []) <= set(presence.get('image_only_pending_asset_ids') or [])
    lead = '图内存在待核查风险点，地理位置与物料类型仍需核验' if image_only else '存在待核查风险点，疑似固废类型与现场状态仍需核验'
    if presence.get('conditional_pending_asset_ids'):
        lead = ('图内存在待核查风险点，地理位置、历史时段与物料类型仍待核验' if image_only else
                '存在待核查风险点，历史时段适用性与疑似固废类型仍待核验')
    return lead


def assess_quality(task: TaskRequest, assets: list[dict]) -> dict:
    gates: dict[str, dict] = {}
    usable = [a for a in assets if a["metadata"]["modality"] == "optical" or a.get("visual_preview")]
    gates["visual_screening"] = {"allowed": bool(usable), "reason": "仅对可见光预览进行候选场景筛查" if usable else "缺少可分析的光学影像"}
    metric = bool(usable) and all(
        a["metadata"].get("georeferenced") and a["metadata"].get("crs")
        and a["metadata"].get("resolution_m") for a in usable
    )
    # Georeferencing declarations alone do not supply a pixel transform or GIS measurements.
    gates["area_distance"] = {"allowed": False, "reason": "尚未接入经核验的像素地理变换与 GIS 测量工具"}
    metadata_ready = len(usable) >= 2 and all(
        a["metadata"].get("acquisition_time") and a["metadata"].get("registration_verified")
        and a["metadata"].get("radiometric_comparability_verified") for a in usable
    ) and len({a["metadata"].get("acquisition_time") for a in usable}) >= 2
    gates["attributed_change"] = {"allowed": False, "reason": "变化计算模块尚未接入；元数据完整也不能代替配准与变化验证",
                                   "metadata_ready": metadata_ready, "requested": bool(set(task.task_types) & CHANGE_TASKS)}
    gates["volume"] = {"allowed": False, "reason": "未提供经配准的 DSM/DEM 或立体高程证据"}
    gates["legal_determination"] = {"allowed": False, "reason": "模型输出为监管筛查线索，需有权人员核实"}
    return {"gates": gates, "measurement_metadata_present": metric,
            "warnings": ["上传元数据为用户声明，未独立核验", "获取时间不等于卫星拍摄时间", "预览图不代替科学波段数值分析"]}


def validate_recognition(result: Recognition, assets: list[dict]) -> None:
    by_id = {a["id"]: a for a in assets}
    for obs in result.observations:
        if obs.asset_id not in by_id:
            raise ValueError("模型引用了不存在的影像")
        if obs.pixel_window:
            x1, y1, x2, y2 = obs.pixel_window
            image = by_id[obs.asset_id]
            if not (0 <= x1 < x2 <= image["width"] and 0 <= y1 < y2 <= image["height"]):
                raise ValueError("模型标注超出原始图像像素范围")
    for candidate in result.candidates:
        if any(i < 0 or i >= len(result.observations) for i in candidate.evidence_indices):
            raise ValueError("模型候选引用了不存在的观测")
    reject_unsupported_claims(result.model_dump_json())


_FORBIDDEN_PHRASES = ("已确认违法", "已确认固废", "确定为危险废物", "已确认危险废物", "已证实污染超标")
_MEASURE = re.compile(r"\d+(?:\.\d+)?\s*(?:平方米|平方公里|立方米|公顷|亩|m²|m³|km²)", re.IGNORECASE)
_ASSERTION = re.compile(r"(?<!不)(?:属于|确认为|认定为|构成|已发生|已造成)\s*(?:了)?\s*(?:危险废物|违法倾倒|污染超标)")
_REASONING_TAG = re.compile(r"</?(?:thinking|think|analysis)\b", re.IGNORECASE)


def _qualified_as_unknown(text: str, start: int) -> bool:
    # Scope a negation/uncertainty to its clause. In particular, the trained
    # model's "无法判定是否属于危险废物" is a boundary, not a hazard finding.
    prefix = re.split(r"[，,。；;！!？?\n]|但是|然而|不过|但|事实上|实际已", text[:start])[-1][-32:]
    return bool(re.search(r"(?:无法|不能|不可|尚未|未能|难以|不足以|不应|不宜|不得|并非|不是|不确定|未确认|未认定|未确定)[^，,。；;！!？?\n]{0,24}$|是否\s*$", prefix))


def _measure_qualified(text: str, start: int, end: int) -> bool:
    """A figure is acceptable when its own sentence denies measuring it or
    attributes it to user-supplied material ("无法测量约100平方米", "资料记载")."""
    left = re.split(r"[。；;！!？?\n]", text[:start])[-1]
    right = re.split(r"[。；;！!？?\n]", text[end:])[0]
    clause = left + text[start:end] + right
    return bool(re.search(r"无法|不能|未能|尚未|未经|没有|不具备|不计算|不输出|不报告|不提供|不代表"
                          r"|用户(?:提供|资料|上传|填写)|资料(?:记载|显示|中)|台账|原表|据(?:用户|资料|举报)", clause))


def claim_issues(text: str) -> list[str]:
    """List unsupported-claim categories in text without raising."""
    issues = []
    if _REASONING_TAG.search(text):
        issues.append("internal_reasoning")
    if any(not _qualified_as_unknown(text, m.start()) for phrase in _FORBIDDEN_PHRASES
           for m in re.finditer(re.escape(phrase), text)):
        issues.append("regulatory_determination")
    if any(not _measure_qualified(text, m.start(), m.end()) for m in _MEASURE.finditer(text)):
        issues.append("unmeasured_quantity")
    if any(not _qualified_as_unknown(text, m.start()) for m in _ASSERTION.finditer(text)):
        issues.append("hazard_or_violation_assertion")
    return issues


def reject_unsupported_claims(text: str) -> None:
    # Conservative product gate; all structured output still requires human review.
    issues = claim_issues(text)
    if "internal_reasoning" in issues:
        raise ValueError("公开分析仅接受可核验摘要，不采用内部推理原文")
    if "regulatory_determination" in issues:
        raise ValueError("模型输出包含未经核验的确定性监管结论，需人工复核")
    if "unmeasured_quantity" in issues:
        raise ValueError("模型输出包含未经测量工具验证的面积或体积，停止自动采纳")
    if "hazard_or_violation_assertion" in issues:
        raise ValueError("模型输出包含未经核验的危险属性、违法或污染认定，需人工复核")


def _sanitize_text(text: str, removed: list[str]) -> str:
    if not claim_issues(text):
        return text
    text = re.sub(r"<(thinking|think|analysis)\b[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = _REASONING_TAG.sub("", text)
    # Remove only the offending sentences; keep the rest of the answer.
    kept = []
    for part in re.split(r"(?<=[。；;！!？?\n])", text):
        if part and claim_issues(part):
            removed.append(part.strip()[:200])
            continue
        kept.append(part)
    return "".join(kept).strip()


def sanitize_claims(value):
    """Return (clean_value, removed_sentences) for a str / list / dict model output.

    A hit drops only the offending sentence instead of discarding the whole
    model result; removed text is returned for the audit record.
    """
    removed: list[str] = []

    def walk(item):
        if isinstance(item, str):
            return _sanitize_text(item, removed)
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item
    return walk(value), removed
