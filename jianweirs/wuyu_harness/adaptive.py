"""证据采集编排：collect_evidence 与科学研究分支。

细节在叶子模块里：见微识别 adaptive_recognition、科学影像 adaptive_science、取证决策 adaptive_actions。
"""
from __future__ import annotations

import base64
import copy
import re
import asyncio
import time
from dataclasses import replace
from datetime import datetime,timedelta,timezone


from .data import DataClientError
from .evidence import assess_quality, sanitize_claims, validate_recognition
from .models import ProviderError
from .schemas import Recognition
from .strategy import (source_family, task_strategy, acquisition_budget, TEMPORAL_TASKS,
                       optical_provider_group, optical_source_allowed, optical_source_status)
from .fusion import fuse_findings
from .views import initial_views, expanded_view, detail_view
from .adaptive_recognition import (  # noqa: F401  重新导出，保持 wuyu_harness.adaptive.<名字> 的旧用法可用
    MODEL_METADATA_RULE,
    _crop_candidate,
    _decision,
    _visual_review,
    coverage_decisions,
    evidence_action_key,
    image_parts,
    merge_recognition,
    model_asset_context,
    recognize,
    recognize_detail_batch,
    refresh_registered_assets,
    replace_asset_recognition,
    requalify_registered_assessments,
    spatial_eligible,
    spatial_prerequisites,
)
from .adaptive_science import (  # noqa: F401  重新导出，保持 wuyu_harness.adaptive.<名字> 的旧用法可用
    _analyze_science_with_events,
    _query_s2_science,
    _query_science,
    _science_window,
    acquisition_view,
    science_subset,
)
from .adaptive_actions import (  # noqa: F401  重新导出，保持 wuyu_harness.adaptive.<名字> 的旧用法可用
    explicit_science_request,
    next_evidence_actions,
    optical_candidate_key,
    science_forbidden,
)


def _usable_research_optical(asset):
    if asset.get('metadata',{}).get('modality')!='optical' or asset.get('science_preview') or asset.get('analysis_scope')=='background_only':
        return False
    if min(asset.get('width',0),asset.get('height',0))<64:return False
    # Online map imagery must have same-point support; an explicitly supplied
    # user image can still be assessed within its declared image-only context.
    return spatial_eligible(asset) or asset.get('metadata',{}).get('source')=='user_upload'


async def _research_optical_context(pipeline,run_id,task,catalog,sources,selection,assets,strategy):
    """At most two optical attempts establish morphology before archive work.

    Unknown registration cannot consume unbounded retries or block a separately
    declared WGS84 science AOI. Context attempts never count as archive replans.
    """
    result={'assets':list(assets),'recognition':Recognition(),'native':[],'assessments':[],'traces':[],'warnings':[],
        'review':{'requested':True,'max_source_attempts':2,'max_images':2,'attempts':[],
            'reused_asset_ids':[],'acquired_asset_ids':[],'analyzed_asset_ids':[],
            'catalog_queries_consumed':0,'research_replan_rounds_consumed':0}}
    record=result['review'];inspected=set();analyzed=set()
    async def inspect_usable():
        usable=[asset for asset in result['assets'] if _usable_research_optical(asset) and asset['id'] not in inspected]
        if not usable:return
        revised,native,assessments,traces=await recognize(pipeline,run_id,task,usable,
            assess_quality(task,result['assets']),phase='research_morphology_context')
        merge_recognition(result['recognition'],revised)
        result['native'].extend(native);result['assessments'].extend(assessments);result['traces'].extend(traces)
        inspected.update(asset['id'] for asset in usable)
        analyzed.update(row['asset_id'] for row in assessments if row.get('quality_passed')
            and row.get('reason') not in {'ProviderError','ValueError','recognizer_not_configured'})
        record['analyzed_asset_ids']=sorted(analyzed);record['inspected_asset_ids']=sorted(inspected)
    existing=[asset for asset in result['assets'] if _usable_research_optical(asset)]
    if existing:
        record.update(status='reused',reused_asset_ids=[asset['id'] for asset in existing],
            stop_reason='复用本轮已有合适高分上下文，未重新下载底图')
        await inspect_usable()
        return result
    if task.coordinate_system!='wgs84' or not(task.center or task.bbox):
        record.update(status='unavailable',stop_reason='缺少明确WGS84点位，未猜测形态上下文取图范围')
        return result
    lon,lat=task.center or ((task.bbox[0]+task.bbox[2])/2,(task.bbox[1]+task.bbox[3])/2)
    from .vicinity import plan_source_coverage
    # This bounded morphology image is local context at the science AOI's
    # declared centre, not a complete scan of the scientific AOI. Keep its
    # footprint consistent with each source's best declared level.
    context_task=task.model_copy(update={'bbox':None,'center':(lon,lat),
        'location_context':{},'nearby_radius_m':None,'complaint_scan':None})
    candidates=[source for source in sources if source['group']=='optical'
                and optical_source_allowed(source['source'],strategy['optical_source_contract'])]
    candidates.sort(key=lambda source:optical_candidate_key(source,strategy))
    seen_families=set();candidates_unique=[]
    for source in candidates:
        family=source_family(source['source'])
        if family and family in seen_families:continue
        if family:seen_families.add(family)
        candidates_unique.append(source)
    for source in candidates_unique[:2]:
        if len(record['acquired_asset_ids'])>=2:break
        if len(result['assets'])>=pipeline.settings.max_assets:
            record['slot_limit_reached']=True;break
        name=source['source']
        plan=plan_source_coverage(context_task,source,requested_zoom=task.optical_zoom,
                                 nearby_authorized=False,max_windows=1)
        attempt={'source':name,'group':'optical','action':'optical_context',
            'requested_bbox':plan.get('working_bbox'),'status':'running','asset_ids':[],'native_resolution_m':None,
            'zoom_decision':plan['zoom_decision'],'full_task_aoi_covered':False}
        record['attempts'].append(attempt)
        if plan['status']!='ready':
            attempt.update(status='unavailable',reason_code=plan['status'])
            pipeline.event(run_id,'research_optical_context',attempt['status'],attempt)
            continue
        view={**plan['windows'][0],'acquisition_purpose':'research_morphology_context',
              'basis':'scientific_aoi_center_source_maximum_context','full_task_aoi_covered':False}
        pipeline.event(run_id,'research_optical_context','running',attempt)
        try:
            follow,issues=await pipeline.acquire_views(run_id,task,selection,catalog,view_plan=[view],source_choices=[name],phase='research_morphology_context')
            result['warnings'].extend(issues)
            identities={asset['id'] for asset in result['assets']}
            follow=[asset for asset in follow if asset['id'] not in identities and asset.get('metadata',{}).get('modality')=='optical']
            follow=follow[:min(2-len(record['acquired_asset_ids']),max(0,pipeline.settings.max_assets-len(result['assets'])))]
            result['assets'].extend(follow);attempt.update(status='completed' if follow else 'unavailable',asset_ids=[asset['id'] for asset in follow])
            record['acquired_asset_ids'].extend(asset['id'] for asset in follow)
            if follow and hasattr(pipeline,'refresh_spatial_evidence'):
                refreshed=await pipeline.refresh_spatial_evidence(result['assets'])
                if isinstance(refreshed,list):
                    refreshed_by_id={asset['id']:asset for asset in refreshed}
                    result['assets']=[refreshed_by_id.get(asset['id'],asset) for asset in result['assets']]
            await inspect_usable()
        except (DataClientError,ValueError,OSError) as error:
            attempt.update(status='unavailable',reason_code=error.code if isinstance(error,DataClientError) else type(error).__name__)
            result['warnings'].append('形态上下文未完成，本轮继续独立科学检索：'+str(attempt['reason_code']))
        pipeline.event(run_id,'research_optical_context',attempt['status'],attempt)
        if any(_usable_research_optical(asset) for asset in result['assets']):break
    for asset in result['assets']:
        if asset['id'] in record['acquired_asset_ids'] and asset['id'] not in inspected:
            result['assessments'].append({'asset_id':asset['id'],'source':asset['metadata']['source'],
                'phase':'research_morphology_context','decision':'unknown','quality_passed':False,
                'target_relevant':False,'same_point_eligible':False,'reason':'spatial_registration_unverified',
                'analysis_summary':['高分上下文尚未通过同点核验，未送固废模型；独立WGS84科学检索继续']})
    context_ready=any(_usable_research_optical(asset) for asset in result['assets'])
    record.update(status='completed' if analyzed else 'context_ready_unanalyzed' if context_ready else 'unavailable',
        stop_reason='合适高分上下文已用于形态判读' if analyzed else
            '合适高分上下文已取得，识别未完成；继续科学研究' if context_ready else '有限上下文核验未形成同点判读，继续科学目录与时序研究')
    return result


