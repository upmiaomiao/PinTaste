"""Deterministic evidence fusion: no invented violation probabilities."""
from datetime import date, datetime, timezone
from .schemas import TaskKind
from .strategy import evidence_family, temporal_readiness
from .presentation import same_point_eligible
from .evidence import candidate_presence_evidence, candidate_presence_judgment


def requested_period_evidence(task, assets):
    """A single-year question needs one usable dated scene, not two phases.

    This gate concerns the requested period only. Spatial/quality suitability
    remains separate; image clues outside the period are retained conditionally.
    """
    from .optical_time import optical_time_intent
    intent = optical_time_intent(task)
    start = date.fromisoformat(intent['requested_start']) if intent['requested_start'] else None
    end = date.fromisoformat(intent['requested_end']) if intent['requested_end'] else None
    by_id = {asset['id']: asset for asset in assets}
    rows = []
    for asset in assets:
        original = asset
        seen = set()
        while original.get('derived_from_asset_id') in by_id and original['id'] not in seen:
            seen.add(original['id'])
            original = by_id[original['derived_from_asset_id']]
        meta = original.get('metadata') or {}
        api = original.get('data_api_metadata') or {}
        flag = (api.get('temporal_evidence') or {}).get('actual_capture_date_verified')
        scope = api.get('acquisition_time_scope') or api.get('actual_date_scope') or meta.get('metadata_scope')
        verified = flag is True or (flag is not False and scope in {'whole_scene', 'full_scene', 'whole_image', 'full_footprint', 'entire_image'}
                                    and api.get('capture_date_verified') is True)
        if api.get('provider') != 'omap_unified' and flag is not False:
            # Existing server-side registration contracts predate the unified
            # provider's explicit date flag. Preserve them without trusting an
            # uploaded filename or a user-declared metadata_scope by itself.
            provider_scope = (api.get('point_acquisition_metadata') or {}).get('scope',
                api.get('metadata_scope', api.get('acquisition_time_scope')))
            registered_whole_scene = (bool(api) and meta.get('metadata_scope') in {'whole_scene', 'full_scene'}
                and provider_scope in {'whole_scene', 'full_scene'}
                and bool(api.get('acquisition_time')) and bool(meta.get('acquisition_time'))
                and bool(original.get('requested_view')) and bool(original.get('acquisition_phase')))
            from .intent import SCIENCE_SOURCES
            registered_native_scene = (meta.get('metadata_scope') == 'scientific_native_window_display'
                and original.get('science_preview') is True and bool(original.get('science_artifact'))
                and bool(original.get('scene_id')) and original.get('acquisition_phase') == 'adaptive_science'
                and meta.get('source') in SCIENCE_SOURCES and meta.get('modality') in {'multispectral', 'sar', 'hyperspectral'}
                and bool(meta.get('acquisition_time')))
            verified = verified or registered_whole_scene or registered_native_scene
        values = api.get('actual_dates') or ([api['actual_date']] if api.get('actual_date') else [])
        if not values and meta.get('acquisition_time'):
            values = [meta['acquisition_time']]
        dates = []
        for value in values:
            try:
                dates.append(date.fromisoformat(str(value)[:10]))
            except (TypeError, ValueError):
                verified = False
        verified = verified and bool(dates)
        within = bool(dates) and all((not start or stamp >= start) and (not end or stamp <= end) for stamp in dates)
        rows.append({'asset_id': asset['id'], 'actual_dates_verified': verified,
                     'reported_dates': [stamp.isoformat() for stamp in dates],
                     'in_requested_period': within if verified else None,
                     'eligible': bool(verified and within),
                     'reason': None if verified and within else 'actual_date_outside_requested_period' if verified else 'actual_capture_date_unverified'})
    qualified = [row['asset_id'] for row in rows if row['eligible']]
    return {'required': intent['constrained'], 'ready': not intent['constrained'] or bool(qualified),
            'minimum_qualified_scenes': 1, 'target_time': intent, 'assets': rows,
            'qualified_asset_ids': qualified,
            'date_unknown_asset_ids': [row['asset_id'] for row in rows if not row['actual_dates_verified']],
            'outside_requested_period_asset_ids': [row['asset_id'] for row in rows if row['actual_dates_verified'] and not row['in_requested_period']],
            'edition_is_capture_date': False,
            'reason': '请求时段内实际拍摄日期已核实；位置与质量仍单独核验' if qualified else
                      '实际拍摄日期未核实或不在请求时段，不能据此确认或排除该年份疑点'}


