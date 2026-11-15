"""下一步取证动作的决策与科学影像关键词判定（依赖 adaptive_recognition）。"""
from __future__ import annotations

import re


from .strategy import task_strategy, TEMPORAL_TASKS, optical_provider_group, optical_source_allowed, optical_source_status
from .adaptive_recognition import coverage_decisions, evidence_action_key, spatial_prerequisites


def explicit_science_request(text,modality):
    """Distinguish a requested analysis from permission to choose a sensor.

    Conditional permission stays available to evidence-driven visual review;
    mentioning a sensor does not itself justify a remote acquisition. Keep
    clauses separate so ``不用SAR，请查询多光谱`` retains its positive request.
    """
    terms={'sar':r'SAR|雷达|散射|微波',
           'multispectral':r'多光谱|光谱|NDVI|植被指数|反射率|近红外'}
    negative=r'无需|不需|不用|不必|不要|暂不|不能|不得|不应|避免|禁止|别用|不(?:使用|调用|获取|分析|补充|补取|考虑|采用|做)|\b(?:no|without)\b|\bdo\s+not\b|\bdon[’\x27]t\b'
    optional=(r'按需|必要(?:时|的话)|有必要时|需要(?:时|的话)|有需要时|视情况|视需要|酌情|可选|决定是否|选择是否|'
              r'只有[^，。；;]{0,40}才|'
              r'是否(?:需要|需用|使用|采用|调用|补充|补取)|需不需要|要不要|有无必要|有没有必要|'
              r'如(?:果)?(?:有)?(?:需要|必要)|可(?:以)?(?:考虑|选择|选用)|\b(?:if\s+needed|as\s+needed|optional|whether)\b')
    request=(r'分析|比较|对比|比对|核对|核查|查询|检索|获取|调取|下载|查看|看看|看一下|计算|'
             r'展示|提供|给出|结合|采用|使用|调用|补充|补取|查|补|需要|必须|什么样|怎样|如何|'
             r'\b(?:analy[sz]e|compare|retrieve|fetch|show|calculate|compute|check|inspect|query|use|need)\b')
    for sentence in re.split(r'[。！？!?；;\n]',text):
        conditional_prefix=False
        for clause in re.split(r'[，,]|但是|不过|然而|而是|但',sentence):
            conditional=bool(re.search(optional,clause,re.I))
            if not re.search(terms[modality],clause,re.I):
                # ``必要时，补充SAR`` is still conditional; a fresh explicit
                # request after another sentence is evaluated independently.
                conditional_prefix=conditional_prefix or conditional
                continue
            if not (conditional_prefix or conditional or re.search(negative,clause,re.I)) and re.search(request,clause,re.I):
                return True
            conditional_prefix=False
    return False


def science_forbidden(text, modality):
    terms={'sar':r'(?:SAR|雷达|散射|微波)',
           'multispectral':r'(?:多光谱|Sentinel-?2|哨兵.?2|NDVI|近红外)'}
    denial=r'(?:不用|不要|无需|不需|不使用|不调用|禁止|禁用|不能|不得|暂不|不必)'
    return bool(re.search(denial+r'(?:调用|获取|分析|使用|补充|采用|用)?\s*'+terms[modality],text,re.I))


