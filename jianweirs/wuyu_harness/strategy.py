"""Task-specific evidence contracts and bounded follow-up rules."""
import re
from .schemas import TASK_LABELS, TaskKind, TaskRequest
from .task_contracts import task_contracts

TEMPORAL_TASKS = {TaskKind.CHANGE, TaskKind.CLEANUP, TaskKind.RECURRENCE, TaskKind.TRACKING}
CURRENT_OPTICAL_PRIORITY = ('tianditu', 'arcgis', 'bing', 'mapquest', 'amap', 'tencent', 'gaofen2026', 'google', 'mapbox')
_PROFILES = {
    TaskKind.DISCOVERY: ("先看高分影像发现疑点；有覆盖或混淆缺口时再比较其他来源并排查正常堆场", ["full_scene", "target_crop"], ["optical", "sar"]),
    TaskKind.CLASSIFY: ("核对局部纹理与场地用途，类别只保留候选", ["target_crop", "local_context"], ["optical", "multispectral", "hyperspectral"]),
    TaskKind.EXTENT: ("核对堆体可见边界；未经地理测量不输出面积", ["target_crop"], ["optical", "sar"]),
    TaskKind.CHANGE: ("比较指定前后时相且验证配准；当前底图不能充当历史时相", ["target_crop"], ["multispectral", "sar", "optical"]),
    TaskKind.CLEANUP: ("比较整改前后范围并检查残留，区分遮挡与清理", ["target_crop", "local_context"], ["optical", "multispectral", "sar"]),
    TaskKind.RESIDUAL: ("检查清理区域内残留小目标及可能遮挡", ["target_crop"], ["optical", "multispectral"]),
    TaskKind.RECURRENCE: ("需要清理前、清理后、复现至少三个实际时相", ["target_crop", "local_context"], ["optical", "multispectral", "sar"]),
    TaskKind.TRACKING: ("保留历次点位身份与实际成像日期，持续建立证据序列", ["target_crop"], ["multispectral", "sar", "optical"]),
    TaskKind.ENVIRONMENT: ("查看地表、水系和土地利用线索；风险需源—路径—受体资料", ["full_scene", "local_context"], ["optical", "multispectral", "sar"]),
    TaskKind.RELATIONSHIP: ("核对场地、道路、水体与居民区可见关系，距离另需GIS", ["local_context", "full_scene"], ["optical", "sar"]),
}


def source_family(source: str) -> str | None:
    """Conservative independence groups; rebrands and unknown uploads do not count twice."""
    if not isinstance(source,str):return None
    source=source.lower()
    if source in {"bing", "mapquest"}:
        return "bing_mosaic"
    if source in {"arcgis", "esri", "esri_world_imagery", "wayback"}:
        return "esri_mosaic"
    # Tencent/AMap/Google mosaics can reuse providers. Different provider names
    # alone do not verify independent acquisitions or dates.
    return {"sentinel2": "sentinel2", "landsat": "landsat", "sentinel1-rtc": "sentinel1"}.get(source)


EVIDENCE_FAMILIES = {"tencent": "cn_commercial_mosaic", "amap": "cn_commercial_mosaic", "mapbox": "mapbox_mosaic",
                     "tianditu": "tianditu", "google": "google_history", "history246": "google_history",
                     "century": "century_space", "jilin2024": "jilin1", "siwei2025": "siwei"}


def evidence_family(source: str) -> str | None:
    """Supplier groups for corroboration grading only (never for source selection).

    Products with their own acquisition chain (Tianditu, Google history,
    Century Space, Jilin-1, Siwei) count separately; Tencent/AMap resell
    overlapping mosaics and count once. The 2026 aggregate and unknown uploads
    count for nothing because their underlying supplier is not declared.
    """
    family = source_family(source)
    if family or not isinstance(source, str):
        return family
    return EVIDENCE_FAMILIES.get(source.lower())


def optical_provider_group(source: str) -> str:
    """Acquisition diversity, not independently verified satellites or dates."""
    return source_family(source) or 'provider:' + source


