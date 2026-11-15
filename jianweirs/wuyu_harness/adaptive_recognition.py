"""见微识别、同图/跨图复核与空间前提（adaptive 的叶子模块）。"""
from __future__ import annotations

import base64
import copy
import re
import asyncio
import time
from dataclasses import replace


from .evidence import sanitize_claims, validate_recognition
from .models import ProviderError
from .schemas import Recognition
from .strategy import source_family


MODEL_METADATA_RULE = ('本图日期和原生分辨率只采用登记metadata字段，null表示未核实，用户声明仍待核验。'
                       '请求bbox、显示比例、图幅米数、像素尺寸以及中心点局部样本均不能推断整图原生GSD或实际覆盖；'
                       '禁止把显示米/像素写成传感器原生分辨率。')


def spatial_eligible(asset):
    registration=asset.get('spatial_registration') or asset.get('data_api_metadata',{}).get('spatial_registration') or {}
    return registration.get('status')=='verified' and registration.get('strict_same_point_eligible') is True


def model_asset_context(asset: dict) -> dict:
    """Allowlist image facts; upstream point samples stay in the private audit.

    Registered metadata has already passed the acquisition scope gate. This
    second gate also prevents a point-scoped declaration becoming a scene fact.
    Requested footprints carry no implicit affine transform or resolution proof.
    """
    fields=('id','width','height','mime','derived_from_asset_id','source_pixel_window','analysis_scope',
            'target_pixel_sufficiency_reason','native_pixels_across_declared_site','visual_preview',
            'scene_id','source_bands','display_transform','acquisition_phase','candidate_context_for_asset_id','detail_review_target')
    context={key:copy.deepcopy(asset[key]) for key in fields if key in asset}
    approved=('role','modality','source','acquisition_time','resolution_m','crs','georeferenced',
              'registration_verified','radiometric_comparability_verified','metadata_scope','period_label')
    metadata={key:copy.deepcopy(asset.get('metadata',{}).get(key)) for key in approved if key in asset.get('metadata',{})}
    scope=str(metadata.get('metadata_scope',''))
    if re.search(r'point|center|中心',scope,re.IGNORECASE):
        metadata.update(acquisition_time=None,resolution_m=None)
    context['metadata']=metadata
    registration=asset.get('spatial_registration') or asset.get('data_api_metadata',{}).get('spatial_registration') or {}
    if registration:
        context['spatial_registration']={key:copy.deepcopy(registration[key]) for key in ('status','strict_same_point_eligible','method','reason','uncertainty_m') if key in registration}
    view=asset.get('requested_view') or {}
    if view.get('bbox'):
        context['requested_coverage']={'bbox':copy.deepcopy(view['bbox']),'meaning':'请求范围；不是经核验的整图实际覆盖或像素地理变换',
                                       'actual_coverage_verified':False,'native_gsd_proven':False}
    context['metadata_adoption_rule']=MODEL_METADATA_RULE
    return context


def image_parts(pipeline, assets):
    parts=[]
    for asset in assets:
        if asset.get('science_preview') or asset.get('metadata',{}).get('modality')!='optical':
            # Dedicated scientific analyst owns all spectral and radar images.
            continue
        content=pipeline.store.asset_path(asset).read_bytes()
        parts.extend([{"type":"text","text":f"asset_id={asset['id']}; role={asset['metadata']['role']}; pixels={asset['width']}x{asset['height']}"},
                      {"type":"image_url","image_url":{"url":f"data:{asset['mime']};base64,{base64.b64encode(content).decode()}"}}])
    return parts


def _decision(recognition, native):
    if recognition.candidates:
        return "positive"
    if native:
        return "negative" if native["decision"]=="未发现固废堆存" else "unknown"
    # Empty JSON candidates alone do not mean an explicit negative review.
    negative=any(re.search(r"未发现|未见|未观察到|未识别到",obs.statement) for obs in recognition.observations)
    return "negative" if negative and not recognition.quality_issues else "unknown"


