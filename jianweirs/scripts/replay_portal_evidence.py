"""Re-validate saved native answers against registered images, without model calls.

The input package is never overwritten. The caller must refresh downstream model
reviews before publishing a revised report; their old status is explicitly reset.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from omap_api.image_quality import validate_optical_preview
from wuyu_harness.adaptive import merge_recognition
from wuyu_harness.evidence import assess_quality, validate_recognition
from wuyu_harness.fusion import fuse_findings
from wuyu_harness.rs_xml import parse_rs_xml
from wuyu_harness.schemas import Recognition, TaskRequest
from wuyu_harness.store import Store, now
from wuyu_harness.strategy import source_family, task_strategy


def replay_native_evidence(package: dict, run_dir: Path, store: Store, *, science_verification: dict | None = None) -> dict:
    """Return a fresh package with only validated answer/image evidence adopted."""
    revised=copy.deepcopy(package)
    task=TaskRequest.model_validate(package['task'])
    assets=[store.get(asset['id'],'image') for asset in package.get('assets',[])]
    requests={}
    for path in sorted((run_dir/'model-calls').glob('*-request.json'),key=lambda item:item.stat().st_mtime):
        request=json.loads(path.read_text(encoding='utf-8'))
        if request.get('role')!='recognizer':continue
        text=request.get('user_text','')
        try:
            context,_=json.JSONDecoder().raw_decode(text[text.index('{'):])
        except (ValueError,TypeError):continue
        identity=(context.get('asset') or {}).get('id') or context.get('asset_id')
        if identity:requests[identity]=(path,request,context)
    previous={row['asset_id']:row for row in package.get('image_assessments',[])}
    combined=Recognition();native_results=[];assessments=[];audit_ids=[]
    calls=revised.setdefault('model_calls',[])
    for asset in assets:
        if asset['metadata']['modality']!='optical' and not asset.get('visual_preview'):continue
        identity=asset['id']
        row={**previous.get(identity,{}),'asset_id':identity,'source':asset['metadata']['source'],
             'source_group':source_family(asset['metadata']['source']),'decision':'unknown','candidate_count':0,
             'quality_passed':False,'revalidated_offline':True}
        row.pop('reason',None);row.pop('error',None)
        row.setdefault('target_relevant',asset['metadata']['role'] not in {'full_scene','local_context'} and min(asset['width'],asset['height'])>=64)
        try:
            content=store.asset_path(asset).read_bytes()
            digest=hashlib.sha256(content).hexdigest()
            if digest!=asset['sha256']:raise ValueError('registered_image_hash_mismatch')
            if not validate_optical_preview(content)['valid']:raise ValueError('non_image_provider_tile')
            row['quality_passed']=True
            if identity not in requests:raise ValueError('native_audit_missing')
            path,request,context=requests[identity]
            hashes=[part['sha256'] for part in request.get('image_inputs',[]) if isinstance(part,dict) and part.get('sha256')]
            if hashes!=[digest]:raise ValueError('native_audit_image_mismatch')
            response_path=path.with_name(path.name.replace('-request.json','-response.txt'))
            raw=response_path.read_bytes();response=json.loads(raw)
            choice=response['choices'][0]
            if choice.get('finish_reason')=='length':raise ValueError('truncated_native_response')
            parsed=parse_rs_xml(choice['message']['content'],asset_id=identity,width=asset['width'],height=asset['height'])
            validate_recognition(parsed.recognition,[asset])
            merge_recognition(combined,parsed.recognition);native_results.append(parsed.native)
            row.update(decision='positive' if parsed.recognition.candidates else 'negative' if parsed.native['decision']=='未发现固废堆存' else 'unknown',
                       phase=context.get('phase',row.get('phase','offline_replay')),candidate_count=len(parsed.recognition.candidates),
                       analysis_summary=[obs.statement for obs in parsed.recognition.observations if obs.pixel_window is None][:2],quality_issues=[])
            audit_id=path.name.removesuffix('-request.json');audit_ids.append(audit_id)
            if not any(call.get('audit_id')==audit_id for call in calls):
                calls.append({'role':'recognizer','model':request.get('model'),'audit_id':audit_id,
                              'response_sha256':hashlib.sha256(raw).hexdigest(),'response_id':response.get('id'),
                              'usage':response.get('usage',{}),'image_input_count':1,'adopted_after_offline_revalidation':True})
        except (ValueError,KeyError,OSError,TypeError,IndexError) as error:
            row.update(decision='unknown',reason='offline_validation_failed',error=str(error)[:250])
            combined.unknowns.append(f'影像{identity}未通过离线重新核验，不能按未发现处理')
        assessments.append(row)
    strategy=revised.get('strategy') or task_strategy(task)
    adaptive=revised.setdefault('adaptive_review',{})
    prior_review=adaptive.get('main_visual_review',{})
    adaptive['main_visual_review']={'status':'needs_refresh','previous_status':prior_review.get('status'),
                                    'reason':'structured_evidence_revalidated_offline'}
    revised.update(recognition=combined.model_dump(),native_rs_results=native_results,image_assessments=assessments,
                   quality=assess_quality(task,assets),recognition_status='completed' if any(row['decision']!='unknown' for row in assessments) else 'needs_more_data',
                   fusion=fuse_findings(task,assets,assessments,strategy,adaptive['main_visual_review']),
                   narrative_status='needs_refresh',specialist_review={'status':'needs_refresh'},status='revision_pending_synthesis')
    revised['offline_replay']={'checked_at':now(),'mode':'native_answer_and_registered_image_revalidation',
                               'audit_ids':audit_ids,'remote_model_calls':0,'downstream_reviews_require_refresh':True}
    if science_verification and science_verification.get('status')=='passed':
        metadata=science_verification.get('metadata',{})
        for item in revised.get('scientific_evidence',[]):
            if metadata.get('source')!=item.get('source') or metadata.get('scene_id') not in {scene.get('id') for scene in item.get('scenes',[])}:continue
            if not metadata.get('bbox_wgs84') or not item.get('bbox'):continue
            west,south,east,north=item['bbox'];lon,lat=(west+east)/2,(south+north)/2
            dx,dy=min((east-west)/2,.005),min((north-south)/2,.005)
            expected=[lon-dx,lat-dy,lon+dx,lat+dy]
            if any(abs(a-b)>1e-8 for a,b in zip(expected,metadata['bbox_wgs84'])):continue
            item['subsequent_verification']={'status':'same_parameters_later_download_passed','scene_id':metadata['scene_id'],
                'retrieved_at':metadata.get('retrieved_at'),'bytes':science_verification.get('bytes'),
                'dimensions':[metadata.get('width'),metadata.get('height')],'band_count':metadata.get('band_count'),
                'valid_pixel_fraction_per_band':metadata.get('valid_pixel_fraction_per_band'),
                'adopted_in_original_recognition':False,
                'meaning':'同参数后续裁图成功，说明原失败可能为暂态读取故障；不追认原任务已读取或识别此科学影像'}
    return revised


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--science-verification',type=Path)
    args=parser.parse_args()
    source=args.package.resolve()
    if args.output.resolve()==source:parser.error('输出必须是新的修订文件，不能覆盖原包')
    store=Store(source.parents[2])
    verification=json.loads(args.science_verification.read_text(encoding='utf-8')) if args.science_verification else None
    revised=replay_native_evidence(json.loads(source.read_text(encoding='utf-8')),source.parent,store,science_verification=verification)
    args.output.write_text(json.dumps(revised,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output.resolve()),'assessments':len(revised['image_assessments']),'fusion':revised['fusion']['level'],'model_calls':0}))


if __name__=='__main__':main()