def optical_source_allowed(source: str, contract: dict) -> bool:
    """Apply the same current-user source boundary at every acquisition choice."""
    group = optical_provider_group(source)
    if group in contract.get('excluded_provider_groups', ()):
        return False
    allowed_sources = contract.get('allowed_sources')
    if allowed_sources is not None and source not in allowed_sources:
        return False
    allowed = contract.get('allowed_provider_groups')
    return allowed is None or group in allowed


def optical_source_contract(task: TaskRequest) -> dict:
    from .intent import named_research_sources, SCIENCE_SOURCES
    aliases={'arcgis':r'Esri|ArcGIS|esri_world_imagery', 'google':r'Google|谷歌',
             'bing':r'Bing|必应', 'tencent':r'Tencent|腾讯', 'amap':r'AMap|高德',
             'mapquest':r'MapQuest', 'mapbox':r'Mapbox', 'tianditu':r'Tianditu|天地图',
             'gaofen2026':r'gaofen2026|2026(?:组)?高清', 'century':r'century|世纪空间|世纪卫星',
             'jilin2024':r'jilin2024|吉林一号(?:2024)?', 'siwei2025':r'siwei2025|四维(?:地球)?\s*2025',
             'history246':r'history246|246历史|谷歌(?:历史|时序)',
             'siwei_daily':r'siwei_daily|四维每日', 'siwei':r'Siwei(?!_daily)|四维(?!每日)'}
    source_name=r'(?<![a-z0-9])(?:'+'|'.join(aliases.values())+r')(?![a-z0-9])'
    source_list=source_name+r'(?:\s*(?:、|和|与|及|以及|/|&|\+|,|，|and|or)\s*'+source_name+r')*'
    verb=r'使用|调用|调取|获取|请求|下载|查看|访问|用|调|取|查'
    ban=(r'(?:不用|禁用|禁止(?:'+verb+r')?|不需要(?:'+verb+r')?|无需(?:'+verb+r')?|'
         r'不要(?:再)?(?:'+verb+r')?|别(?:再)?(?:'+verb+r')?|暂不(?:'+verb+r')?|不(?:再)?(?:'+verb+r')|do\s+not\s+(?:use|request|fetch|download))\s*')
    excluded=set()
    for match in re.finditer(ban+r'(?P<sources>'+source_list+r')',task.objective,re.I):
        for source, pattern in aliases.items():
            if re.search(r'(?<![a-z0-9])(?:'+pattern+r')(?![a-z0-9])',match['sources'],re.I):
                excluded.add(source)
    for match in re.finditer(r'(?P<sources>'+source_list+r')\s*(?:都|均)?(?:不用|不使用|不调用|不下载|不要调用|不要使用)',task.objective,re.I):
        for source, pattern in aliases.items():
            if re.search(r'(?<![a-z0-9])(?:'+pattern+r')(?![a-z0-9])',match['sources'],re.I):
                excluded.add(source)
    positive_clauses=[];unconditional_clauses=[]
    conditional=r'按需|必要时|需要时|视情况|决定是否|要不要|如果|若|不清[^，。]{0,20}再'
    for sentence in re.split(r'[。；;！？!?\n]',task.objective):
        conditional_prefix=False
        for clause in re.split(r'[，,]|但是|不过|而是',sentence):
            if re.search(conditional,clause):
                conditional_prefix=True
                continue
            if not conditional_prefix:
                unconditional_clauses.append(clause)
            if not conditional_prefix and not re.search(r'不用|不要|无需|不需要|不必|暂不|别用',clause):
                positive_clauses.append(clause)
            if named_research_sources(clause) or re.search(r'高分|图源|底图|影像|可见光',clause):
                conditional_prefix=False
    names = [source for clause in positive_clauses for source in named_research_sources(clause) if source not in SCIENCE_SOURCES]
    for clause in positive_clauses:
        if re.search(r'(?<![a-z0-9])MapQuest(?![a-z0-9])', clause, re.I):
            names.append('mapquest')
    excluded_groups={optical_provider_group(source) for source in excluded}
    names = [source for source in dict.fromkeys(names) if optical_provider_group(source) not in excluded_groups]
    # Only a current, unconditional acquisition restriction closes the fallback
    # list. Mentioning a provider, or an optional "if needed, only ...", does not.
    exclusive_names=[]
    exclusive=False
    only_prefix=(r'(?:(?:仅|只)(?:能)?(?:'+verb+r')|仅限(?:于)?|限定(?:为|在)|'
                 r'(?:use|request|fetch|download)\s+only|only\s+(?:use|request|fetch|download))\s*')
    for clause in positive_clauses:
        for match in re.finditer(only_prefix+r'(?P<sources>'+source_list+r')',clause,re.I):
            exclusive=True
            exclusive_names.extend(source for source,pattern in aliases.items()
                if re.search(r'(?<![a-z0-9])(?:'+pattern+r')(?![a-z0-9])',match['sources'],re.I))
    other_sources=(r'(?:其他|其它|其余)(?:的)?\s*'
                   r'(?:(?:高分(?:辨率)?|光学|卫星)\s*)*'
                   r'(?:图源|来源|底图|影像源|提供方|影像)')
    no_others=any(re.search(ban+other_sources,clause,re.I) or
                 re.search(other_sources+r'\s*(?:都|均)?(?:不要|不用|不使用|不调用|不要调用|不要使用)',clause,re.I)
                 for clause in unconditional_clauses)
    if no_others and names:
        exclusive=True
        exclusive_names.extend(names)
    allowed_sources=(sorted(source for source in set(exclusive_names)
        if optical_provider_group(source) not in excluded_groups) if exclusive else None)
    selected = [s for s in task.selected_sources if s not in SCIENCE_SOURCES]
    if selected:
        # The current turn's structured user choice supersedes earlier prose.
        names = list(dict.fromkeys(selected))
        allowed_sources = sorted(names)
        excluded = set()
        excluded_groups = set()
        exclusive = True
    allowed_groups=(sorted({optical_provider_group(source) for source in allowed_sources})
                    if allowed_sources is not None else None)
    if exclusive:
        names=[source for source in names if optical_provider_group(source) in allowed_groups]
    named_groups = {optical_provider_group(source) for source in names}
    multiple = False
    for clause in positive_clauses:
        if re.search(r'时相|月份|日期|前中后|前后|历史',clause) and not re.search(r'来源|图源|多源|多星|提供方',clause):
            continue  # Multiple dates from one provider are not multiple providers.
        if re.search(r'多个|多种|多家|多源|多星|两[个种家]|[2-9]\s*[个种家]|不同', clause) and re.search(r'高分|图源|底图|影像|可见光', clause):
            multiple = True
    explicit = multiple or len(named_groups) >= 2
    preferred = list(dict.fromkeys([*names, *([task.optical_source] if task.optical_source else [])]))
    preferred = [source for source in preferred if optical_provider_group(source) not in excluded_groups
                 and (allowed_sources is None or source in allowed_sources)]
    return {'explicit_multiple_providers': explicit, 'minimum_provider_groups': 2 if explicit else 1,
            'preferred_sources': preferred,
            'required_sources': names if len(named_groups) >= 2 else [],
            'excluded_sources': sorted(excluded), 'excluded_provider_groups': sorted(excluded_groups),
            'allowed_sources': allowed_sources, 'allowed_provider_groups': allowed_groups,
            'counting_scope': 'distinct_basemap_provider_groups_not_independent_satellites_or_acquisition_dates',
            'same_point_verification_separate': True}


