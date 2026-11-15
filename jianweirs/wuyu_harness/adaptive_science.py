"""科学影像（多光谱/SAR）窗口读取与场景查询（adaptive 的叶子模块）。"""
from __future__ import annotations

from datetime import datetime,timedelta,timezone


from .data import DataClientError


def acquisition_view(asset, assets):
    """Follow a local crop back to its requested frame, never geolocate pixels."""
    by_id = {item['id']: item for item in assets}
    seen = set()
    while asset and asset.get('id') not in seen:
        seen.add(asset.get('id'))
        view = asset.get('requested_view')
        if isinstance(view, dict) and view.get('bbox'):
            return view
        asset = by_id.get(asset.get('derived_from_asset_id'))
    return None


async def _science_window(pipeline, run_id, task, group, source, scene, bbox):
    from .science import acquire_science_window
    return await acquire_science_window(pipeline,run_id,task,group,source,scene,bbox)


async def _analyze_science_with_events(pipeline,run_id,task,assets,evidence,traces,*,context_assets=None):
    from .science import analyze_science
    details={'source':evidence.get('source','user_upload'),'asset_ids':[a['id'] for a in assets]}
    config=pipeline.settings.models.get('science_analyst')
    if config and config.ready and 'image' in config.input_modalities and assets:
        pipeline.event(run_id,'science_analysis','running',details)
    result=await analyze_science(pipeline,task,assets,evidence,traces,context_assets=context_assets)
    pipeline.event(run_id,'science_analysis',result.get('status','unavailable'),
        {**details,'summary':str(result.get('summary') or '本次科学影像未形成有效分析，保留未知。')[:900],
         'science_pixels_analyzed':result.get('science_pixels_analyzed') is True})
    return result


async def science_subset(pipeline, run_id, task, group, source, scenes, bbox, *, max_attempts=2):
    """Try distinct scenes within a fixed budget; never replay an accepted job.

    A transient scene failure is not a source-wide coverage or permission result.
    Each scene has its own idempotency key so gateway retries cannot collide.
    """
    if source not in {"sentinel2","sentinel1-rtc"} or not scenes or max_attempts<1:
        return None,{"status":"not_attempted","reason":"no_supported_candidate_subset","attempts":[]}
    attempts=[];seen=set();last_evidence={}
    for scene in scenes:
        identity=scene.get("id")
        if not isinstance(identity,str) or not identity or identity in seen:continue
        if len(attempts)>=min(2,max_attempts):break
        seen.add(identity)
        try:
            asset,evidence=await _science_window(pipeline,run_id,task,group,source,scene,bbox)
            last_evidence=evidence
            attempts.append({"scene_id":identity,"status":evidence["status"]})
            if asset or evidence['status']=='science_window_downloaded':
                return asset,{**evidence,"attempts":attempts,"fallback_scene_used":len(attempts)>1}
            if len(attempts)>=min(2,max_attempts):
                return asset,{**evidence,"attempts":attempts,"fallback_scene_used":len(attempts)>1}
        except (DataClientError,ValueError,OSError,KeyError) as error:
            code=error.code if isinstance(error,DataClientError) else "invalid_science_window" if isinstance(error,(ValueError,OSError)) else "invalid_scene_metadata"
            attempts.append({"scene_id":identity,"status":"unavailable","reason":code})
            if code in {"data_deadline_exceeded","source_cached_blocked","provider_authentication_required","unsupported_operation",
                        "source_group_mismatch","invalid_parameters","unsupported_reflectance_band","rasterio_missing"}:
                break
    return None,{**last_evidence,"status":"unavailable","source":source,"reason":attempts[-1].get("reason","no_usable_science_window") if attempts else "no_valid_scene_identifier",
                 "attempts":attempts,"source_globally_unavailable":False}


