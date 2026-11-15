"""Evidence ledger and a compact, traceable image comparison selection."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shutil


def publish_scientific_artifacts(records: list[dict], folder: Path) -> list[str]:
    """Expose exact run-owned files through the existing flat artifact endpoint."""
    published = []
    seen = {}
    def walk(value, parent=None):
        if isinstance(value,list):
            for item in value: walk(item,parent)
        elif isinstance(value,dict):
            name=value.get('artifact')
            inherited=parent
            if isinstance(name,str):
                candidate=(folder/name).resolve()
                if parent and (parent/name).is_file():
                    candidate=(parent/name).resolve()
                if candidate.is_relative_to(folder.resolve()) and candidate.is_file() and not candidate.is_symlink() and candidate.suffix.lower() in {'.tif','.tiff','.xml','.json'}:
                    inherited=candidate.parent
                    key=str(candidate)
                    if key not in seen:
                        fingerprint=hashlib.sha256(candidate.read_bytes()).hexdigest()[:20]
                        destination=folder/('science-'+fingerprint+candidate.suffix.lower())
                        if candidate!=destination:shutil.copyfile(candidate,destination)
                        seen[key]=destination.name;published.append(destination.name)
                    value['source_artifact']=str(candidate.relative_to(folder)).replace('\\','/')
                    value['artifact']=seen[key]
            for key,item in list(value.items()):
                if key not in {'artifact','source_artifact'}:walk(item,inherited)
    walk(records)
    return list(dict.fromkeys(published))


def spatial_state(asset: dict) -> dict:
    metadata = asset.get('metadata') or {}
    source_meta = asset.get('data_api_metadata') or {}
    return asset.get('spatial_registration') or source_meta.get('spatial_registration') or metadata.get('spatial_registration') or {}


def same_point_eligible(asset: dict) -> bool:
    state = spatial_state(asset)
    if state:
        return state.get('strict_same_point_eligible') is True and state.get('status') not in {'misaligned','unknown','failed'}
    # Legacy/user assets retain their original contract. Newly acquired optical
    # assets always carry an explicit state and cannot inherit this fallback.
    return asset.get('acquisition_phase') not in {'baseline','adaptive','coverage_same_coverage_alternate','coverage_expand_context','coverage_detail_view'}


def _scene_time(row):
    """Only registered acquisition time may order scientific comparisons."""
    try:
        value=datetime.fromisoformat(str(row.get('acquisition_time','')).replace('Z','+00:00'))
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    except (ValueError,TypeError,OverflowError):return None


def _temporal_selection(identities,rows,limit,*,seeds=()):
    identities=list(dict.fromkeys(identities));allowed=set(identities)
    selected=list(dict.fromkeys(identity for identity in seeds if identity in allowed))[:limit]
    times={identity:_scene_time(rows[identity]) for identity in identities}
    dated=sorted((identity for identity in identities if times[identity] is not None),key=lambda identity:(times[identity],identity))
    if not selected and dated:
        selected.append(dated[0])
        if limit>1 and dated[-1]!=dated[0]:selected.append(dated[-1])
    while len(selected)<min(limit,len(identities)):
        remaining=[identity for identity in identities if identity not in selected]
        known=[identity for identity in remaining if times[identity] is not None]
        chosen_times=[times[identity] for identity in selected if times[identity] is not None]
        if known and chosen_times:
            # Fill the largest unrepresented temporal interval, including a
            # missing endpoint. Neither retrieval order nor filenames are dates.
            candidate=min(known,key=lambda identity:(-min(abs(times[identity]-other) for other in chosen_times),times[identity],identity))
        else:
            candidate=min(known,key=lambda identity:(times[identity],identity)) if known else min(remaining)
        selected.append(candidate)
    return selected


def _observed_quality(statement):
    # These flags rank display usefulness only; they are not another QA test or
    # a numerical quality estimate. Preserve the original observations verbatim.
    text=str(statement)
    unobscured=bool(re.search(r'无(?:明显)?(?:云|霾|遮挡)|cloud[- ]?free|unobscured',text,re.I))
    cleaned=re.sub(r'(?:无|没有|未见|未发现|不受)(?:明显)?(?:云层?|薄云|雾霾|遮挡|大气干扰|气溶胶)(?:遮挡|覆盖|干扰|影响)?','',text)
    cleaned=re.sub(r'\b(?:no|without)\s+(?:significant\s+)?(?:cloud\s*cover|haze|obscuration)\b','',cleaned,flags=re.I)
    obscured=bool(re.search(r'不清晰|模糊|薄云|薄霾|雾|霾|遮挡|泛白|发白|对比度(?:低|低下|不足)|细节(?:退化|缺失)|清晰度(?:较差|低|不足)|大气干扰|气溶胶|haze|obscur|cloud.?cover|blur|low.?contrast',cleaned,re.I))
    clear_text=re.sub(r'(?:不|未|无法|难以|欠缺|缺乏)[^，,。；;]{0,8}清晰','',text)
    clear=unobscured or bool(re.search(r'清晰|质量(?:较佳|较好|良好|较高)|\bclear(?:er)?\b',clear_text,re.I))
    return clear and not obscured,obscured


def _science_group_selection(identities,science,assets,rows,limit=4):
    eligible=[identity for identity in identities if same_point_eligible(assets[identity])]
    allowed=set(eligible);recommendations=[];quality={identity:[False,False] for identity in eligible}
    for item in science:
        analysis=item.get('analysis') or {};source=item.get('source')
        if analysis.get('status') not in {None,'completed'}:continue
        subsets=item.get('subsets') or [item.get('subset') or {}]
        downloaded={subset.get('preview_asset_id') for subset in subsets if isinstance(subset,dict)}
        local={identity for identity in allowed & downloaded if assets[identity].get('metadata',{}).get('source')==source}
        keys=analysis.get('key_evidence_asset_ids') or []
        valid=list(dict.fromkeys(identity for identity in keys if isinstance(identity,str) and identity in local)) if isinstance(keys,list) else []
        if valid:recommendations.append(valid)
        for observation in analysis.get('observations',[]):
            identity=observation.get('asset_id')
            if identity not in local:continue
            clear,obscured=_observed_quality(observation.get('statement',''))
            quality[identity][0]|=clear;quality[identity][1]|=obscured
    if recommendations:
        # Preserve each analyst's priority, sharing the limited comparison slots
        # between sources rather than letting the first provider fill all four.
        selected=[]
        for index in range(max(map(len,recommendations))):
            for recommendation in recommendations:
                if index<len(recommendation) and recommendation[index] not in selected:selected.append(recommendation[index])
                if len(selected)>=limit:return selected,'优先展示科学分析指定的关键影像；选图仅调整展示，不增加证据权重'
        return selected,'优先展示科学分析指定的关键影像；选图仅调整展示，不增加证据权重'
    clear=[identity for identity in eligible if quality[identity][0] and not quality[identity][1]]
    obscured=[identity for identity in eligible if quality[identity][1]]
    if clear:
        selected=_temporal_selection(clear,rows,min(2,limit))
        if obscured and len(selected)<limit:
            chosen_times=[_scene_time(rows[identity]) for identity in selected if _scene_time(rows[identity]) is not None]
            dated=[identity for identity in obscured if _scene_time(rows[identity]) is not None]
            if dated and chosen_times:
                midpoint=(min(chosen_times)+max(chosen_times))/2
                contrast=min(dated,key=lambda identity:(abs(_scene_time(rows[identity])-midpoint),identity))
            else:contrast=_temporal_selection(obscured,rows,1)[0]
            selected.append(contrast)
        # More cloudy repeats cannot crowd out an observed clear baseline or a
        # useful additional date. Keep at most one explicit low-quality contrast.
        pool=list(dict.fromkeys([*selected,*[identity for identity in eligible if identity not in obscured]]))
        selected=_temporal_selection(pool,rows,limit,seeds=selected)
        selected.sort(key=lambda identity:(_scene_time(rows[identity]) is None,_scene_time(rows[identity]) or 0,identity))
        return selected,'依据明确质量观测展示清晰时相与必要的低质量对照，并补足实际日期覆盖；选图不增加证据权重'
    selected=_temporal_selection(eligible,rows,limit)
    selected.sort(key=lambda identity:(_scene_time(rows[identity]) is None,_scene_time(rows[identity]) or 0,identity))
    return selected,'缺少明确关键图建议，按已登记成像日期选择端点与中间时相；选图不增加证据权重'


def evidence_presentation(assets, recognition, assessments, science, fusion):
    observations = recognition.model_dump() if hasattr(recognition, 'model_dump') else recognition
    by_assessment = {row['asset_id']:row for row in assessments}
    science_by_id = {}
    for item in science:
        analysis = item.get('analysis') or {}
        subsets = item.get('subsets') or [item.get('subset') or {}]
        for subset in subsets:
            if subset.get('preview_asset_id'):
                science_by_id[subset['preview_asset_id']] = (item, subset, analysis)
    rows = []
    for index, asset in enumerate(assets, 1):
        identity = asset['id'];meta = asset.get('metadata') or {};api = asset.get('data_api_metadata') or {}
        item, subset, analysis = science_by_id.get(identity, ({},{},{}))
        modality = meta.get('modality','optical');group = item.get('group') or ('optical' if modality == 'optical' else modality)
        if group not in {'optical','multispectral','sar','hyperspectral'}:group=modality
        assessment = by_assessment.get(identity) or {}
        statements = [o.get('statement','') for o in (observations or {}).get('observations',[]) if o.get('asset_id') == identity]
        statements += [o.get('statement','') for o in analysis.get('observations',[]) if o.get('asset_id') == identity]
        if not statements:
            statements = assessment.get('analysis_summary') or ([analysis['summary']] if analysis.get('status') == 'completed' and analysis.get('summary') else [])
        registration = spatial_state(asset)
        parameters = {'bbox': subset.get('bbox') or asset.get('requested_view',{}).get('bbox'),
                      'bands': subset.get('bands') or asset.get('source_bands'),
                      'dimensions': subset.get('dimensions') or [asset.get('width'),asset.get('height')]}
        calibration = subset.get('calibration') or {}
        for key in ('polarization','radiometry','input_units','display_units','native_pixel_spacing_m','effective_resolution_m'):
            if calibration.get(key) is not None: parameters[key] = calibration[key]
        if registration.get('method'): parameters['registration_method'] = registration['method']
        if registration.get('scope'): parameters['registration_scope'] = registration['scope']
        rows.append({'asset_id':identity,'figure':f'图{index}','source':meta.get('source'),'group':group,
                     'acquisition_time':meta.get('acquisition_time') or subset.get('scene_acquired_at'),
                     'retrieved_at':api.get('retrieved_at'),'registered_at':asset.get('created_at'),
                     'native_resolution_m':meta.get('resolution_m'),
                     'parameters':{k:v for k,v in parameters.items() if v is not None},
                     'observation':'；'.join(dict.fromkeys(str(s) for s in statements if s)) or '尚未形成有效判读',
                     'role':'科学辅助' if group != 'optical' else '局部细节' if asset.get('derived_from_asset_id') else '高分辨率形态核查',
                     'spatial_status':registration.get('status','user_supplied' if meta.get('source','').startswith('user') else 'unverified'),
                     'spatial_registration':registration,'same_point_eligible':same_point_eligible(asset),
                     'optical_false_positive_review':assessment.get('optical_false_positive_review'),
                     'decision':assessment.get('decision','unknown'),'selected':False})
    by_id = {a['id']:a for a in assets};row_by_id = {r['asset_id']:r for r in rows}
    positives = [i for i in fusion.get('positive_asset_ids',[]) if i in by_id and same_point_eligible(by_id[i])]
    eligible = [a['id'] for a in assets if same_point_eligible(a) and a.get('metadata',{}).get('modality','optical')=='optical' and not a.get('science_preview') and not a.get('derived_from_asset_id')]
    primary = list(dict.fromkeys(positives or eligible))[:2]
    # A useful local crop may be paired with its parent without calling it a
    # second satellite or independent corroboration.
    if len(primary) == 1:
        detail = next((a['id'] for a in assets if a.get('derived_from_asset_id') == primary[0] and same_point_eligible(a)), None)
        if detail: primary.append(detail)
    groups = []
    if len(primary)>1:
        groups.append({'title':'关键高分影像对照','asset_ids':primary,'reason':'比较同一目标的形态与细节，来源数不等于独立证据数'})
    for group,label in (('multispectral','多光谱辅助'),('sar','SAR辅助'),('hyperspectral','高光谱辅助')):
        ids = [r['asset_id'] for r in rows if r['group']==group]
        if ids:
            chosen,reason=_science_group_selection(ids,science,by_id,row_by_id)
            if chosen:groups.append({'title':label,'asset_ids':chosen,'reason':reason})
    # Scientific-only answers need an explicit primary set as well; otherwise a
    # single scientific image can disappear behind an empty optical primary.
    if not primary and groups:primary=list(groups[0]['asset_ids'])
    selected_ids = set(primary)
    for g in groups: selected_ids.update(g['asset_ids'])
    for r in rows: r['selected'] = r['asset_id'] in selected_ids
    supporting = [r['asset_id'] for r in rows if r['selected'] and r['asset_id'] not in primary]
    return rows, {'mode':'comparison' if len(primary)>1 or supporting else 'single', 'primary_asset_ids':primary,
                  'supporting_asset_ids':supporting,'comparison_groups':groups,
                  'reason':'按实际回答问题的关键证据组织；未核验位置图仅供上下文，不能用于同点变化结论'}