def optical_source_status(task: TaskRequest, assets: list[dict]) -> dict:
    contract = optical_source_contract(task)
    sources = {asset.get('metadata', {}).get('source') for asset in assets
               if asset.get('metadata', {}).get('modality') == 'optical'
               and not asset.get('derived_from_asset_id') and not asset.get('science_preview')}
    sources.discard(None)
    sources.discard('user_upload')
    sources.discard('user_workbook_embedded')
    sources={source for source in sources if optical_source_allowed(source,contract)}
    groups = {optical_provider_group(source) for source in sources}
    missing = [source for source in contract['required_sources'] if optical_provider_group(source) not in groups]
    return {**contract, 'acquired_provider_count': len(groups), 'acquired_provider_groups': sorted(groups),
            'acquired_sources': sorted(sources), 'missing_requested_sources': missing,
            'provider_count_met': len(groups) >= contract['minimum_provider_groups'] and not missing}


def task_strategy(task: TaskRequest, max_assets: int = 8) -> dict:
    temporal = bool(set(task.task_types) & TEMPORAL_TASKS)
    contracts = [{**task_contracts([kind])[0], "task_type": kind.value, "label": TASK_LABELS[kind], "question": _PROFILES[kind][0],
                  "initial_roles": _PROFILES[kind][1], "preferred_modalities": _PROFILES[kind][2]} for kind in task.task_types]
    roles = list(dict.fromkeys(role for item in contracts for role in item["initial_roles"]))[:2]
    groups = list(dict.fromkeys(["optical",*(group for item in contracts for group in item["preferred_modalities"])]))
    optical_contract = optical_source_contract(task)
    return {
        "strategy_version": "adaptive-evidence-v3", "contracts": contracts, "initial_roles": ["target_crop"],"followup_roles":roles,
        "preferred_modalities": groups, "requires_temporal_evidence": temporal,
        "required_distinct_dates": 3 if TaskKind.RECURRENCE in task.task_types else 2 if temporal else 0,
        "temporal_requirement": "实际拍摄时相、同一区域及配准依据" if temporal else "本次可见场景",
        "initial_optical_priority": list(CURRENT_OPTICAL_PRIORITY),
        "optical_source_contract": optical_contract,
        "baseline": {"target_source_groups":optical_contract['minimum_provider_groups'],"same_requested_coverage":True,"per_source_initial_views":1,
                     "acquisition_before_recognition":False,"preferred_science_rgb":[],"max_initial_optical_attempts":6,
                     "max_initial_optical_seconds":180,
                     "meaning":("先判读一份高分影像，再完成用户明确的同点多来源或指定来源对照；不同底图提供方不代表独立卫星/时相，取得失败须列具体缺口"
                                if optical_contract['explicit_multiple_providers'] else
                                "先看一份高分影像；按实际疑点、可见性、配准或任务时相缺口补充其他图源，单份证据足够回答即可停止")},
        "science_budget": {"max_catalog_queries":2,"catalog_limit":12,"max_quality_probes":6,
                           "max_quality_probes_per_period":3,"max_subset_attempts":2,"max_scenes_per_source":2,
                           "analyst_role":"science_analyst","requires_evidence_trigger":True,
                           "fallback_rule":"先对最多6个不同场景做点位SCL质量检查；只有通过筛查才读取科学波段，最多两个完整窗口；目录和有限候选均不代表全历史"},
        "research_budget": {"max_catalog_queries":48,"max_periods":24,"catalog_limit":50,
                            "max_quality_probes":24,"max_windows":min(8,max(0,max_assets-len(task.asset_ids))),
                            "max_replan_rounds":4,"analyst_role":"science_analyst",
                            "catalog_requires_image_slot":False,"optical_registration_required":False,
                            "budget_scope":"resource_circuit_breaker_not_completed_action_types",
                            "completeness_rule":"按实际来源与时间分片记录检索完整性；达到资源边界不代表完整时序或无影像"},
        "adaptive_review": {"enabled": task.adaptive_review, "trigger": "per_image_quality_coverage_edge_or_visual_request",
                            "max_additional_source_attempts": 5 if optical_contract['explicit_multiple_providers'] else 2,
                            "max_total_optical_provider_attempts": 6,
                            "max_additional_images": 2,
                            "max_local_crops": 4, "max_main_visual_reviews": 2,"max_rounds":4,
                            "stop_rule":"已回答目标且无待解关键冲突即停止；无适用来源、预算耗尽或质量不足则保留未知并解释停止",
                            "independence_rule": "Bing与Mapquest同组；同图裁切与不同尺度不增加独立来源数"},
        "stages": [{"id": "task_analysis", "label": "任务分析与大纲"},
                   {"id": "source_selection", "label": "按任务选择多星数据"},
                   {"id": "recognition_review", "label": "见微识别与追加核查"},
                   {"id": "synthesis", "label": "证据融合与资料综合"}],
        "boundaries": ["选源清单不等于已下载数据", "低NDVI或SAR强散射不能独立识别固废", "科学影像仅交科学分析角色，不送高分识别器", "底图供应商不等于具体卫星"],
    }