async def _query_s2_science(pipeline,run_id,task,rank,bbox,strategy,counters,*,context_assets=None):
    from .science import probe_s2_scene,rank_science_scenes,scene_search_periods
    source='sentinel2';group='multispectral';budget=strategy['science_budget']
    end=task.date_end or datetime.now(timezone.utc).date();start=task.date_start or end-timedelta(days=90)
    temporal=rank.get('action')=='timeseries';periods=scene_search_periods(start,end,temporal=temporal)
    remaining_queries=max(0,budget['max_catalog_queries']-counters['queries'])
    if temporal and remaining_queries==1:periods=periods[-1:]
    item={'group':group,'source':source,'operation':'multispectral.search','bbox':bbox,'time_range':f'{start}/{end}',
          'science_pixels_analyzed':False,'selection_reason':rank.get('reason'),'trigger_action':rank.get('action'),
          'spatial_basis':rank.get('spatial_basis'),
          'time_basis':'user_requested' if task.date_start and task.date_end else 'recent_90_days_context_not_historical_substitution',
          'catalog_complete':False,'full_history_complete':False,'catalog_queries':[],'quality_probes':[],
          'temporal_coverage':[],'status':'catalog_only','limitation':'目录是有限候选；场景云量仅用于排序，点位是否可用必须经过实际SCL筛查'}
    acquired=[];windows=[];seen=set();all_scenes={};counters.setdefault('quality_probes',0)
    target_count=min(rank.get('max_scenes',2),2 if temporal else 1)
    if temporal and target_count==1:periods=periods[-1:]
    for period in periods:
        if counters['queries']>=budget['max_catalog_queries'] or len(acquired)>=target_count:break
        period_text=f"{period['start']}/{period['end']}";query={'source':source,'bbox':bbox,'datetime':period_text,'limit':min(12,budget.get('catalog_limit',12))}
        progress={'source':source,'group':group,'operation':'multispectral.search','period':period['label']}
        counters['queries']+=1;pipeline.event(run_id,'science_query','running',progress)
        try:
            result=await pipeline.data.execute('multispectral.search',query);scenes=result.get('scenes')
            if not isinstance(scenes,list):raise ValueError('invalid_scene_catalog')
            scenes=[scene for scene in scenes if isinstance(scene,dict) and isinstance(scene.get('id'),str)]
            if temporal:
                def in_period(scene):
                    try:acquired=datetime.fromisoformat(str(scene.get('acquired_at')).replace('Z','+00:00')).date()
                    except ValueError:return False
                    return period['start']<=acquired<=period['end']
                scenes=[scene for scene in scenes if in_period(scene)]
            for scene in scenes:all_scenes[scene['id']]=scene
            item['catalog_queries'].append({'period':period['label'],'time_range':period_text,'returned':len(scenes),'limit':query['limit'],
                'more_available':bool(result.get('more_available') or result.get('results_limited')),'full_history_complete':False})
            pipeline.event(run_id,'science_query','completed',{**progress,'scene_count':len(scenes)})
        except (DataClientError,ValueError) as error:
            code=error.code if isinstance(error,DataClientError) else 'invalid_catalog'
            item['catalog_queries'].append({'period':period['label'],'time_range':period_text,'status':'unavailable','reason':code})
            pipeline.event(run_id,'science_query','unavailable',{**progress,'reason':code});continue
        candidates=rank_science_scenes(scenes,anchor=period['start'] if period['label']=='early_endpoint' else period['end'])
        period_attempts=0;selected=None
        for scene in candidates:
            if scene['id'] in seen:continue
            if counters['quality_probes']>=budget.get('max_quality_probes',6):break
            if temporal and period_attempts>=budget.get('max_quality_probes_per_period',3):break
            if counters['subsets']>=budget['max_subset_attempts']:break
            seen.add(scene['id']);period_attempts+=1;counters['quality_probes']+=1
            pipeline.event(run_id,'science_quality_probe','running',{**progress,'scene_id':scene['id']})
            try:
                probe=await probe_s2_scene(pipeline,run_id,task,scene,bbox)
            except (DataClientError,ValueError,OSError) as error:
                probe={'scene_id':scene['id'],'scene_acquired_at':scene.get('acquired_at'),'status':'quality_rejected',
                       'quality_screening':{'status':'unknown','usable':False,'reason':error.code if isinstance(error,DataClientError) else type(error).__name__}}
            probe['period']=period['label'];item['quality_probes'].append(probe)
            pipeline.event(run_id,'science_quality_probe','completed' if probe['status']=='usable' else 'unavailable',
                           {**progress,'scene_id':scene['id'],'quality_status':probe['quality_screening']['status'],'reason':probe['quality_screening'].get('reason')})
            if probe['status']!='usable':continue
            pipeline.event(run_id,'science_subset','running',{**progress,'scene_id':scene['id']})
            asset,subset=await science_subset(pipeline,run_id,task,group,source,[scene],bbox,max_attempts=1)
            counters['subsets']+=len(subset.get('attempts',[]));windows.append(subset)
            pipeline.event(run_id,'science_subset','completed' if asset else 'unavailable',
                {**progress,'scene_id':scene['id'],'result_status':subset['status'],'reason':subset.get('reason')})
            if asset:
                acquired.append(asset);selected=scene.get('acquired_at');break
            if subset.get('reason') in {'data_deadline_exceeded','source_cached_blocked','provider_authentication_required'}:break
        item['temporal_coverage'].append({'period':period['label'],'requested_time_range':period_text,'selected_acquired_at':selected,
                                          'status':'sample_acquired' if selected else 'no_usable_sample_within_budget','full_history_complete':False})
    item.update(scene_count=len(all_scenes),scenes=[{key:scene.get(key) for key in ('id','acquired_at','bbox','source','cloud_cover')} for scene in all_scenes.values()],subsets=windows)
    if windows:item['subset']=windows[0]
    if acquired:
        item['analysis']=await _analyze_science_with_events(pipeline,run_id,task,acquired,{'source':source,'windows':windows,'spatial_basis':item.get('spatial_basis'),**windows[0]},item.setdefault('_traces',[]),context_assets=context_assets)
        item['science_pixels_analyzed']=item['analysis'].get('science_pixels_analyzed',False)
        item['status']='analyzed' if item['science_pixels_analyzed'] else 'downloaded_unanalyzed'
    else:
        item.update(status='unavailable',reason='no_locally_usable_scene_within_probe_budget',
            unknown='有限候选中未获得通过点位SCL筛查的科学影像，不能按未见变化或没有固废处理')
    return acquired,item