async def _collect_research_evidence(pipeline,run_id,task,request,catalog,sources,selection,assets,
                                    strategy,outline,stages,stage,warnings,traces,*,evidence_only=False):
    """Research uses its own resource budget, never the optical repair loop."""
    from .science_research import research_science
    morphology=None
    temporal_assets,temporal_collection=[],None
    # A temporal task routed to research still needs dated high-resolution
    # periods (Century Space / Google history / annual editions) and a
    # Jianwei reading of each period; science indices alone cannot say when a
    # pile appeared. Fetch them first, inside the shared image cap.
    if (request.mode!='availability' and set(task.task_types)&TEMPORAL_TASKS and task.acquire_optical
            and task.coordinate_system=='wgs84' and (task.center or task.bbox)):
        from .temporal_optical import collect_temporal_optical
        from .views import initial_views
        temporal_views=initial_views(task,strategy)
        temporal_assets,temporal_warnings,temporal_collection=await collect_temporal_optical(
            pipeline,run_id,task,strategy,selection,catalog,temporal_views,assets)
        warnings.extend(temporal_warnings)
        assets=[*assets,*[a for a in temporal_assets if a['id'] not in {x['id'] for x in assets}]]
        if temporal_assets and hasattr(pipeline,'refresh_spatial_evidence'):
            assets=await pipeline.refresh_spatial_evidence(assets)
    selection['temporal_optical']=temporal_collection
    if not evidence_only and getattr(request,'include_optical_context',False) and request.mode!='availability':
        morphology=await _research_optical_context(pipeline,run_id,task,catalog,sources,selection,assets,strategy)
        assets=morphology['assets'];warnings.extend(morphology['warnings']);traces.extend(morphology['traces'])
        strategy['research_budget']={**strategy['research_budget'],
            'max_windows':min(8,max(0,pipeline.settings.max_assets-len(assets)))}
        outline['review_budget']=strategy['research_budget']
    # Call even when every image slot is occupied: checking a catalog does not
    # create an image and remains a useful answer in its own right.
    study=await research_science(pipeline,run_id,task,catalog,assets,request)
    warnings.extend(study.get('warnings',[]));traces.extend(study.get('traces',[]))
    science=list(study.get('scientific_evidence',[]));existing={asset['id'] for asset in assets}
    added=[]
    for asset in study.get('assets',[]):
        if asset['id'] in existing:continue
        if asset.get('metadata',{}).get('modality') not in {'multispectral','sar','hyperspectral'}:
            warnings.append('科学研究未接纳新的高分底图，已有光学影像仅作声明范围内的上下文')
            continue
        if len(assets)>=pipeline.settings.max_assets:
            warnings.append('科学图像槽位达到上限，已检索目录与未展示场景仍保留在研究记录')
            continue
        assets.append(asset);added.append(asset);existing.add(asset['id'])
    selected=list(dict.fromkeys([*request.sources,*[item['source'] for item in science if isinstance(item.get('source'),str)]]))
    by_source={source['source']:source for source in sources}
    selection.update(method='scientific_research_request',ranking=[])
    if morphology:
        for attempt in morphology['review']['attempts']:
            selection['ranking'].append({'group':'optical','source':attempt['source'],'status':attempt['status'],
                'reason':'本轮堆存与历史变化问题先检查可见形态；仅有限尝试高分上下文',
                'independence_group':source_family(attempt['source']),
                'limitations':['当前拼接底图的拍摄日期与传感器未核实，不能替代历史时相；未配准图不作同点证据']})
    for source in selected:
        records=[item for item in science if item.get('source')==source]
        group=by_source.get(source,{}).get('group') or next((item.get('group') for item in records if item.get('group')),'unknown')
        selection['ranking'].append({'group':group,'source':source,'reason':'按用户研究范围检索实际科学目录并记录数据条件',
            'independence_group':source_family(source),'status':records[-1].get('status','checked') if records else 'requested',
            'limitations':['目录覆盖、点位质量与实际取得的分析窗口分别记录，不以底图配准作为科学检索前置条件']})
    pipeline.event(run_id,'selection','completed',{'method':selection['method'],'ranking':selection['ranking'],'research_request':request.model_dump(mode='json')})
    quality=assess_quality(task,assets);pipeline.event(run_id,'quality','completed',quality)
    stage('source_selection','completed',selection=selection,science_queries=science,
        registered_asset_ids=[asset['id'] for asset in assets],acquisition_issues=list(warnings))
    if evidence_only:
        return _evidence_only_result(warnings,traces,strategy,outline,stages,catalog,selection,assets,quality,science)
    stage('recognition_review','running')
    # Availability asks what data exist; it never needs a solid-waste model.
    # Analysis may reuse declared existing optical context, without requesting
    # another map or attempting to repair its spatial registration.
    # The prompt can contain inherited reports and old screening instructions.
    # Only the current objective may request a fresh optical-context judgment;
    # a scientific-only follow-up still passes existing metadata to the analyst.
    optical_named=bool(re.search(r'高分|光学|底图|Bing|Google|Esri|ArcGIS|腾讯|高德',task.objective,re.I))
    optical_review_requested=bool(re.search(r'比较|对照|对比|对应|对[得的]?上|结合|辅助|研判|判断|核查|确认|分析',task.objective))
    optical_needed=request.mode in {'analysis','timeseries'} and optical_named and optical_review_requested
    visual=[asset for asset in assets if asset['id'] not in {a['id'] for a in added}
        and asset.get('metadata',{}).get('modality')=='optical' and not asset.get('science_preview')]
    recognition=morphology['recognition'] if morphology else Recognition()
    native=morphology['native'] if morphology else [];assessments=morphology['assessments'] if morphology else []
    if optical_needed and visual and not morphology:
        recognition,native,assessments,context_traces=await recognize(pipeline,run_id,task,visual,quality,phase='research_existing_context')
        traces.extend(context_traces)
    # Every dated high-resolution period gets its own Jianwei reading.
    reviewed={row.get('asset_id') for row in assessments}
    period_visual=[asset for asset in assets if asset['id'] in {a['id'] for a in temporal_assets}
                   and asset['id'] not in reviewed and asset.get('metadata',{}).get('modality')=='optical']
    if period_visual:
        period_recognition,period_native,period_assessments,period_traces=await recognize(
            pipeline,run_id,task,period_visual,quality,phase='temporal_period')
        merge_recognition(recognition,period_recognition)
        native=[*native,*period_native];assessments=[*assessments,*period_assessments];traces.extend(period_traces)
    analyses=[item['analysis'] for item in science if isinstance(item.get('analysis'),dict)]
    analysis_completed=any(result.get('status')=='completed' for result in analyses)
    science_pixels_analyzed=any(result.get('science_pixels_analyzed') is True for result in analyses)
    supplied_review=copy.deepcopy(study.get('review',{}))
    availability_answered=supplied_review.get('availability_answered') is True
    research_status=supplied_review.get('status') or ('completed' if analysis_completed or availability_answered else 'unavailable')
    answered=availability_answered if request.mode=='availability' else analysis_completed
    support='data_availability_answered' if request.mode=='availability' and answered else 'scientific_analysis_answered' if answered else 'scientific_research_incomplete'
    review={'triggered':bool(supplied_review.get('action_history')),'enabled':True,'requests':[],
        'local_crops':[],'main_visual_review':{'status':'not_triggered','reason':'scientific_research_uses_existing_context_only'},
        'science_analyses':analyses,'budget':strategy['research_budget'],'coverage_decisions':[],
        'action_history':[],'rounds':0,'stop_reason':'科学研究已按实际来源、时段与资源边界结束',
        'stop_reason_code':'scientific_research_finished',**supplied_review,
        'additional_asset_count':len(added),'research_mode':request.mode,'optical_registration_required':False}
    review['budget']=strategy['research_budget']
    review['science_analyses']=analyses
    if morphology:
        review['optical_context']=morphology['review']
        review['additional_asset_count']+=len(morphology['review']['acquired_asset_ids'])
    fusion=fuse_findings(task,assets,assessments,strategy,review['main_visual_review'])
    optical_same_point_ids={asset['id'] for asset in assets if asset.get('metadata',{}).get('modality')=='optical' and spatial_eligible(asset)}
    hybrid_screening=bool(morphology and any(row.get('asset_id') in optical_same_point_ids
        and row.get('quality_passed') and row.get('decision') in {'positive','negative'} for row in assessments))
    scientific_summary={'status':research_status,'support':support,'availability_answered':availability_answered,
        'analysis_completed':analysis_completed,'science_pixels_analyzed':science_pixels_analyzed,
        'stop_reason':review['stop_reason'],'requested_scientific_analysis_completed':analysis_completed and research_status=='completed',
        'historical_coverage':[{'source':item.get('source'),'catalog_complete':item.get('catalog_complete'),
            'full_history_complete':item.get('full_history_complete'),
            'uncertainties':item.get('analysis',{}).get('uncertainties',[]) if isinstance(item.get('analysis'),dict) else []} for item in science],
        'evidence_boundary':'光谱指数、雷达散射和地表变化只作辅助观测，不作为固废存在或材料类别的直接证据'}
    if hybrid_screening:
        # Preserve the actual optical screening result. Scientific failure or
        # missing history cannot erase a qualified present-day morphology clue;
        # scientific completion cannot promote its level either.
        fusion.update(screening_applicable=True,support='optical_morphology_screening',
            scientific_research=scientific_summary)
    else:
        # A catalog-only answer or unknown optical context must never acquire a
        # waste judgment merely because scientific pixels were analyzed.
        judgment=('已完成科学影像可用性核查' if research_status=='completed' else '已取得部分科学影像可用性记录') if request.mode=='availability' and answered else '已取得科学影像分析结果' if answered else '本次科学研究仍有待补数据'
        fusion.update(level='not_applicable',judgment=judgment,basis=review['stop_reason'],support=support,
            screening_applicable=False,task_evidence_sufficient=answered and research_status=='completed',probability=None,
            analysis_summary=[judgment,review['stop_reason']],scientific_research=scientific_summary,
            boundary='本结论回答科学影像目录或分析问题，不作固废存在、材料类别、违法事实或概率认定')
    result={'mode':request.mode,'request':request.model_dump(mode='json'),'status':research_status,'support':support,
        'availability_answered':availability_answered,'analysis_completed':analysis_completed,
        'science_pixels_analyzed':science_pixels_analyzed,'science_item_count':len(science),
        'asset_ids':[asset['id'] for asset in assets],'new_scientific_asset_ids':[asset['id'] for asset in added],
        'stop_reason':review['stop_reason'],'stop_reason_code':review['stop_reason_code'],
        'screening_applicable':hybrid_screening,'current_morphology_answered':hybrid_screening}
    if morphology:result['optical_context']=morphology['review']
    stage('recognition_review','completed',assessment_count=len(assessments),review=review,fusion=fusion,research_result=result)
    recognition_status='completed' if answered or hybrid_screening else 'needs_more_data'
    pipeline.event(run_id,'recognition',recognition_status,{'research_mode':request.mode,'support':support})
    return {'warnings':warnings,'traces':traces,'strategy':strategy,'task_outline':outline,'workflow_stages':list(stages.values()),
        'catalog':catalog,'selection':selection,'assets':assets,'quality':quality,'recognition':recognition,
        'native_results':native,'recognition_status':recognition_status,'image_assessments':assessments,
        'adaptive_review':review,'fusion':fusion,'scientific_evidence':science,'research_result':result,
        'image_parts':image_parts(pipeline,visual) if optical_needed or morphology else []}


