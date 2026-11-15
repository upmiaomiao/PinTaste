"""Refresh only a completed report's narrative, preserving the recorded evidence."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
import httpx
from wuyu_harness.adaptive import image_parts
from wuyu_harness.auth_portal import PortalDB
from wuyu_harness.business_narrative import REPORT_INSTRUCTION, narrative_material, final_summary
from wuyu_harness.config import Settings
from wuyu_harness.evidence import reject_unsupported_claims
from wuyu_harness.models import AUDIT_DIR,ModelClient
from wuyu_harness.portal_config import PortalConfig
from wuyu_harness.report import render_report
from wuyu_harness.schemas import TaskRequest,ReportNarrative
from wuyu_harness.store import Store,now


async def refine(portal_dir,run_id):
    load_dotenv(ROOT/'.env.wuyu',override=False)
    settings=PortalConfig(PortalDB(portal_dir),Settings.from_env()).settings()
    store=Store(portal_dir/'engine');run=store.get(run_id,'run')
    if run['status']!='succeeded':raise ValueError('Only a completed run can be refined')
    folder=store.run_dir(run_id);original=(folder/'finding-package.json').read_bytes();p=json.loads(original)
    task=TaskRequest.model_validate(p['task']);assets=[store.get(a['id'],'image') for a in p['assets']]
    documents=[store.get(identity,'document') for identity in task.document_ids]
    note='专业复核中提及的0.6米原生分辨率未在整图元数据中核实，本报告不采用该数值；裁图不增加原生细节。'
    prior_reviews=json.dumps([p.get('adaptive_review',{}).get('main_visual_review',{}),p.get('specialist_review',{})],ensure_ascii=False)
    if '0.6' in prior_reviews and any(a['metadata'].get('resolution_m') is None for a in assets) and note not in p['warnings']:
        p['warnings'].append(note)
    token=AUDIT_DIR.set(folder/'model-calls')
    try:
        async with httpx.AsyncClient() as http:
            result,trace=await ModelClient(http).complete(settings.models['main'],REPORT_INSTRUCTION,
                narrative_material(task,p['fusion'],assets,p['recognition'],p['native_rs_results'],p['image_assessments'],
                    p['adaptive_review'],p['specialist_review'],p['scientific_evidence'],p['policy'],documents,selection=p['selection']),
                ReportNarrative,image_parts(SimpleNamespace(store=store),assets) if 'image' in settings.models['main'].input_modalities else None)
        reject_unsupported_claims(result.model_dump_json())
        if not set(result.cited_policy_ids)<={s['id'] for s in p['policy']['sources']}:raise ValueError('Unknown policy reference')
        revisions=folder/'revisions';revisions.mkdir(exist_ok=True)
        (revisions/(uuid.uuid4().hex+'-finding-package.json')).write_bytes(original)
        p.update(narrative=result.model_dump(),narrative_status='completed',refined_at=now(),previous_package_sha256=hashlib.sha256(original).hexdigest())
        p['narrative']['summary']=final_summary(p['narrative']['summary'],p['fusion']['judgment'])
        p['model_calls'].append(trace)
        render_report(p,store,run_id)
        verification=ROOT/'outputs/wuyu-multisource-20261017-verification.json'
        if verification.is_file():
            v=json.loads(verification.read_text(encoding='utf-8'));v.update(narrative=p['narrative'],narrative_refined_at=p['refined_at'])
            verification.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':'passed','refreshed':'main narrative only','summary_characters':len(result.summary)},ensure_ascii=False),flush=True)
    finally:AUDIT_DIR.reset(token)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--portal-dir',type=Path,required=True);parser.add_argument('--run-id',required=True)
    args=parser.parse_args();asyncio.run(refine(args.portal_dir,args.run_id))