async def _query_science(pipeline,run_id,task,rank,bbox,strategy,counters,*,context_assets=None):
    if rank['source']=='sentinel2' and rank['group']=='multispectral':
        return await _query_s2_science(pipeline,run_id,task,rank,bbox,strategy,counters,context_assets=context_assets)
    source,group=rank['source'],rank['group']
    end=task.date_end or datetime.now(timezone.utc).date();start=task.date_start or end-timedelta(days=90)
    period=f'{start}/{end}';query={'source':source,'bbox':bbox,'limit':min(12,strategy['science_budget'].get('catalog_limit',12))}
    query.update({'start':str(start)+'T00:00:00Z','end':str(end)+'T23:59:59Z'} if group=='sar' else {'datetime':period})
    item={'group':group,'source':source,'operation':group+'.search','bbox':bbox,'time_range':period,
          'science_pixels_analyzed':False,'selection_reason':rank.get('reason'),'trigger_action':rank.get('action'),
          'spatial_basis':rank.get('spatial_basis'),
          'time_basis':'user_requested' if task.date_start and task.date_end else 'recent_90_days_context_not_historical_substitution'}
    acquired=[];counters['queries']+=1;progress={'source':source,'group':group,'operation':group+'.search'}
    pipeline.event(run_id,'science_query','running',progress)
    try:
        result=await pipeline.data.execute(group+'.search',query);scenes=result.get('items' if group=='sar' else 'scenes')
        if not isinstance(scenes,list):raise ValueError('invalid_scene_catalog')
        scenes=list({scene['id']:scene for scene in scenes if isinstance(scene,dict) and scene.get('id')}.values())
        item.update(status='catalog_only',scene_count=len(scenes),catalog_complete=False,
                    scenes=[{key:scene.get(key) for key in ('id','acquired_at','bbox','source')} for scene in scenes])
        pipeline.event(run_id,'science_query','completed',{**progress,'scene_count':len(scenes)})
        windows=[];limit=min(rank.get('max_scenes',2),2) if rank.get('action')=='timeseries' else 2
        for scene in scenes[:limit]:
            if counters['subsets']>=strategy['science_budget']['max_subset_attempts']:break
            pipeline.event(run_id,'science_subset','running',{**progress,'scene_id':scene['id']})
            asset,subset=await science_subset(pipeline,run_id,task,group,source,[scene],bbox,max_attempts=1)
            counters['subsets']+=len(subset.get('attempts',[]));windows.append(subset)
            if asset:acquired.append(asset)
            pipeline.event(run_id,'science_subset','completed' if asset else 'unavailable',
                {**progress,'scene_id':scene['id'],'result_status':subset['status'],'reason':subset.get('reason')})
            if subset.get('reason') in {'data_deadline_exceeded','source_cached_blocked','provider_authentication_required'}:break
            if acquired and rank.get('action')!='timeseries':break
        item['subsets']=windows
        if windows:item['subset']=windows[0]
        if acquired:
            item['analysis']=await _analyze_science_with_events(pipeline,run_id,task,acquired,{'source':source,'windows':windows,'spatial_basis':item.get('spatial_basis'),**windows[0]},item.setdefault('_traces',[]),context_assets=context_assets)
            item['science_pixels_analyzed']=item['analysis'].get('science_pixels_analyzed',False)
            item['status']='analyzed' if item['science_pixels_analyzed'] else 'downloaded_unanalyzed'
        elif scenes:item['status']='unavailable'
    except (DataClientError,ValueError) as error:
        item.update(status='unavailable',reason=error.code if isinstance(error,DataClientError) else 'invalid_catalog')
        pipeline.event(run_id,'science_query','unavailable',{**progress,'reason':item['reason']})
    return acquired,item