def _evidence_only_result(warnings,traces,strategy,outline,stages,catalog,selection,assets,quality,science):
    """Collect registered evidence without claiming stockpile discovery ran."""
    stages['recognition_review'].update(status='not_required',
        reason='本轮由独立环境描述、风险初评与综合复核阶段接续，未运行固废发现模板')
    outline['deliverable']='环境描述、源—路径—受体风险研判及国家和地方要求的事实对照'
    return {'warnings':warnings,'traces':traces,'strategy':strategy,'task_outline':outline,
        'workflow_stages':list(stages.values()),'catalog':catalog,'selection':selection,'assets':assets,
        'quality':quality,'recognition':Recognition(),'native_results':[],
        'recognition_status':'not_requested_for_this_workflow','image_assessments':[],
        'adaptive_review':{'status':'not_required','enabled':False,'requests':[],
            'reason':'发现环节不替代环境风险初评与复核'},
        'fusion':{'level':'not_applicable','screening_applicable':False},'scientific_evidence':science}


async def collect_evidence(pipeline, run_id, task, *, evidence_only=False):
    from .pipeline import catalog_sources,Selection
    from .intent import resolve_research_request
    uploaded_only=task.online_supplement=='uploaded_only'
    if uploaded_only:
        # Apply the contract for direct API/MCP callers too, not just the portal.
        task=task.model_copy(update={'acquire_optical':False,'selector_mode':'auto'})
    request=resolve_research_request(task)
    if uploaded_only:
        from .schemas import ResearchRequest
        request=ResearchRequest(mode='auto',sources=[])
    warnings=[];traces=[];strategy=task_strategy(task,pipeline.settings.max_assets)
    stages={stage["id"]:{**stage,"status":"pending"} for stage in strategy["stages"]}
    def stage(name,status,**details):
        stages[name].update(status=status,**details)
        pipeline.event(run_id,name,status,{"stage":name,**details})
    stage("task_analysis","running")
    outline={"objective":task.objective,"questions":strategy["contracts"],"required_inputs":{
        "actual_dates":strategy["required_distinct_dates"],"coordinate_system":task.coordinate_system,
        "uploaded_images":len(task.asset_ids),"uploaded_documents":len(task.document_ids)},
        "review_budget":strategy["adaptive_review"],"deliverable":"筛查判断、可核验观测、冲突与数据缺口"}
    if request.mode!='auto':
        stages['source_selection']['label']='高分形态与科学存档检索' if getattr(request,'include_optical_context',False) and request.mode!='availability' else '科学存档检索'
        stages['recognition_review']['label']='逐月目录核验' if request.mode=='availability' else '质量筛查与影像分析'
        stages['synthesis']['label']='结果与证据综合'
        outline.update(questions=[{'task_type':'scientific_research','label':request.mode,'question':task.objective}],
            research_request=request.model_dump(mode='json'),review_budget=strategy['research_budget'],
            deliverable='实际科学目录、可用性和完整性说明' if request.mode=='availability' else '真实科学影像分析与未覆盖的研究问题')
    stage("task_analysis","completed",outline=outline)
    stage("source_selection","running")
    try:
        need_catalog=not uploaded_only and (request.mode!='auto' or task.acquire_optical or task.selector_mode=="model" or bool(task.center or task.bbox))
        catalog=await pipeline.data.catalog() if need_catalog else {"groups":{}}
    except DataClientError as error:
        catalog={"groups":{},"errors":[{"code":error.code}]}
    if catalog.get("errors"):warnings.append("图源目录有失败项，保留上传影像并标明未验证能力")
    pipeline.event(run_id,"catalog","partial" if catalog.get("errors") else "completed",{"errors":catalog.get("errors",[])})
    sources=catalog_sources(catalog);views=initial_views(task,strategy) if request.mode=='auto' and (task.center or task.bbox) else []
    bbox=views[0]["bbox"] if views else list(task.bbox) if task.bbox else None
    route=pipeline.data.route("optical","optical.latest",bbox=bbox,catalog=catalog) if not uploaded_only and request.mode=='auto' and hasattr(pipeline.data,"route") else {"eligible":[],"unknown":[s["source"] for s in sources if s["group"]=="optical"] if request.mode=='auto' else [],"blocked":[]}
    if task.refresh_imagery:
        transient={'unified_api_rejected','unified_provider_render_failed','unified_timeout',
                   'unified_unavailable','data_deadline_exceeded','-1','history_source_error'}
        retryable=[item for item in route['blocked'] if item.get('state')=='failed' and item.get('reason') in transient]
        route['blocked']=[item for item in route['blocked'] if item not in retryable]
        route['unknown']=list(dict.fromkeys([*route['unknown'],*[item['source'] for item in retryable]]))
    blocked={item["source"] for item in route["blocked"]}
    blocked.update(s['source'] for s in sources if s['group']=='optical' and
        (s['metadata'].get('configured') is False or s['metadata'].get('configured_nodes') == []))
    blocked.update(strategy['optical_source_contract']['excluded_sources'])
    blocked.update(source['source'] for source in sources if source['group']=='optical'
                   and not optical_source_allowed(source['source'],strategy['optical_source_contract']))
    priorities=list(dict.fromkeys([*route["eligible"],*strategy["initial_optical_priority"],*route["unknown"]]))
    selection={"ranking":[],"method":"task_contract_and_verified_capabilities","route":route,"missing_requirements":[]}
    for group in strategy["preferred_modalities"]:
        candidates=[s for s in sources if s["group"]==group and not(group=="optical" and s["source"] in blocked)]
        if group=='optical':
            candidates.sort(key=lambda source:optical_candidate_key(source,strategy,route['eligible']))
        else:
            candidates.sort(key=lambda s:priorities.index(s["source"]) if s["source"] in priorities else 100)
        if group != 'optical' and request.mode == 'auto':
            continue  # Scientific modalities require a concrete evidence question.
        for source in candidates[:6]:
            selection["ranking"].append({"group":group,"source":source["source"],
                "reason":"先按可用高分影像核查，需要地表光谱或雷达线索时再补充",
                "independence_group":source_family(source["source"]),"limitations":["仅实际下载并质检后的数据进入视觉证据"],"status":"planned"})
    optical_fallback_pool = [r['source'] for r in selection['ranking'] if r['group'] == 'optical']
    if request.mode == 'auto':
        from .demand import concise_ranking, demand_brief
        selection['ranking'] = concise_ranking(selection['ranking'], task, strategy)
        selection['demand'] = demand_brief(task)
    if request.mode=='auto' and task.selector_mode=="model" and sources and pipeline.settings.models["selector"].ready:
        try:
            proposed,trace=await pipeline.models.complete(pipeline.settings.models["selector"],
                "依据任务数据合同和真实来源选前5项。group/source必须来自允许清单；注明历史时相、尺度、传感器适用性。禁止将当前底图冒充指定历史日期。",
                {"task":task.model_dump(mode="json"),"strategy":strategy,"allowed_sources":selection["ranking"]},Selection)
            allowed={(r["group"],r["source"]) for r in selection["ranking"]}
            if any((r.group,r.source) not in allowed for r in proposed.ranking):raise ValueError("模型选源超出允许清单")
            selection.update(ranking=[{**r.model_dump(),"status":"planned","independence_group":source_family(r.source)} for r in proposed.ranking],method="model_with_task_contract",missing_requirements=proposed.missing_requirements)
            traces.append(trace)
        except (ProviderError,ValueError) as error:warnings.append("模型选源未完成，沿用任务规则："+str(error)[:250])
    if request.mode=='auto' and strategy['optical_source_contract']['explicit_multiple_providers']:
        # A model's short ranking must not hide available providers needed by a
        # current explicit acquisition contract. All additions come from catalog.
        additional=sorted((source for source in sources if source['group']=='optical' and source['source'] not in blocked),
                          key=lambda source:optical_candidate_key(source,strategy,route['eligible']))
        ranked={row['source'] for row in selection['ranking'] if row['group']=='optical'}
        for required in strategy['optical_source_contract']['required_sources']:
            if required not in ranked and any(source['source']==required for source in additional):
                selection['ranking'].append({'group':'optical','source':required,
                    'reason':'用户明确指定的真实目录图源；需实际取得或如实记录缺口',
                    'independence_group':source_family(required),'limitations':['底图提供方不等于独立卫星或时相'],'status':'planned'})
                ranked.add(required)
        for source in additional:
            if len(ranked)>=max(2,len(strategy['optical_source_contract']['required_sources'])):break
            if source['source'] in ranked:continue
            selection['ranking'].append({'group':'optical','source':source['source'],
                'reason':'为用户明确的同点多来源要求保留真实目录中的可回退图源；实际位置与时相另行核验',
                'independence_group':source_family(source['source']),'limitations':['不同底图提供方不代表独立卫星或时相'],'status':'planned'})
            ranked.add(source['source'])
    assets=[pipeline.store.get(asset_id,'image') for asset_id in task.asset_ids]
    if request.mode!='auto':
        return await _collect_research_evidence(pipeline,run_id,task,request,catalog,sources,selection,assets,
            strategy,outline,stages,stage,warnings,traces,evidence_only=evidence_only)
    budget=acquisition_budget(task,pipeline.settings.max_assets)
    baseline={'requested_bbox':bbox,'target_source_groups':strategy['baseline']['target_source_groups'],
              'attempts':[],'budget':budget,'acquired_source_groups':[],'detail_source_groups':[],
              'context_source_groups':[],'shortfalls':[],'uploaded_asset_ids':list(task.asset_ids),
              'all_baseline_acquired_before_recognition':False}
    selection['baseline']=baseline
    science=[];counters={'queries':0,'subsets':0};new_assets=[];successful_families=set();attempted=set()
    pipeline.event(run_id,'selection','completed',{'method':selection['method'],'ranking':selection['ranking'],'baseline':baseline})
    def remember(source,group,acquired,reason=None,science_item=None):
        planned_row = next((r for r in selection['ranking'] if r['group']==group and r['source']==source), None)
        if planned_row is None:
            planned_row = {'group':group,'source':source,'reason':'前一来源未满足本轮覆盖、质量或配准要求后补充',
                           'independence_group':source_family(source),'limitations':[]}
            selection['ranking'].append(planned_row)
        planned_row['status'] = 'downloaded' if acquired else 'unavailable'
        family=source_family(source)
        row={'source':source,'group':group,'independence_group':family,'requested_bbox':bbox,
             'source_identity':'basemap_provider' if group=='optical' else 'satellite_product',
             'status':'downloaded' if acquired else 'unavailable','asset_ids':[a['id'] for a in acquired],
             'reason':reason,'analysis_scope':'target_screening' if acquired else 'unavailable','actual_bbox':None,'full_task_aoi_covered':False}
        if acquired:
            row['analysis_scope']=acquired[0].get('analysis_scope','target_screening')
            row['actual_bbox']=acquired[0].get('requested_view',{}).get('bbox')
            row['full_task_aoi_covered']=row['actual_bbox']==bbox and group!='optical'
            if group=='optical':
                row['returned_requested_bbox']=row['actual_bbox'];row['actual_bbox']=None
                row['coverage_note']='底图实际影像覆盖和原生分辨率仍未独立核验；共同bbox是请求范围'
                row['reason']=row['reason'] or row['coverage_note']
            if row['analysis_scope']=='background_only':row['reason']=acquired[0].get('target_pixel_sufficiency_reason')
            if family:successful_families.add(family)
            for asset in acquired:
                asset['baseline_requested_bbox']=bbox
                asset.setdefault('acquisition_phase','baseline')
                pipeline.store.put('image',asset)
            new_assets.extend(acquired);assets.extend(acquired)
        elif science_item:
            subset=science_item.get('subset',{})
            code=subset.get('reason') or science_item.get('reason')
            row['reason_code']=code
            messages={'spectral_raster_read_failed':'科学栅格暂时读取失败，已按预算核验候选场景；不表示该来源在其他区域不可用',
                      'provider_authentication_required':'此来源需要提供商账户或访问权限，本次未下载科学影像',
                      'no_valid_pixels':'本次窗口在候选场景中没有有效像元，需更换场景或核对范围',
                      'source_cached_blocked':'该来源当前核验结果仍在缓存暂停期，未重复发起同类请求',
                      'data_deadline_exceeded':'数据作业超过等待时限，未重放可能已接受的远程作业'}
            row['reason']=messages.get(code,'科学目录或裁图未完成，本次未形成可分析影像；原因保留在核验记录' if code else '未查到可下载场景，覆盖状态不泛化到其他区域或时段')
            row['actual_bbox']=subset.get('bbox');row['status']=subset.get('status',science_item['status'])
        baseline['attempts'].append(row)
        pipeline.event(run_id,'baseline_acquisition',row['status'],row)
    from .temporal_optical import collect_temporal_optical, HISTORICAL
    from .optical_time import prioritize_optical_period
    historical_first = prioritize_optical_period(task)
    temporal_assets, temporal_collection = [], None
    if historical_first:
        temporal_assets, issues, temporal_collection = await collect_temporal_optical(
            pipeline, run_id, task, strategy, selection, catalog, views, assets)
        warnings.extend(issues)
        for source in dict.fromkeys(row['source'] for row in temporal_collection.get('requests', [])):
            acquired = [a for a in temporal_assets if a['metadata']['source'] == source]
            attempted.add(source)
            remember(source, 'optical', acquired,
                     reason=None if acquired else '指定年度/日期取图未完成；具体错误见获取记录，不能当作无覆盖')
    # Sources share the working region, but each uses its own declared highest
    # level and pixel windows. A provider is not an independent satellite/date.
    optical=list(dict.fromkeys([*[r['source'] for r in selection['ranking'] if r['group']=='optical'],*optical_fallback_pool]))
    if task.optical_source:optical=list(dict.fromkeys([task.optical_source,*optical]))
    if task.acquire_optical and views and not temporal_assets:
        optical_attempts=0
        acquisition_started=time.monotonic()
        time_limit=strategy['baseline']['max_initial_optical_seconds']
        acquisition_deadline=acquisition_started+time_limit
        baseline['source_time_budget_seconds']=time_limit
        for source in optical:
            if historical_first and source in HISTORICAL:
                continue  # Never retry a failed annual product without its year.
            family=source_family(source)
            if not optical_source_allowed(source,strategy['optical_source_contract']):
                baseline['attempts'].append({'source':source,'group':'optical','status':'blocked',
                    'reason':'当前用户限定或禁止的图源范围不允许本来源','asset_ids':[],'requested_bbox':bbox})
                continue
            if source in blocked:
                baseline['attempts'].append({'source':source,'group':'optical','status':'blocked','reason':'缓存的接口/权限或精确区域能力检查未通过','asset_ids':[],'requested_bbox':bbox})
                continue
            if source in attempted or (family and family in successful_families):continue
            remaining_seconds=acquisition_deadline-time.monotonic()
            if remaining_seconds<=0:
                break
            if (len(new_assets)>=budget['baseline_capacity'] or
                    optical_attempts>=strategy['baseline']['max_initial_optical_attempts']):break
            attempted.add(source);optical_attempts+=1
            try:
                async with asyncio.timeout(remaining_seconds):
                    acquired,issues=await pipeline.acquire_views(run_id,task,selection,catalog,view_plan=views[:1],source_choices=[source],
                        phase='historical_gap_background' if historical_first else 'baseline',
                        plan_coverage=True, max_new_assets=max(0,pipeline.settings.max_assets-len(assets)))
            except TimeoutError:
                acquired=[]
                issues=['本轮高分取图总时间预算已用完，本来源请求未完成；其余来源保留未尝试记录，不能视为无覆盖']
                pipeline.event(run_id,'acquisition','failed',{'source':source,
                    'reason':'initial_optical_time_budget_exhausted','summary':issues[0],
                    'elapsed_seconds':round(time.monotonic()-acquisition_started,3)})
            if historical_first:
                for asset in acquired:
                    asset['requested_period_applicability'] = 'undated_background_only_not_requested_year_evidence'
                    pipeline.store.put('image', asset)
            warnings.extend(issues);remember(source,'optical',acquired,reason='；'.join(issues) if not acquired else None)
            # Recognize before choosing any additional modality or source.
            if acquired:break
        baseline['source_acquisition_elapsed_seconds']=round(time.monotonic()-acquisition_started,3)
        if time.monotonic()>=acquisition_deadline and not new_assets:
            baseline['stop_reason']='initial_optical_time_budget_exhausted'
            baseline['not_attempted_sources']=[source for source in optical if source not in attempted and source not in blocked]
            baseline['not_attempted_reason']='高分取图总时间预算耗尽，尚未请求这些来源，不代表这些来源无覆盖'
            warnings.append(baseline['not_attempted_reason'])
            pipeline.event(run_id,'baseline_acquisition','partial',{
                'reason':baseline['stop_reason'],'summary':baseline['not_attempted_reason'],
                'time_budget_seconds':time_limit,'not_attempted_sources':baseline['not_attempted_sources']})
    # Dated optical products have their own bounded path. A historical request
    # must not silently compare two undated current basemaps as before/after.
    if not historical_first:
        temporal_assets, temporal_warnings, temporal_collection = await collect_temporal_optical(
            pipeline, run_id, task, strategy, selection, catalog, views, assets)
        warnings.extend(temporal_warnings)
        assets.extend(temporal_assets); new_assets.extend(temporal_assets)
    selection['temporal_optical'] = temporal_collection
    # The initial source may require several highest-level windows to cover a
    # named-place neighbourhood. Count these real images against the shared
    # cap; the old one-centre-frame budget description must not survive.
    if len(new_assets) > budget['baseline_capacity']:
        budget['baseline_capacity'] = len(new_assets)
        free_slots = max(0, pipeline.settings.max_assets-len(assets))
        budget['reserved_followup_slots'] = min(2, free_slots) if task.adaptive_review else 0
        budget['max_followup_images'] = budget['reserved_followup_slots']
        budget['explanation'] = (f"总上限{pipeline.settings.max_assets}张；已取得{len(new_assets)}张年度/周边窗口影像，"
            f"可追加{budget['max_followup_images']}张；图源层级、工作范围与未执行窗口分别记录，不按来源名字凑数量。")
    if temporal_assets and hasattr(pipeline, 'refresh_spatial_evidence'):
        assets = await pipeline.refresh_spatial_evidence(assets)
    # Scientific modalities are deferred until actual optical evidence exposes
    # a relevant temporal/spectral/scattering question. No baseline science quota.
    baseline['acquired_source_groups']=sorted(successful_families)
    baseline['detail_source_groups']=sorted({source_family(a['metadata']['source']) for a in new_assets if a.get('analysis_scope')!='background_only' and source_family(a['metadata']['source'])})
    baseline['context_source_groups']=sorted({source_family(a['metadata']['source']) for a in new_assets if a.get('analysis_scope')=='background_only' and source_family(a['metadata']['source'])})
    if not bbox:baseline['shortfalls'].append('缺少可核验点位或范围，不能根据未配准的上传图片猜测取图位置')
    if not task.acquire_optical and bbox:baseline['shortfalls'].append('本任务未启用在线光学取图，未擅自获取底图')
    warnings.extend(baseline['shortfalls'])
    quality=assess_quality(task,assets);pipeline.event(run_id,'quality','completed',quality)
    stage('source_selection','completed',selection=selection,science_queries=science,
          registered_asset_ids=[a['id'] for a in assets],acquisition_issues=list(warnings))
    if evidence_only:
        return _evidence_only_result(warnings,traces,strategy,outline,stages,catalog,selection,assets,quality,science)
    stage('recognition_review','running')
    visual=[a for a in assets if a['metadata']['modality']=='optical' and not a.get('science_preview')]
    recognition,native,assessments,initial_traces=await recognize(pipeline,run_id,task,visual,quality,phase='initial')
    traces.extend(initial_traces)
    # User-provided scientific previews also belong to the scientific role.
    # Their radiometry/QA is explicitly unknown unless a native adapter supplies it.
    supplied_science=[a for a in assets if a['metadata']['modality'] in {'multispectral','sar','hyperspectral'}]
    if supplied_science:
        item={'group':'user_science','source':'user_upload','status':'uploaded_unverified',
              'limitation':'用户科学预览的辐射标定、QA、实际范围和配准未由原始产品核验；不从截图反算定量指数',
              'calibration':{'status':'unknown','offset_assumed':False}}
        item['analysis']=await _analyze_science_with_events(pipeline,run_id,task,supplied_science,item,traces,context_assets=assets)
        item['science_pixels_analyzed']=item['analysis'].get('science_pixels_analyzed',False);science.append(item)
    review={'triggered':False,'enabled':task.adaptive_review,'requests':[],'local_crops':[],
            'main_visual_review':{'status':'not_triggered'},'science_analyses':[item['analysis'] for item in science if item.get('analysis')],
            'budget':{**strategy['adaptive_review'],'effective_max_additional_images':budget['max_followup_images']},
            'coverage_decisions':[],'action_history':[],'rounds':0,'stop_reason':None}
    completed=set();completed_views=set();spatially_reread=set();added=0;remote_added=0;fetch_attempts=0;local_crops=0
    single_reviewed = (len(visual)==1 and bool(assessments)
        and assessments[0].get('optical_false_positive_review',{}).get('status')=='completed')
    if single_reviewed:
        review['main_visual_review']={'status':'not_required','reason':'single_image_candidates_already_reviewed',
            'evidence_asset_ids':[visual[0]['id']], 'can_override_positive_candidate':False}
    elif task.adaptive_review and (recognition.candidates or any(row['decision']=='unknown' and not row.get('reason') for row in assessments)):
        review['main_visual_review']=await _visual_review(pipeline,task,visual,recognition,traces,run_id=run_id)
    for round_index in range(strategy['adaptive_review']['max_rounds']):
        planning_sources=optical  # Internal fallback pool, exposed only when an actual gap triggers it.
        review['spatial_prerequisites']=spatial_prerequisites(task,assets,assessments,available_sources=planning_sources,attempted_sources=attempted)
        spatial=review['spatial_prerequisites']
        if spatial['spatial_correspondence_unresolved'] and not spatial['can_attempt_reference_recovery']:
            review['unresolved_spatial_requirements']=[spatial['reason'],'未核验光学图仅作图内上下文，不能认定为任务点位的目标']
        review_context={**review['main_visual_review'],'science_analyses':review['science_analyses']}
        decisions=next_evidence_actions(task,assets,recognition,assessments,review_context,views[0] if views else None,completed=completed,
            available_sources=planning_sources,attempted_sources=attempted)
        if not decisions:
            provider_status=optical_source_status(task,assets)
            if uploaded_only:
                review['stop_reason']='已按仅使用上传资料的范围完成本轮分析，未在线补图；尚未解决的定位、时相或专业证据缺口继续保留。'
                review['stop_reason_code']='uploaded_only_scope_completed'
            elif provider_status['explicit_multiple_providers'] and not provider_status['provider_count_met']:
                review['stop_reason']='本次有限图源尝试仍未满足用户明确的多来源要求；保留已取得影像和缺失来源，不将单图视为任务完成'
                review['stop_reason_code']='explicit_multi_provider_shortfall'
            elif not assessments or not any(row.get('decision') in {'positive','negative'} for row in assessments):
                review['stop_reason']='现有影像未形成有效同点判读，保留未知；盲目追加同类数据不能修复模型或空间核验缺口'
                review['stop_reason_code']='insufficient_current_evidence'
            elif strategy['requires_temporal_evidence'] and not (task.date_start and task.date_end):
                review['stop_reason']='缺少任务实际日期，未用当前底图或近期默认场景替代历史；只能回答当前可见部分'
                review['stop_reason_code']='missing_requested_dates'
            else:
                review['stop_reason']='当前证据已足以回答可见筛查问题，无需为凑来源数量追加；剩余法定或现场问题单列核查'
                review['stop_reason_code']='current_visible_question_answered'
            break
        if not task.adaptive_review:
            review['stop_reason']='追加核验已关闭，按已有证据回答并保留缺口';review['stop_reason_code']='adaptive_disabled';break
        decision=decisions[0];action=decision['action']
        if len(assets)>=pipeline.settings.max_assets or (action!='local_crop' and remote_added>=budget['max_followup_images']):
            review['stop_reason']='追加影像预算已到上限，保留尚未解决的问题';review['stop_reason_code']='image_budget_exhausted';break
        completed.add(decision.get('action_key', action))
        review['triggered']=True;review['rounds']=round_index+1
        review['trigger_reason']='按具体疑点、质量、空间对应或任务时相缺口选择最少必要补证'
        decision.update(round=round_index+1);review['coverage_decisions'].append(decision)
        original=next((a for a in assets if a['id']==decision.get('asset_id')),visual[0] if visual else None)
        follow=[];scientific=action in {'multispectral','sar','timeseries'}
        if scientific:
            if not bbox:
                decision.update(status='not_attempted',outcome='缺少可信空间范围，不能由图片猜测科学取图位置')
            elif not spatial['independent_task_aoi_available']:
                decision.update(status='not_attempted',outcome='任务坐标基准未明确，不能从未对齐光学图推断科学取图位置')
            elif action=='timeseries' and not (task.date_start and task.date_end):
                decision.update(status='not_attempted',outcome='缺少实际任务日期，未用当前底图或默认日期替代历史')
            elif counters['queries']>=strategy['science_budget']['max_catalog_queries'] or counters['subsets']>=strategy['science_budget']['max_subset_attempts']:
                decision.update(status='skipped_budget',outcome='科学查询或场景读取预算已用尽，未重复接受过的作业')
            else:
                group='sar' if action=='sar' else 'multispectral';source='sentinel1-rtc' if group=='sar' else 'sentinel2'
                available=any(row['group']==group and row['source']==source for row in sources)
                analyst=pipeline.settings.models.get('science_analyst')
                if not analyst or not analyst.ready or 'image' not in analyst.input_modalities:
                    decision.update(status='not_attempted',outcome='科学影像分析角色未就绪，未为无法分析的影像发起下载')
                elif not available:
                    decision.update(status='not_attempted',outcome='当前真实目录未提供所需科学来源，未猜测数据接口')
                else:
                    rank={'group':group,'source':source,'action':action,'reason':decision['reason'],
                          'spatial_basis':{'scope':spatial['scientific_scope'],'optical_target_correspondence_verified':spatial['optical_same_point_supported'],
                                           'can_repair_optical_registration':False},
                          'max_scenes':min(budget['max_followup_images']-remote_added,pipeline.settings.max_assets-len(assets))}
                    follow,item=await _query_science(pipeline,run_id,task,rank,bbox,strategy,counters,context_assets=assets)
                    remaining=min(budget['max_followup_images']-remote_added,pipeline.settings.max_assets-len(assets))
                    # All downloaded previews count toward the same task image cap.
                    follow=follow[:remaining];traces.extend(item.pop('_traces',[]));science.append(item)
                    if item.get('analysis'):review['science_analyses'].append(item['analysis'])
                    if item.get('analysis',{}).get('reason')=='ProviderError':
                        completed.update({'multispectral','sar','timeseries'})
                    decision.update(acquisition_source=source,status='completed' if follow else 'unavailable',
                                    outcome='已取得真实科学窗口并由科学角色独立分析' if item.get('science_pixels_analyzed') else '科学证据未形成有效分析，保留未知')
        elif action=='local_crop':
            if local_crops>=strategy['adaptive_review']['max_local_crops']:
                decision.update(status='skipped_budget',outcome='局部裁图预算已用尽')
            else:
                available_crop_slots=min(strategy['adaptive_review']['max_local_crops']-local_crops,
                                         pipeline.settings.max_assets-len(assets))
                crop_queue=[row for row in decisions if row['action']=='local_crop'][:available_crop_slots]
                decision['candidate_reviews']=[]
                for crop_decision in crop_queue:
                    completed.add(crop_decision.get('action_key', 'local_crop'))
                    parent_id=crop_decision.get('asset_id')
                    try:
                        cropped=_crop_candidate(pipeline,recognition,assets,preferred_asset_id=parent_id,
                            preferred_pixel_window=crop_decision.get('pixel_window'))
                    except (ValueError, KeyError, OSError):
                        cropped=None
                    item={'parent_asset_id':parent_id,'pixel_window':crop_decision.get('pixel_window'),
                          'candidate_index':crop_decision.get('candidate_index'),
                          'evidence_labels':crop_decision.get('evidence_labels',[]),
                          'status':'cropped' if cropped else 'unavailable'}
                    decision['candidate_reviews'].append(item)
                    if not cropped:continue
                    item['asset_id']=cropped['id']
                    follow.append(cropped);local_crops+=1
                    cropped['detail_review_target']={key:value for key,value in item.items() if key!='status'}
                    pipeline.store.put('image',cropped)
                    pipeline.event(run_id, 'image_crop', 'completed', {
                        'asset_id': cropped['id'], 'parent_asset_id': cropped['derived_from_asset_id'],
                        'source': cropped['metadata']['source'], 'pixel_window': cropped['source_pixel_window'],
                        'candidate_index':crop_decision.get('candidate_index'),
                        'candidate_reference':'、'.join(crop_decision.get('evidence_labels',[])),
                        'width': cropped['width'], 'height': cropped['height'],
                        'independent_evidence': False, 'native_detail_increased': False})
                    review['local_crops'].append({'asset_id':cropped['id'],'parent_asset_id':cropped['derived_from_asset_id'],
                        'candidate_index':crop_decision.get('candidate_index'),
                        'evidence_labels':crop_decision.get('evidence_labels',[]),
                        'target_pixel_window':crop_decision.get('pixel_window'),
                        'source_pixel_window':cropped['source_pixel_window'],'independent_evidence':False})
                if not follow:decision.update(status='not_attempted',outcome='没有可用候选像素框或原图完整性未通过；保留原图疑点，未猜测位置')
        else:
            # A candidate may lie in any neighbourhood grid window. Follow its
            # own requested footprint rather than jumping back to the task centre.
            base=acquisition_view(original, assets)
            base=base or (views[0] if views else None)
            view=expanded_view(task,base) if action=='expand_context' else detail_view(task,base) if action=='detail_view' and base else base
            source=original['metadata']['source'] if original else None
            source_options=[source] if source else []
            if action=='same_coverage_alternate':
                used={optical_provider_group(a['metadata']['source']) for a in assets
                      if a.get('metadata',{}).get('modality')=='optical' and not a.get('derived_from_asset_id')}
                requested=decision.get('requested_source')
                status=optical_source_status(task,assets)
                options=[requested,*status['missing_requested_sources'],*planning_sources]
                source_options=list(dict.fromkeys(name for name in options if name and name in planning_sources
                    and name not in attempted and name not in blocked and optical_provider_group(name) not in used))
                if not (spatial['reference_available'] or status['explicit_multiple_providers']):
                    source_options=source_options[:1]  # An unsuccessful anchor does not make unknown mosaics co-located.
                source=source_options[0] if source_options else None
            source_options=[name for name in source_options if name not in blocked
                            and optical_source_allowed(name,strategy['optical_source_contract'])]
            source=source_options[0] if source_options else None
            if not task.acquire_optical or not source or not view:
                decision.update(status='not_attempted',outcome='未启用在线取图、缺少适用来源/范围，或当前尺度约束不允许追加')
            elif fetch_attempts>=strategy['adaptive_review']['max_additional_source_attempts']:
                decision.update(status='skipped_budget',outcome='追加远程请求次数已用尽')
            elif (source,tuple(view['bbox'])) in completed_views:
                decision.update(status='skipped_duplicate',outcome='相同图源和范围已经请求，未重复下载')
            else:
                decision['source_attempts']=[]
                for source in source_options:
                    if fetch_attempts>=strategy['adaptive_review']['max_additional_source_attempts'] or len(attempted)>=strategy['adaptive_review']['max_total_optical_provider_attempts']:
                        break
                    if (source,tuple(view['bbox'])) in completed_views:continue
                    completed_views.add((source,tuple(view['bbox'])));fetch_attempts+=1;attempted.add(source)
                    grid_followup = bool(base and base.get('coverage_window_id') and
                                         not (task.complaint_scan and task.complaint_scan.window_id))
                    follow_task = task.model_copy(update={'center': None, 'bbox': tuple(view['bbox']),
                        'location_context': {}, 'nearby_radius_m': None}) if grid_followup else task
                    source_view = dict(view)
                    if source != 'history246':
                        source_view.pop('date', None)
                    if source not in {'century', 'jilin2024', 'siwei2025'}:
                        source_view.pop('year', None)
                    follow,issues=await pipeline.acquire_views(run_id,follow_task,selection,catalog,
                        view_plan=[source_view],source_choices=[source],phase='coverage_'+action,
                        plan_coverage=grid_followup,
                        max_new_assets=min(budget['max_followup_images']-remote_added,pipeline.settings.max_assets-len(assets)))
                    warnings.extend(issues);decision.update(requested_bbox=view['bbox'],acquisition_source=source)
                    decision['source_attempts'].append({'source':source,'status':'completed' if follow else 'unavailable',
                        'asset_ids':[asset['id'] for asset in follow],
                        'reason':'已取得影像，空间对应另行核验' if follow else '本次请求未取得可用影像；不代表该来源无覆盖'})
                    if follow or action!='same_coverage_alternate':break
        if follow:
            if not scientific:
                try:
                    if uploaded_only or action=='local_crop':
                        reconsider=[]  # A local crop does not authorize gateway metadata requests.
                    else:
                        assets,follow,reconsider=await refresh_registered_assets(pipeline,assets,follow,assessments,spatially_reread)
                except (DataClientError,ValueError,OSError):
                    reconsider=[];warnings.append('空间核验元数据刷新未完成，保留原空间未知状态')
                if original and any(row['asset_id']==original['id'] and row['decision']=='positive' for row in assessments):
                    for asset in follow:
                        asset['candidate_context_for_asset_id']=original['id'];pipeline.store.put('image',asset)
                extra,new_native,new_assess,new_traces=(await recognize_detail_batch(pipeline,run_id,task,follow,assess_quality(task,[*assets,*follow]),context_assets=assets)
                    if action=='local_crop' else await recognize(pipeline,run_id,task,follow,assess_quality(task,[*assets,*follow]),phase='coverage_followup'))
                merge_recognition(recognition,extra);native.extend(new_native);assessments.extend(new_assess);traces.extend(new_traces)
                if reconsider:
                    identities={asset['id'] for asset in reconsider};spatially_reread.update(identities)
                    reused=requalify_registered_assessments(assessments,reconsider)
                    review.setdefault('spatial_reassessments',[]).append({'asset_ids':sorted(identities),'reason':'existing_image_became_spatially_eligible',
                        'new_asset_count':0,'independent_source_count_increased':False,'semantic_result_reused':True,
                        'requalified_asset_ids':reused,'status':'completed'})
                    pipeline.event(run_id,'spatial_registration_refresh','completed',{'asset_ids':sorted(identities),'new_asset_count':0,'semantic_result_reused':True})
                if action=='local_crop':
                    by_assessment={row['asset_id']:row for row in new_assess}
                    completed_reviews=0
                    for item in decision['candidate_reviews']:
                        row=by_assessment.get(item.get('asset_id'),{})
                        read_done='jianwei_raw_decision' in row
                        review_done=read_done and (not row.get('jianwei_raw_candidate_count') or
                            row.get('optical_false_positive_review',{}).get('status')=='completed')
                        item.update(recognition_status='completed' if read_done else 'unavailable',
                            visual_review_status='completed' if review_done else 'unavailable',
                            status='completed' if review_done else 'partial' if read_done else 'unavailable')
                        completed_reviews+=int(review_done)
                    all_done=completed_reviews==len(decision['candidate_reviews'])
                    decision.update(status='completed' if all_done else 'partial',
                        outcome=f'已提取 {len(follow)} 处候选局部，其中 {completed_reviews} 处完成见微重读与复核；'
                            +('未解决的线索继续保留。' if all_done else '其余复核尚未完成，具体缺口逐处保留，不能按未发现处理。'))
                else:decision.update(status='completed',outcome='已取得补充高分影像并完成逐图核验')
            assets.extend(follow);added+=len(follow);remote_added+=0 if action=='local_crop' else len(follow);quality=assess_quality(task,assets)
            decision['result_asset_ids']=[a['id'] for a in follow]
            if not scientific and action!='local_crop' and recognition.candidates and round_index+1<strategy['adaptive_review']['max_main_visual_reviews']:
                visual=[a for a in assets if a['metadata']['modality']=='optical' and not a.get('science_preview')]
                review['main_visual_review']=await _visual_review(pipeline,task,visual,recognition,traces,run_id=run_id)
        elif decision.get('status')=='planned':decision.update(status='unavailable',outcome='未取得补充影像，保留原疑点与具体缺口')
        review['requests'].append({'source':decision.get('acquisition_source',original['metadata']['source'] if original else None),
            'reason':decision['reason'],'action':action,'status':decision['status'],'asset_ids':[a['id'] for a in follow]})
        review['action_history'].append(copy.deepcopy(decision))
        pipeline.event(run_id,'adaptive_review',decision['status'],decision)
    if not review['stop_reason']:
        pending=next_evidence_actions(task,assets,recognition,assessments,{**review['main_visual_review'],'science_analyses':review['science_analyses']},views[0] if views else None,completed=completed,
            available_sources=[row['source'] for row in selection['ranking'] if row['group']=='optical'],attempted_sources=attempted)
        if any(row['status'] not in {'completed'} for row in review['action_history']):
            review['stop_reason']='补证受可用来源、分析服务或预算限制，停止重复请求；尚未解决的问题保留未知'
            review['stop_reason_code']='source_or_analysis_unavailable'
        else:
            review['stop_reason']='有限核验轮次已结束，保留尚未解决的问题' if pending else '已完成针对关键缺口的核验；当前证据可回答的部分已收敛'
            review['stop_reason_code']='max_rounds_reached' if pending else 'targeted_review_finished'
    review['additional_asset_count']=added
    review['additional_remote_asset_count']=remote_added
    review['pending_candidate_reviews']=[row for row in next_evidence_actions(task,assets,recognition,assessments,
        {},views[0] if views else None,completed=completed) if row['action']=='local_crop']
    provider_status=optical_source_status(task,assets)
    review['multisource_requirement']=provider_status
    baseline.update(acquired_provider_groups=provider_status['acquired_provider_groups'],
                    provider_count_met=provider_status['provider_count_met'],
                    missing_requested_sources=provider_status['missing_requested_sources'])
    if provider_status['explicit_multiple_providers'] and not provider_status['provider_count_met']:
        shortfall=(f"用户要求的多来源影像尚未取得：本次{provider_status['acquired_provider_count']}个底图来源组，"
                   f"至少需要{provider_status['minimum_provider_groups']}个；指定来源缺口："
                   + ('、'.join(provider_status['missing_requested_sources']) or '仍缺另一来源')
                   +'。来源数量不代表独立卫星或时相，空间对应仍须另行核验。')
        baseline['shortfalls'].append(shortfall);warnings.append(shortfall)
        selection['missing_requirements'].append(shortfall)
    reviewed=set(review['main_visual_review'].get('evidence_asset_ids',[]))
    review['main_visual_review']['additional_assets_not_in_this_review']=[a['id'] for a in assets
        if a['metadata']['modality']=='optical' and a['id'] not in reviewed]
    fusion=fuse_findings(task,assets,assessments,strategy,review["main_visual_review"])
    quality=assess_quality(task,assets)
    stage("recognition_review","completed",assessment_count=len(assessments),review=review,fusion=fusion)
    config=pipeline.settings.models["recognizer"]
    recognition_status="not_configured" if not config.ready else "needs_more_data" if not assessments or not any(a["decision"]!="unknown" for a in assessments) else "completed"
    if config.ready and provider_status['explicit_multiple_providers'] and not provider_status['provider_count_met']:
        recognition_status='needs_more_data'
    pipeline.event(run_id,"recognition",recognition_status)
    return {"warnings":warnings,"traces":traces,"strategy":strategy,"task_outline":outline,"workflow_stages":list(stages.values()),
            "catalog":catalog,"selection":selection,"assets":assets,"quality":quality,"recognition":recognition,
            "native_results":native,"recognition_status":recognition_status,"image_assessments":assessments,
            "adaptive_review":review,"fusion":fusion,"scientific_evidence":science,"image_parts":image_parts(pipeline,assets)}