def _independent_groups(rows, by_id):
    """Identical pixels and locally corrected derivatives never add sources."""
    groups, fingerprints = set(), set()
    for row in rows:
        asset = by_id[row['asset_id']]
        if asset.get('derived_from_asset_id'):
            continue
        metadata = asset.get('data_api_metadata') or {}
        audit = metadata.get('spatial_registration') or {}
        hashes = {value for value in (asset.get('sha256'), metadata.get('source_original_sha256'), audit.get('source_sha256')) if value}
        family = evidence_family(asset['metadata']['source'])
        if hashes & fingerprints:
            continue
        fingerprints.update(hashes)
        if family: groups.add(family)
    return groups


def _time_scope(task, assets, by_id):
    temporal_kinds = {TaskKind.CHANGE, TaskKind.CLEANUP, TaskKind.RESIDUAL, TaskKind.RECURRENCE, TaskKind.TRACKING}
    enabled = bool(temporal_kinds.intersection(task.task_types) and
                   any(a.get('metadata', {}).get('role') == 'baseline' for a in assets))
    if not enabled:
        return False, set(), set(by_id), set()

    def parent_metadata(asset):
        seen = set()
        while asset.get('derived_from_asset_id') in by_id and asset['id'] not in seen:
            seen.add(asset['id'])
            asset = by_id[asset['derived_from_asset_id']]
        return asset.get('metadata', {})

    def acquired(metadata):
        try:
            value = datetime.fromisoformat(str(metadata.get('acquisition_time')).replace('Z', '+00:00'))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None

    metadata = {a['id']: parent_metadata(a) for a in assets}
    baseline_times = [acquired(m) for m in metadata.values() if m.get('role') == 'baseline' and acquired(m)]
    latest_baseline = max(baseline_times) if baseline_times else None
    historical, later, unassigned = set(), set(), set()
    for identity, meta in metadata.items():
        stamp = acquired(meta)
        if meta.get('role') == 'baseline':
            historical.add(identity)
        elif meta.get('role') == 'current':
            # A declared later role can describe an undated appearance, but a
            # contradictory actual date cannot establish the later phase.
            (unassigned if stamp and latest_baseline and stamp <= latest_baseline else later).add(identity)
        elif stamp and latest_baseline:
            (later if stamp > latest_baseline else historical).add(identity)
        else:
            unassigned.add(identity)
    return True, historical, later, unassigned