async def recognize(pipeline, run_id, task, assets, quality, *, phase, context_assets=None):
    combined=Recognition();native_results=[];assessments=[];traces=[]
    config=pipeline.settings.models["recognizer"]
    async def _read_one(asset):
        local={'assessments':[],'traces':[],'native':[],'recognition':None,'unknowns':[]}
        if asset.get('science_preview') or asset.get('metadata',{}).get('modality')!='optical':return None
        result=Recognition();native=None
        assessment={"asset_id":asset["id"],"phase":phase,"source":asset["metadata"]["source"],
                    'candidate_context_for_asset_id':asset.get('candidate_context_for_asset_id'),
                    "source_group":source_family(asset["metadata"]["source"]),"decision":"unknown",
                    "target_relevant":asset["metadata"]["role"] not in {"full_scene","local_context"} and min(asset["width"],asset["height"])>=64
                        and (not asset.get("visual_preview") or asset.get('analysis_scope')=='target_screening' or asset["metadata"].get("resolution_m",999)<=5),
                    "quality_passed":False,"candidate_count":0}
        try:
            from omap_api.image_quality import validate_optical_preview
            if max(asset["width"],asset["height"])>4096:
                raise ValueError("识别输入边长超过4096，请分块")
            content=pipeline.store.asset_path(asset).read_bytes()
            asset_quality=validate_optical_preview(content)
            if not asset_quality["valid"]:
                raise ValueError("非影像错误瓦片："+asset_quality["reason"])
            assessment["quality_passed"]=True
            if asset.get("analysis_scope")=="background_only":
                assessment.update(analysis_scope="background_only",target_relevant=False,reason="native_pixels_insufficient_for_target",
                                  analysis_summary=[asset.get("target_pixel_sufficiency_reason","原生像元不足，只作背景参考")])
                local['assessments'].append(assessment)
                pipeline.event(run_id,"image_recognition","background_only",{"asset_id":asset["id"],"phase":phase,"reason":assessment["reason"]})
                return local
            if not config.ready:
                assessment["reason"]="recognizer_not_configured"
                local['assessments'].append(assessment)
                return local
            pipeline.event(run_id,'image_recognition','running',{'asset_id':asset['id'],'source':asset['metadata']['source'],'phase':phase})
            parent=next((item for item in context_assets or [] if item['id']==asset.get('derived_from_asset_id')),None)
            context_parent=copy.deepcopy(parent) if parent else None
            if context_parent:context_parent['metadata']['role']='full_scene'
            inputs=[asset,*([context_parent] if context_parent else [])]
            if pipeline.settings.rs_output_format=="xml":
                from .rs_xml import NATIVE_XML_PROMPT,parse_rs_xml
                raw,trace=await pipeline.models.complete_native(config,NATIVE_XML_PROMPT+'\n'+MODEL_METADATA_RULE,
                    {"task":task.model_dump(mode="json"),"asset":model_asset_context(asset),"quality":quality,
                     "phase":phase,"parent_context":model_asset_context(context_parent) if context_parent else None,
                     "evidence_boundary":"只使用最终answer和当前主图像，父图仅提供同源上下文；框坐标只用主图原始像素，不采用内部思考文本"},image_parts(pipeline,inputs))
                parsed=parse_rs_xml(raw,asset_id=asset["id"],width=asset["width"],height=asset["height"])
                result,native=parsed.recognition,parsed.native
            else:
                result,trace=await pipeline.models.complete(config,
                    "依据当前注册图像给出可见特征和候选。仅引用当前asset_id与原始像素框；evidence_indices从0开始。"
                    "候选须有非固废竞争解释。清晰审查后确实未发现目标时在observations写明未发现及所见场景，"
                    "看不清或不相关时写quality_issues/unknowns；空候选本身不代表负判。不得输出内部推理或未实现的测量结果。"+MODEL_METADATA_RULE,
                    {"task":task.model_dump(mode="json"),"assets":[model_asset_context(asset)],"quality":quality,"phase":phase,
                     "parent_context":model_asset_context(context_parent) if context_parent else None},
                    Recognition,image_parts(pipeline,inputs))
            validate_recognition(result,[asset])
            local['traces'].append(trace)
            if native:local['native'].append(native)
            raw_decision=_decision(result,native)
            assessment.update(jianwei_raw_decision=raw_decision,jianwei_raw_candidate_count=len(result.candidates))
            pipeline.event(run_id,'image_recognition',raw_decision,{'phase':phase,'asset_id':asset['id'],
                'source':asset['metadata']['source'],'candidate_count':len(result.candidates),'raw_result':True,
                'summary':'；'.join(obs.statement for obs in result.observations if obs.pixel_window is None)[:900]})
            same_image_review=None
            if result.candidates:
                from .optical_review import review_optical_candidates
                assessment['jianwei_raw_recognition']=result.model_dump(mode='json')
                result,same_image_review,review_trace=await review_optical_candidates(
                    pipeline,run_id,task,asset,result,model_asset_context(asset),content,
                    context_parts=image_parts(pipeline,[context_parent]) if context_parent else None)
                assessment['optical_false_positive_review']=same_image_review
                if review_trace:local['traces'].append(review_trace)
            # A failed/unconfigured review leaves the detector's own decision.
            decision=(same_image_review.get('effective_decision',raw_decision)
                      if same_image_review and same_image_review.get('status')=='completed' else raw_decision)
            assessment.update(decision=decision,candidate_count=len(result.candidates),
                              analysis_summary=[obs.statement for obs in result.observations if obs.pixel_window is None][:2],
                              quality_issues=list(result.quality_issues))
            if same_image_review and same_image_review.get('status')=='completed':
                assessment['analysis_summary']=[same_image_review['summary']]
            registration=asset.get('spatial_registration') or asset.get('data_api_metadata',{}).get('spatial_registration') or {}
            online=asset.get('acquisition_phase') not in (None,'user_upload') and asset['metadata']['source']!='user_upload'
            if online and not spatial_eligible(asset):
                assessment.update(image_only_decision=assessment['decision'],decision='unknown',target_relevant=False,
                    spatial_status=registration.get('status','unknown'),same_point_eligible=False,
                    reason='spatial_registration_unverified',analysis_summary=[*assessment.get('analysis_summary',[]),
                        '本图仅作图内场景筛查；尚未核验与任务点的空间对应，不能作为同点正证据'])
            elif online:
                assessment.update(spatial_status=registration.get('status','verified'),same_point_eligible=True)
            # A JSON output explicitly reporting uncertainty is never a qualified negative.
            if result.quality_issues or (not native and result.unknowns and not result.candidates):
                assessment["decision"]="unknown"
            local['recognition']=result
        except (ProviderError,ValueError) as error:
            assessment.update(decision="unknown",reason=type(error).__name__,error=str(error)[:500])
            local['unknowns'].append(f"影像{asset['id']}未形成有效识别，不能按未发现处理")
        local['assessments'].append(assessment)
        pipeline.event(run_id,"image_assessment",assessment["decision"],{"phase":phase,"asset_id":asset["id"],
            "source":asset['metadata']['source'],"candidate_count":assessment["candidate_count"],
            "summary":"；".join(assessment.get('analysis_summary',[]))[:900],
            "raw_candidate_count":assessment.get('jianwei_raw_candidate_count'),
            "visual_review_status":assessment.get('optical_false_positive_review',{}).get('status')})
        return local

    limit=max(1,int(getattr(pipeline.settings,'recognition_concurrency',3) or 1))
    gate=asyncio.Semaphore(limit)
    async def _guarded(asset):
        async with gate:
            return await _read_one(asset)
    # 各图独立判读，并发执行；结果按输入顺序合并，所以与逐张串行时完全一致。
    outcomes=await asyncio.gather(*(_guarded(asset) for asset in assets))
    for local in outcomes:
        if local is None:continue
        result=local['recognition']
        if result is not None:
            offset=len(combined.observations)
            combined.observations.extend(result.observations)
            for candidate in result.candidates:
                candidate.evidence_indices=[index+offset for index in candidate.evidence_indices]
                combined.candidates.append(candidate)
            combined.quality_issues.extend(result.quality_issues)
            combined.unknowns.extend(result.unknowns)
        combined.unknowns.extend(local['unknowns'])
        native_results.extend(local['native']);assessments.extend(local['assessments']);traces.extend(local['traces'])
    return combined,native_results,assessments,traces