def next_evidence_actions(task,assets,recognition,assessments,review,base_view=None,*,completed=(),available_sources=(),attempted_sources=()):
    """Explain the next minimum useful tool; never require a sensor/source quota."""
    provider_status=optical_source_status(task,assets)
    available_sources=[source for source in available_sources
                       if optical_source_allowed(source,provider_status)]
    done=set(completed);actions=[]
    def add(action,reason,asset_id=None,*,allow_completed=False,**extra):
        if action in {'multispectral','sar'} and science_forbidden(task.objective,action):return
        if action=='local_crop' and any(a['id']==asset_id and a.get('derived_from_asset_id') for a in assets):return
        if action=='local_crop' and not extra.get('pixel_window'):
            # A generic model request must resolve to an as-yet-unreviewed
            # original object, never re-crop the first E2 and starve E3.
            if any(row['action']=='local_crop' and row['asset_id']==asset_id for row in actions):return
            boxes=[obs.pixel_window for candidate in recognition.candidates for index in candidate.evidence_indices
                   for obs in [recognition.observations[index]] if obs.asset_id==asset_id and obs.pixel_window]
            box=next((box for box in boxes if evidence_action_key(action,asset_id,box) not in done),None)
            if not box:return
            extra['pixel_window']=list(box)
        key=evidence_action_key(action,asset_id,extra.get('pixel_window'))
        if (action not in done or allow_completed) and (key not in done or allow_completed) and not any(row['action_key']==key for row in actions):
            actions.append({'action':action,'reason':reason,'asset_id':asset_id,'status':'planned','action_key':key,**extra})
    visual=[asset for asset in assets if asset.get('metadata',{}).get('modality')=='optical']
    identity=visual[0]['id'] if visual else None
    objective=task.objective  # Inherited task/prompt text is evidence, not a new source request.
    substantive=(review.get('status')=='completed' and review.get('same_target_supported') is True
                 and review.get('alternative_explanations_reviewed') is True)
    spatial=spatial_prerequisites(task,assets,assessments,available_sources=available_sources,attempted_sources=attempted_sources)
    # User-explicit scientific questions remain valid even after a negative high
    # resolution image, but no implicit fixed SAR/multispectral baseline exists.
    # No optical anchor does not invalidate a separately declared WGS84 task AOI.
    # Such science remains independent context, never a repair of the optical map.
    independent_note='；仅按独立声明的WGS84任务范围取图，不采用未对齐光学图中的候选位置' if spatial['spatial_correspondence_unresolved'] else ''
    if explicit_science_request(objective,'sar') and spatial['independent_task_aoi_available']:
        add('sar','用户任务明确需要雷达散射证据，先取得真实标定数据再交科学角色分析'+independent_note,identity)
    if explicit_science_request(objective,'multispectral') and spatial['independent_task_aoi_available']:
        add('multispectral','用户任务明确需要光谱或地表背景证据，不能由底图颜色推断'+independent_note,identity)
    if provider_status['explicit_multiple_providers'] and not provider_status['provider_count_met']:
        used_groups=set(provider_status['acquired_provider_groups'])
        pending=[*provider_status['missing_requested_sources'],*available_sources]
        alternate=next((source for source in pending if source in available_sources and source not in attempted_sources
                        and optical_provider_group(source) not in used_groups),None)
        if alternate:
            add('same_coverage_alternate','用户明确要求同点多来源；当前来源数量或指定来源尚未取得，按同一请求范围补取并单独核验空间对应',
                identity,requested_source=alternate,allow_completed=True,issue='explicit_multiple_providers')
    if spatial['spatial_correspondence_unresolved'] and spatial['can_attempt_reference_recovery']:
        add('same_coverage_alternate','先修复高分影像与任务点的空间对应；'+spatial['reason'],identity,
            requested_source=spatial['alternate_candidate'] if spatial['reference_available'] else spatial['reference_candidate'],
            allow_completed=True,
            issue='spatial_reference_prerequisite')
    if set(task.task_types)&TEMPORAL_TASKS:
        if not task.date_start or not task.date_end:
            pass  # Never substitute a recent default period for missing history.
        else:
            from .strategy import temporal_readiness,task_strategy
            if spatial['independent_task_aoi_available'] and not temporal_readiness(task,assets,task_strategy(task))['ready']:
                add('timeseries','任务需要真实前后时相；当前底图不提供足够拍摄日期或配准依据',identity)
    for decision in coverage_decisions(task,assets,recognition,assessments,review,base_view):
        if spatial['spatial_correspondence_unresolved'] and not spatial['reference_available'] and decision['action'] in {'expand_context','detail_view','same_coverage_alternate'}:
            continue  # More unknown mosaics or crops cannot create a missing anchor.
        if decision['action'] in {'sar','multispectral','timeseries'} and not spatial['independent_task_aoi_available']:continue
        if substantive and decision.get('issue') in {'small_candidate_pixel_window','candidate_near_image_edge'}:
            # A small/edge pixel box alone does not force another tool once an
            # actual visual review has resolved the user's relevant ambiguity.
            continue
        if decision['action']!='none':add(decision['action'],decision['reason'],decision['asset_id'],
            **{key:decision[key] for key in ('pixel_window','candidate_index','image_only_review','issue','evidence_labels') if key in decision})
    positives=[row for row in assessments if row.get('decision')=='positive']
    if positives and not substantive:
        add('same_coverage_alternate','有可见疑点而主要混淆或同目标对应尚未解决，补充另一高分来源复核',positives[0]['asset_id'])
    for result in review.get('science_analyses',[]):
        if result.get('status')!='completed' or result.get('stop_recommendation'):continue
        for item in result.get('recommended_next_actions',[]):
            action={'high_resolution_alternate':'same_coverage_alternate'}.get(item['action'],item['action'])
            if spatial['spatial_correspondence_unresolved'] and not spatial['reference_available'] and action in {'same_coverage_alternate','expand_context','detail_view','local_crop'}:continue
            if action in {'sar','multispectral','timeseries'} and not spatial['independent_task_aoi_available']:continue
            if action not in {'stop','field_verification'}:add(action,item['reason'],(item.get('asset_ids') or [identity])[0])
    # Conditional permission becomes an actual evidence trigger only after the
    # existing object windows have been checked. It never installs a mandatory
    # multispectral/SAR phase for every task or promises small-pile materials.
    conditional_spectral = (re.search(r'多光谱|Sentinel-?2|哨兵.?2', objective, re.I)
        and re.search(r'不确定|看不清|必要时|需要时|按需|补证|辅助', objective)
        and not science_forbidden(objective,'multispectral'))
    has_reviewed_crop = any(asset.get('derived_from_asset_id') for asset in visual)
    unresolved = any((row.get('optical_false_positive_review') or {}).get('uncertain_candidate_count', 0) > 0
                     for row in assessments)
    if conditional_spectral and has_reviewed_crop and unresolved and spatial['independent_task_aoi_available'] and not any(row['action']=='local_crop' for row in actions):
        add('multispectral','高分局部复核后仍有未排除疑点；补查相近时段的植被、裸地和地表背景，不用10米像元直接认定小堆体物料'+independent_note,identity)
    if not explicit_science_request(objective,'sar') and not explicit_science_request(objective,'multispectral'):
        actions.sort(key=lambda row:0 if row.get('issue')=='uncertain_candidate_detail' else 1)
    if task.online_supplement=='uploaded_only':
        # Expert recommendations and explicit science words are not permission
        # to acquire online imagery after the user chose supplied data only.
        return [row for row in actions if row['action']=='local_crop']
    return actions


def optical_candidate_key(source,strategy,eligible_sources=()):
    """Use the declared source sequence within admitted, non-failed providers.

    A map's nominal level cap is only a bounded acquisition preference. It is
    never evidence of native sensor resolution, image quality or availability.
    """
    name=source['source'];cap=(source.get('metadata',{}).get('map_metadata') or {}).get('max_zoom')
    cap=cap if isinstance(cap,(int,float)) and not isinstance(cap,bool) and 0<cap<=30 else 0
    defaults=strategy['initial_optical_priority']
    preferred=strategy['optical_source_contract'].get('preferred_sources', [])
    return (0 if name in preferred else 1, preferred.index(name) if name in preferred else len(preferred),
        defaults.index(name) if name in defaults else len(defaults),
        0 if name in eligible_sources else 1,-cap)