def fuse_findings(task, assets: list[dict], assessments: list[dict], strategy: dict, visual_review: dict | None = None) -> dict:
    by_id = {a["id"]: a for a in assets}
    period = requested_period_evidence(task, assets)
    presence = candidate_presence_evidence(assets, assessments, requested_period=period, visual_review=visual_review, task=task)
    effective = {row['asset_id']: row['effective_decision'] for row in presence['assets']}
    assessments = [{**row, 'decision': effective.get(row.get('asset_id'), row.get('decision', 'unknown'))} for row in assessments]
    spatially_excluded = [r['asset_id'] for r in assessments if r.get('asset_id') in by_id and not same_point_eligible(by_id[r['asset_id']])]
    valid = [r for r in assessments if r.get("quality_passed") and r.get("asset_id") in by_id and r['asset_id'] not in spatially_excluded]
    conditional_positives, conditional_negatives = [], []
    if period['required']:
        eligible = set(period['qualified_asset_ids'])
        conditional_positives = [row for row in valid if row['asset_id'] not in eligible and row.get('decision') == 'positive']
        conditional_negatives = [row for row in valid if row['asset_id'] not in eligible and row.get('decision') == 'negative']
        valid = [row for row in valid if row['asset_id'] in eligible]
    phased, historical_ids, later_ids, unassigned_ids = _time_scope(task, assets, by_id)
    historical_positives = [r for r in valid if r['asset_id'] in historical_ids and r.get('decision') == 'positive']
    historical_negatives = [r for r in valid if r['asset_id'] in historical_ids and r.get('decision') == 'negative']
    positives = [r for r in valid if r.get("decision") == "positive" and r['asset_id'] in later_ids]
    negatives = [r for r in valid if r.get("decision") == "negative" and r.get("target_relevant") and r['asset_id'] in later_ids and not by_id[r["asset_id"]].get("derived_from_asset_id")]
    unknown = [r for r in assessments if r.get("decision") == "unknown" or not r.get("quality_passed")]
    blocking_unknown = [r for r in unknown if r.get("target_relevant") and (not phased or r.get('asset_id') in later_ids | unassigned_ids)]
    blocking_unknown += [r for r in valid if phased and r['asset_id'] in unassigned_ids and r.get('target_relevant')
                         and r.get('decision') == 'positive']
    blocking_unknown += conditional_positives
    positive_families = _independent_groups(positives, by_id)
    negative_families = _independent_groups(negatives, by_id)
    temporal_strategy = ({**strategy, 'requires_temporal_evidence': True,
                          'required_distinct_dates': max(2, strategy['required_distinct_dates'])} if phased else strategy)
    temporal = temporal_readiness(task, assets, temporal_strategy)
    temporal['requested_period'] = period
    if period['required']:
        usable_dated = set(period['qualified_asset_ids']) & {row['asset_id'] for row in valid}
        temporal.update(required=True, ready=temporal['ready'] and bool(usable_dated),
                        required_distinct_dates=max(1, temporal['required_distinct_dates']),
                        requested_period_evidence_satisfied=bool(usable_dated))
        if not temporal_strategy['requires_temporal_evidence']:
            temporal['observed_distinct_dates'] = len({stamp for row in period['assets'] if row['asset_id'] in usable_dated for stamp in row['reported_dates']})
        temporal['date_unknown_asset_ids'] = list(dict.fromkeys([*temporal['date_unknown_asset_ids'], *period['date_unknown_asset_ids']]))
        if not usable_dated:
            temporal['reason'] = period['reason']
        elif not temporal_strategy['requires_temporal_evidence']:
            temporal['reason'] = '已有请求时段内实际日期核实的相关影像；单年状态核查不要求两期比较'
    if phased:
        phase_assets = [a for a in assets if a['id'] in historical_ids | later_ids and not a.get('derived_from_asset_id')
                        and a.get('metadata', {}).get('role') not in {'full_scene', 'local_context'}]
        phase_dates_verified = bool(historical_ids and later_ids) and all(a.get('metadata', {}).get('acquisition_time') for a in phase_assets)
        temporal['phase_dates_verified'] = bool(phase_dates_verified)
        if not phase_dates_verified:
            temporal.update(ready=False, reason='缺少基准或后期的实际拍摄日期，不能确认清理完成、变化归因或复发时序')
    visual_review=visual_review or {}
    positive_ids={r["asset_id"] for r in positives}
    reviewed_positive_ids=positive_ids & set(visual_review.get("evidence_asset_ids",[]))
    reviewed_positive_families=_independent_groups([row for row in positives if row['asset_id'] in reviewed_positive_ids],by_id)
    unreviewed_ids=set(visual_review.get('additional_assets_not_in_this_review',[]))
    new_unreviewed_conflicts=[row['asset_id'] for row in assessments if row.get('asset_id') in unreviewed_ids
                             and (not phased or row.get('asset_id') in later_ids)
                             and (row.get('target_relevant') or row.get('candidate_context_for_asset_id'))
                             and row.get('decision') in {'negative','unknown'}]
    substantive_review=(visual_review.get("status")=="completed" and visual_review.get("relationship")=="supports_candidate"
                        and visual_review.get("same_target_supported") is True
                        and visual_review.get("alternative_explanations_reviewed") is True
                        and len(reviewed_positive_families)>=2)
    # Positive clues survive a conflicting negative image. More source names,
    # crops and zoom levels do not by themselves establish corroboration.
    # A clear same-image reviewer confirmation on a quality, localized image is
    # strong single-source support; it is the realistic path to "high" when
    # only one provider covers the point.
    same_image_supported = [row for row in positives
                            if (row.get('optical_false_positive_review') or {}).get('status') == 'completed'
                            and (row['optical_false_positive_review'].get('supported_candidate_count') or 0) > 0
                            and row['optical_false_positive_review'].get('image_quality_sufficient') is True
                            and not row.get('quality_issues')]
    multi_source_high = len(positive_families) >= 2 and substantive_review
    single_source_high = bool(same_image_supported) and (substantive_review or len(positive_families) >= 2
                                                          or visual_review.get('status') != 'completed')
    if positives:
        # Unregistered comparisons stay qualitative and cannot reach "high".
        temporal_strong = temporal["ready"] and (not temporal["required"] or temporal.get("registration_verified") is True)
        level = "high" if (multi_source_high or single_source_high) and temporal_strong and not new_unreviewed_conflicts else "medium"
        rationale = "存在可定位的疑似堆存线索；其他影像未发现不能抵消该线索。"
        if new_unreviewed_conflicts:
            rationale="正向疑点保留；补充的相关影像出现未检出或无法判定，尚未纳入视觉交叉复核，暂维持中等筛查优先级。"
        if level == "high":
            rationale = ("两个独立来源保留正向线索，视觉交叉复核支持同一目标并审查主要混淆解释；仍为待核查筛查判断。" if multi_source_high else
                         "见微识别的疑似堆存经独立同图复核确认可见特征支持，影像质量合格；仍为待核查筛查判断。")
    elif ((presence['localized_pending_asset_ids'] or presence['nominal_location_pending_asset_ids'])
          and not period['required'] and not phased):
        # Retained, not-yet-excluded candidates at the task location are at
        # least "medium": an unresolved reviewer disagreement or a missing
        # control-point audit is not evidence of absence.
        level = "medium"
        rationale = "见微识别的疑似堆存线索尚未被有效排除，保留为中等筛查优先级；复核分歧或位置核验缺口需人工确认。"
        if not presence['localized_pending_asset_ids']:
            rationale += " 影像按任务坐标获取且实际范围覆盖任务点，但尚未完成控制点位置核验，具体位置以现场核对为准。"
    elif negatives and not blocking_unknown and temporal['ready'] and presence['negative_task_scope_complete']:
        level = 'low'
        rationale = '本轮相关影像的可判读范围已完成有效核查，未保留尚未排除的候选；仅限已审查影像的范围与时点，不证明现实中没有固废。'
    else:
        level = "insufficient"
        rationale = "缺少足以回答本任务的合格相关证据，或存在无法判定、位置未核验、时相不足；空候选不能解释为低可能性。"
    labels = {"high": "疑似倾倒/堆存可能性高", "medium": "疑似倾倒/堆存可能性中", "low": "本次可见疑似倾倒/堆存可能性低", "insufficient": "证据不足，暂不能判断疑似倾倒/堆存可能性"}
    if period['required'] and not temporal['ready']:
        rationale += ' ' + period['reason'] + '。'
        if conditional_positives:
            rationale += ' 图内正向线索单独保留为待定时相线索，不计为请求年份的正向证据，也不被其他影像未检出抹去。'
            if level == 'insufficient':
                labels['insufficient'] = '图内保留疑似堆存线索，但其实际日期不满足请求时段，尚不能判断该年份状态'
    if phased:
        rationale += ' 历史基准线索单独保留，不作为后期正向投票；后期未检出也不抹去历史线索。'
        if level == 'insufficient' and historical_positives:
            labels['insufficient'] = '历史影像存在疑似堆存线索，后期状态与清理或复发情况尚不能确定'
    if presence['has_pending_candidates']:
        pending_ids = set(presence['pending_asset_ids'])
        historical_only = phased and bool(pending_ids) and pending_ids <= historical_ids
        labels[level] = candidate_presence_judgment(presence, historical_only=historical_only)
        rationale += ' 尚未被有效同图复核排除的候选持续保留；拍摄日期不符、日期未知或其他影像未检出均不删除这些图内线索。'
        if presence['image_only_pending_asset_ids']:
            rationale += ' 部分线索仅定位在图内，与任务地理点位的对应仍需核验。'
        if presence['priority_factors']:
            rationale += ' 已记录候选与水体可见邻接，宜优先核查物料、排水和岸边防护；未测量距离，也未确认污染。'
    reviewed_exclusions=[{'asset_id':r['asset_id'],'excluded_candidate_count':r['optical_false_positive_review']['excluded_candidate_count'],
                         'summary':r['optical_false_positive_review']['summary']}
                        for r in assessments if r.get('optical_false_positive_review',{}).get('status')=='completed'
                        and r['optical_false_positive_review'].get('excluded_candidate_count')]
    return {"level": level, "judgment": labels[level], "basis": rationale,
            'candidate_presence': presence,
            'reviewed_false_positive_exclusions':reviewed_exclusions,
            "positive_asset_ids": [r["asset_id"] for r in positives], "negative_asset_ids": [r["asset_id"] for r in negatives],
            "unknown_asset_ids": [r["asset_id"] for r in unknown], "positive_independent_groups": sorted(positive_families),
            "blocking_unknown_asset_ids": [r["asset_id"] for r in blocking_unknown],
            "negative_independent_groups": sorted(negative_families), "conflict_present": bool(positives and negatives),
            'historical_positive_asset_ids': [r['asset_id'] for r in historical_positives],
            'historical_negative_asset_ids': [r['asset_id'] for r in historical_negatives],
            'temporally_unassigned_asset_ids': [a['id'] for a in assets if a['id'] in unassigned_ids],
            'historical_current_difference': bool(historical_positives and negatives or historical_negatives and positives),
            'temporal_scope': {'applicable': phased, 'historical_asset_ids': [a['id'] for a in assets if a['id'] in historical_ids],
                               'later_asset_ids': [a['id'] for a in assets if a['id'] in later_ids] if phased else [],
                               'basis': '声明的基准/后期角色或可比较的实际日期；角色不能代替精确拍摄时点' if phased else '本轮不按基准角色分期'},
            "substantive_visual_review_supported":substantive_review,
            'new_unreviewed_conflict_asset_ids':new_unreviewed_conflicts,
            'spatially_excluded_asset_ids':spatially_excluded,
            'conditional_positive_asset_ids': [row['asset_id'] for row in conditional_positives],
            'conditional_negative_asset_ids': [row['asset_id'] for row in conditional_negatives],
            'requested_period_evidence': period,
            "temporal_evidence": temporal, "task_evidence_sufficient": temporal["ready"] and level != "insufficient",
            "probability": None, "calibrated_probability_available": False,
            "analysis_summary": [rationale, temporal["reason"]] if temporal["required"] else [rationale],
            "boundary": "可能性等级为筛查优先级，不是违法概率、危险属性或现场事实认定"}