def merge_recognition(target, extra):
    offset=len(target.observations)
    target.observations.extend(extra.observations)
    for candidate in extra.candidates:
        candidate.evidence_indices=[index+offset for index in candidate.evidence_indices]
        target.candidates.append(candidate)
    target.quality_issues.extend(extra.quality_issues);target.unknowns.extend(extra.unknowns)


def replace_asset_recognition(target,extra,asset_ids):
    """Replace re-read images without duplicating observations or stale candidates."""
    identities=set(asset_ids);mapping={};observations=[]
    for index,observation in enumerate(target.observations):
        if observation.asset_id in identities:continue
        mapping[index]=len(observations);observations.append(observation)
    candidates=[]
    for candidate in target.candidates:
        if all(index in mapping for index in candidate.evidence_indices):
            candidate=candidate.model_copy(deep=True)
            candidate.evidence_indices=[mapping[index] for index in candidate.evidence_indices];candidates.append(candidate)
    target.observations=observations;target.candidates=candidates
    target.unknowns=[item for item in target.unknowns if not any(identity in item for identity in identities)]
    merge_recognition(target,extra)


async def refresh_registered_assets(pipeline,assets,follow,assessments,already_refreshed):
    """Metadata-only refresh; return existing images newly eligible for re-reading."""
    if not hasattr(pipeline,'refresh_spatial_evidence'):return assets,follow,[]
    before={asset['id']:spatial_eligible(asset) for asset in assets}
    refreshed=await pipeline.refresh_spatial_evidence([*assets,*follow])
    if not isinstance(refreshed,list):return assets,follow,[]
    by_id={asset['id']:asset for asset in refreshed}
    assets=[by_id.get(asset['id'],asset) for asset in assets];follow=[by_id.get(asset['id'],asset) for asset in follow]
    unknown={row['asset_id'] for row in assessments if row.get('reason')=='spatial_registration_unverified'}
    reconsider=[asset for asset in assets if asset['id'] in unknown and asset['id'] not in already_refreshed
                and not before[asset['id']] and spatial_eligible(asset)]
    return assets,follow,reconsider