def acquisition_budget(task: TaskRequest,max_assets: int) -> dict:
    """Keep uploads and budget for need-driven follow-ups, never mandatory sensors."""
    uploaded=len(task.asset_ids);available=max(0,max_assets-uploaded)
    capacity=min(1,available)
    reserve=min(2,max(0,available-capacity)) if task.adaptive_review else 0
    explicit = optical_source_contract(task)['explicit_multiple_providers']
    return {"max_assets":max_assets,"uploaded_assets":uploaded,"available_new_slots":available,
            "baseline_capacity":capacity,"reserved_followup_slots":reserve,
            "max_followup_images":min(2,max(0,available-capacity)) if task.adaptive_review else 0,
            "explanation":f"总上限{max_assets}张，上传{uploaded}张保留；先取最多{capacity}张高分影像，追加最多{reserve}张；"
                + ("本轮须完成用户明确要求的多来源对照，失败请求不占成功影像数；仍受有限图源尝试约束，未满足须保留缺口。"
                   if explicit else "按证据需要补取，不为凑传感器数量取图。")}


def temporal_readiness(task: TaskRequest, assets: list[dict], strategy: dict) -> dict:
    relevant = [a for a in assets if not a.get("derived_from_asset_id") and a.get('analysis_scope')!='background_only'
                and a.get("metadata", {}).get("role") not in {"full_scene", "local_context"}]
    # A known date in only the centre tile must not order a whole-image change.
    def date_usable(asset):
        api = asset.get('data_api_metadata') or {}
        if api.get('provider') == 'omap_unified':
            return (api.get('temporal_evidence') or {}).get('actual_capture_date_verified') is True
        return True
    dated = [a for a in relevant if date_usable(a)]
    times = {str(a["metadata"].get("acquisition_time"))[:10] for a in dated if a["metadata"].get("acquisition_time")
             and (not task.date_start or str(a["metadata"]["acquisition_time"])[:10]>=str(task.date_start))
             and (not task.date_end or str(a["metadata"]["acquisition_time"])[:10]<=str(task.date_end))}
    registered = bool(dated) and all(a["metadata"].get("registration_verified") for a in dated)
    # Verified capture dates are the hard requirement. Pixel registration only
    # lowers confidence: a qualitative per-date comparison remains possible,
    # while change areas and attribution stay disabled by the quality gates.
    enough_dates = len(times) >= strategy["required_distinct_dates"]
    ready = not strategy["requires_temporal_evidence"] or enough_dates
    if not ready:
        reason = "缺少足够的实际拍摄时相，不能判定变化/整改/复现"
    elif strategy["requires_temporal_evidence"] and not registered:
        reason = "已有足够实际拍摄时相，可逐期定性比较；未做像素级配准，位置对应以目视为准，不计算变化面积"
    else:
        reason = "时相与配准要求已声明满足，仍须人工复核"
    return {"required": strategy["requires_temporal_evidence"], "ready": ready,
            "required_distinct_dates": strategy["required_distinct_dates"], "observed_distinct_dates": len(times),
            "registration_verified": registered,
            "comparison_basis": "registered" if registered else "qualitative_visual",
            "date_unknown_asset_ids": [a['id'] for a in relevant if not a.get('metadata', {}).get('acquisition_time') or not date_usable(a)],
            "edition_is_capture_date": False,
            "reason": reason}