def _crop_candidate(pipeline, recognition, assets, preferred_asset_id=None, preferred_pixel_window=None):
    by_id={a["id"]:a for a in assets}
    from .crops import crop_from_store
    if preferred_pixel_window is not None:
        if preferred_asset_id not in by_id:
            return None
        return crop_from_store(pipeline.store, preferred_asset_id, preferred_pixel_window, padding=.35)
    for candidate in recognition.candidates:
        for index in candidate.evidence_indices:
            obs=recognition.observations[index]
            if preferred_asset_id and obs.asset_id!=preferred_asset_id:continue
            if obs.pixel_window and obs.asset_id in by_id:
                return crop_from_store(pipeline.store, obs.asset_id, obs.pixel_window, padding=.35)
    return None


def evidence_action_key(action, asset_id=None, pixel_window=None):
    """A detail review belongs to an object window, never to an action quota."""
    if action == 'local_crop' and asset_id and pixel_window:
        return f"local_crop:{asset_id}:" + ','.join(map(str, pixel_window))
    return action


def requalify_registered_assessments(assessments, reconsider):
    """Adopt new registration eligibility without rereading identical pixels."""
    by_id = {asset['id']: asset for asset in reconsider}
    changed = []
    for row in assessments:
        asset = by_id.get(row['asset_id'])
        if not asset or not spatial_eligible(asset) or row.get('reason') != 'spatial_registration_unverified':
            continue
        semantic = row.get('image_only_decision')
        if semantic not in {'positive', 'negative', 'unknown'}:
            continue
        row.update(decision=semantic, same_point_eligible=True, spatial_status='verified',
                   semantic_result_reused=True, semantic_pixels_unchanged=True,
                   target_relevant=asset['metadata'].get('role') not in {'full_scene', 'local_context'}
                       and min(asset['width'], asset['height']) >= 64 and asset.get('analysis_scope') != 'background_only')
        row.pop('reason', None)
        row['analysis_summary'] = [text for text in row.get('analysis_summary', [])
                                  if not text.startswith('本图仅作图内场景筛查；尚未核验')]
        changed.append(row['asset_id'])
    return changed


async def recognize_detail_batch(pipeline, run_id, task, assets, quality, *, context_assets=None):
    """Review distinct objects with two bounded workers and deterministic output."""
    semaphore = asyncio.Semaphore(2)
    async def one(asset):
        async with semaphore:
            return await recognize(pipeline, run_id, task, [asset], quality, phase='coverage_followup',context_assets=context_assets)
    results = await asyncio.gather(*(one(asset) for asset in assets))
    combined = Recognition(); native = []; assessments = []; traces = []
    for extra, extra_native, extra_assessments, extra_traces in results:
        merge_recognition(combined, extra)
        native.extend(extra_native); assessments.extend(extra_assessments); traces.extend(extra_traces)
    return combined, native, assessments, traces


def coverage_decisions(task,assets,recognition,assessments,visual_review,base_view=None):
    """Explain a bounded next action from image-specific evidence, not source count."""
    by_id={asset['id']:asset for asset in assets};decisions=[]
    def add(identity,issue,action,reason,**extra):
        if identity not in by_id:return
        asset=by_id[identity]
        if asset.get('analysis_scope')=='background_only' and action in {'local_crop','detail_view'}:
            action='none';reason='原生像元不足；显示放大或裁图不会增加传感器细节，只保留背景参考'
        if action == 'local_crop' and asset.get('derived_from_asset_id'):
            return  # Do not recursively crop a crop; retain its unresolved review.
        if action=='local_crop' and extra.get('pixel_window'):
            extra['evidence_labels']=[f'E{i+1}' for i,obs in enumerate(recognition.observations)
                if obs.asset_id==identity and obs.pixel_window and list(obs.pixel_window)==list(extra['pixel_window'])]
        if not any(evidence_action_key(row['action'], row['asset_id'], row.get('pixel_window')) ==
                   evidence_action_key(action, identity, extra.get('pixel_window')) for row in decisions):
            decisions.append({'asset_id':identity,'source':asset['metadata']['source'],'issue':issue,'action':action,
                              'reason':reason,'independent_evidence':False,'status':'planned' if action!='none' else 'informational',**extra})
    for row in assessments:
        identity=row['asset_id'];asset=by_id.get(identity,{})
        same_image_review = row.get('optical_false_positive_review') or {}
        if same_image_review.get('status') == 'completed' and asset.get('analysis_scope') != 'background_only':
            for reviewed in same_image_review.get('candidates', []):
                if reviewed.get('effective_status') != 'uncertain':
                    continue
                for box in reviewed.get('original_pixel_windows') or []:
                    if (isinstance(box, (list, tuple)) and len(box) == 4 and all(type(v) is int for v in box)
                            and 0 <= box[0] < box[2] <= asset.get('width', 0)
                            and 0 <= box[1] < box[3] <= asset.get('height', 0)):
                        add(identity, 'uncertain_candidate_detail', 'local_crop',
                            '该处边界或材质细节尚不确定；先提取原图局部交给见微再次判读，再复核混淆解释。裁图仅作图内核查，不增加原生细节或修复定位。',
                            pixel_window=list(box), candidate_index=reviewed.get('candidate_index'), image_only_review=True)
                        from .evidence import _visible_water_adjacency
                        cues=[text for text in reviewed.get('visible_evidence',[]) if isinstance(text,str) and _visible_water_adjacency(text)]
                        if cues:
                            matching=next((row for row in decisions if row['asset_id']==identity and row.get('pixel_window')==list(box)),None)
                            if matching:matching['priority_factor']='visible_water_adjacency'
                        break
        if asset.get('analysis_scope')=='background_only':
            add(identity,'native_pixels_insufficient','none',asset.get('target_pixel_sufficiency_reason','科学图仅作背景参考'))
        elif row.get('reason') in {'ProviderError','ValueError','recognizer_not_configured'}:
            add(identity,'analysis_unavailable','none','模型或结构校验未完成；新增同类影像不能自动修复识别服务')
        elif row.get('decision')=='unknown' and (row.get('quality_issues') or re.search(r'模糊|分辨率不足|云层?遮挡|无法看清|覆盖不完整|范围不足',' '.join(row.get('analysis_summary',[])))):
            add(identity,'image_unclear','detail_view','逐图判读指出可见性不足，申请一次较小显示窗口；原生分辨率仍需核验')
    for candidate in recognition.candidates:
        for index in candidate.evidence_indices:
            obs=recognition.observations[index]
            if not obs.pixel_window or obs.asset_id not in by_id:continue
            asset=by_id[obs.asset_id];left,top,right,bottom=obs.pixel_window
            if left<=asset['width']*.05 or top<=asset['height']*.05 or right>=asset['width']*.95 or bottom>=asset['height']*.95:
                add(obs.asset_id,'candidate_near_image_edge','expand_context','候选接近图边，需查看被截断的周边；仅围绕任务点扩大，不反算候选地理坐标')
            elif min(right-left,bottom-top)<64:
                add(obs.asset_id,'small_candidate_pixel_window','local_crop','已有候选像素较小，提取原图局部便于复核；裁切不增加独立来源或原生细节',pixel_window=list(obs.pixel_window))
    for request in visual_review.get('coverage_requests',[]):
        add(request['asset_id'],'visual_review_request',request['action'],request['reason'])
    if base_view and base_view.get('requested_width_m',900)<900 and any(kind in {'environmental_context','surrounding_relationship'} for kind in task.task_types):
        asset=next((a for a in assets if a['metadata']['modality']=='optical' and a.get('acquisition_phase')=='baseline'),None)
        if asset:add(asset['id'],'task_requires_surroundings','expand_context','环境或周边关系任务需要场地外上下文，初始同点小范围不足以覆盖周边')
    priority={'expand_context':0,'same_coverage_alternate':1,'detail_view':2,'local_crop':3,'none':4}
    return sorted(decisions,key=lambda row:-2 if row.get('priority_factor')=='visible_water_adjacency' else
                  -1 if row.get('issue')=='uncertain_candidate_detail' else priority.get(row['action'],4))


async def _visual_review(pipeline,task,assets,recognition,traces,*,run_id=None):
    from .pipeline import VisualCrossReview
    # User-authorized Gemini checks optical confusers as well as scientific
    # context. DeepSeek remains text-only; another image's non-detection still
    # cannot erase a same-image candidate without the explicit object review.
    # The detector never cross-reviews its own candidates: without an
    # independent image-capable reviewer this optional step is skipped.
    recognizer=pipeline.settings.models.get('science_analyst')
    if not recognizer or not recognizer.ready or 'image' not in recognizer.input_modalities:
        return {'status':'not_configured_for_images','reviewer_role':'science_analyst',
                'reason':'independent_reviewer_not_configured'}
    # This optional cross-image summary must not stall an already reviewed
    # candidate for the scientific role's much longer native-window timeout.
    recognizer = replace(recognizer, timeout=min(recognizer.timeout, getattr(pipeline.settings,'cross_review_timeout',45)))
    started=time.monotonic()
    if run_id:
        pipeline.event(run_id,'visual_cross_review','running',{'asset_ids':[a['id'] for a in assets],
            'timeout_seconds':recognizer.timeout,'summary':'逐图复核已保留；现在仅补查跨图对应和分歧，不会用其他图未检出抹去疑点。'})
    try:
        inspected=assets[:pipeline.settings.max_assets]
        result,trace=await pipeline.models.complete(recognizer,
            '对已取得的同点多源影像与候选做一次简短视觉交叉复核，先检查是否同目标、可见性及主要混淆。'
            'assessment不超过350个中文字，limitations最多3条；正文用图1、图2，不写UUID、字段名、流程或模型分工。'
            'evidence_asset_ids仍用原ID。只有可见同名特征支持同一目标才same_target_supported=true；实际审查主要混淆才alternative_explanations_reviewed=true。'
            '按用户目标和已见证据决定是否需要补证。若单幅图已足够回答，应说明主要混淆后选择none，不为凑多源调用工具。'
            '如某图覆盖不足、候选靠边或局部不清，可在coverage_requests最多3项引用asset_id，选择expand_context/detail_view/local_crop/same_coverage_alternate。'
            '只有明确的地表光谱问题、散射/遮挡补证需求或真实前后时相缺口，才分别选择multispectral/sar/timeseries并说明具体原因；没有必要选none。'
            '不要提供新坐标或bbox，由程序按预算计算；科学RGB背景不能替代小目标材料识别，放大不增加原生细节。'
            '请按本次JSON schema返回结构化复核；此前的XML逐图初筛不能代替本次跨图复核。'
            '其他图未检出不能取消当前图正疑点。同图明显误报的排除仅采用另行记录的逐候选独立复核，本次不得临时删除候选。'
            '同图裁切不是独立来源，不输出内部推理或未经计算的测量。'+MODEL_METADATA_RULE,
            {'task':task.model_dump(mode='json'),'recognition':recognition.model_dump(),'assets':[model_asset_context(a) for a in inspected]},
            VisualCrossReview,image_parts(pipeline,inspected))
        allowed={a['id'] for a in inspected}
        if not set(result.evidence_asset_ids)<=allowed or any(row.asset_id not in allowed for row in result.coverage_requests):raise ValueError('视觉复核引用未知资产')
        cleaned,removed_claims=sanitize_claims(result.model_dump(mode='json'))
        cleaned['assessment']=cleaned['assessment'] or '跨图复核摘要含未经核验表述，已删除；结构化结论保留。'
        result=VisualCrossReview.model_validate(cleaned)
        trace['purpose']='optical_cross_image_context_review';traces.append(trace)
        if run_id:pipeline.event(run_id,'visual_cross_review','completed',{'summary':result.assessment,'elapsed_seconds':round(time.monotonic()-started,3)})
        return {'status':'completed',**result.model_dump(),'reviewer_role':recognizer.role,
                'can_override_positive_candidate':False,'reviewed_before_additional_acquisition':True,
                'removed_unsupported_sentences':removed_claims}
    except (ProviderError,ValueError) as error:
        if run_id:pipeline.event(run_id,'visual_cross_review','unavailable',{'failure_kind':getattr(error,'failure_kind','invalid_response'),
            'elapsed_seconds':round(time.monotonic()-started,3),'summary':'跨图补充复核未返回有效结果，已有逐图判读与未排除候选继续保留，转入下一项必要核查。'})
        return {'status':'unavailable','reviewer_role':recognizer.role,'reason':str(error)[:250]}


def spatial_prerequisites(task,assets,assessments,*,available_sources=(),attempted_sources=()):
    """Reference availability is a prerequisite, not a source-count heuristic."""
    visual=[asset for asset in assets if asset.get('metadata',{}).get('modality')=='optical']
    unverified={row['asset_id'] for row in assessments if row.get('reason')=='spatial_registration_unverified'}
    needs=bool(unverified)
    # Current local runtime can register providers against an Esri worker image.
    # A provider name or an arbitrary unknown image is not itself a usable anchor.
    reference=any(spatial_eligible(asset) or (source_family(asset['metadata'].get('source'))=='esri_mosaic'
        and re.fullmatch(r'[0-9a-f]{32}',str(asset.get('worker_image_id','')))) for asset in visual)
    attempted=set(attempted_sources);available=set(available_sources)
    candidate=next((source for source in ('arcgis','esri','esri_world_imagery') if source in available and source not in attempted),None)
    used_families={source_family(asset['metadata'].get('source')) for asset in visual};used_families.discard(None)
    alternate=next((source for source in available_sources if source not in attempted
        and (source_family(source) is None or source_family(source) not in used_families)),None) if reference else None
    independent_aoi=task.coordinate_system=='wgs84' and bool(task.center or task.bbox)
    return {'spatial_correspondence_unresolved':needs,'reference_available':reference,'reference_candidate':candidate,
        'alternate_candidate':alternate,
        'can_attempt_reference_recovery':bool(task.acquire_optical and (alternate if reference else candidate)),
        'unverified_asset_ids':sorted(unverified),'optical_same_point_supported':any(spatial_eligible(asset) for asset in visual),
        'independent_task_aoi_available':independent_aoi,
        'scientific_scope':'declared_WGS84_task_AOI_not_optical_candidate_geolocation' if independent_aoi else 'unknown_task_georeference',
        'reason':'已有可用参考，可用尚未请求的另一图源尝试相对配准' if reference and alternate else
                 '已有参考但适用图源均已请求，不能重复同来源取图修复空间缺口' if reference else '可尝试尚未请求的Esri参考影像' if candidate else
                 '没有可用参考且本轮没有可再尝试的Esri来源；其他未知底图不能自行建立同点证据'}
