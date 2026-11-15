"""Labelled real uploaded-image acceptance against the running local portal."""
from datetime import datetime, timezone
from pathlib import Path
import io
import json
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from wuyu_harness.auth_portal import PortalDB,COOKIE


def main():
    db=PortalDB(ROOT/'outputs/portal')
    user=next(u for u in db.users() if u['role']=='admin' and u['active'])
    token,csrf=db.issue_session(user['id'])
    report={'tested_at':datetime.now(timezone.utc).isoformat(),'passed':False}
    try:
        with httpx.Client(base_url='http://127.0.0.1:3080',cookies={COOKIE:token},
                headers={'Origin':'http://127.0.0.1:3080','X-CSRF-Token':csrf},trust_env=False,timeout=30) as c:
            image=ROOT/'outputs/deployment/public-verification-artifacts-236aedf1a22d/aligned-bing.png'
            if not image.is_file():raise ValueError('sample_not_found')
            archive=io.BytesIO()
            with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('imagery/test-image.png',image.read_bytes())
                z.writestr('notes/metadata.json',json.dumps({'acquisition_time':None,'coordinate_verified':False}))
            upload=c.post('/portal/api/uploads',files={'file':('labelled-acceptance.zip',archive.getvalue(),'application/zip')})
            upload.raise_for_status();document=upload.json()
            manifest=c.get('/portal/api/uploads/'+document['id']+'/archive');manifest.raise_for_status()
            members=[r['id'] for r in manifest.json()['files'] if r['import_kind']=='display_image']
            added=c.post('/portal/api/uploads/'+document['id']+'/archive/images',json={'member_ids':members});added.raise_for_status()
            assets=added.json()['assets']
            report.update(archive_uploaded=True,archive_member_count=len(manifest.json()['files']),display_images_imported=len(assets))
            body={'objective':'验收测试V3：只分析上传图中是否存在疑似堆存线索及周边环境。以见微判读为主，给重点结论和不确定性；不调用平台影像，不联网查资料。',
                'asset_ids':[a['id'] for a in assets],'document_ids':[document['id']],
                'online_supplement':'uploaded_only','acquire_optical':False,'search_policy':False,'consult_shensi':False,
                'task_types':['suspected_site_discovery']}
            check=c.post('/portal/api/conversation/prepare',json=body);check.raise_for_status()
            if not check.json()['ready']:raise ValueError('unexpected_clarification')
            reply=c.post('/portal/api/tasks',json=body);reply.raise_for_status()
            identity=reply.json()['id'];report['task_id']=identity
            print('V3_LIVE_TASK '+identity,flush=True)
            last=None;deadline=time.monotonic()+660
            while time.monotonic()<deadline:
                response=c.get('/portal/api/tasks/'+identity);response.raise_for_status();view=response.json()
                if view['status']!=last:print('V3_STATE '+view['status'],flush=True);last=view['status']
                if view['status'] not in {'planning','queued','running'}:break
                time.sleep(3)
            record=db.task(identity,user['id'])
            package_path=ROOT/'outputs/portal/engine/runs'/record.get('engine_run_id','missing')/'finding-package.json'
            package=json.loads(package_path.read_text(encoding='utf-8')) if package_path.is_file() else {}
            calls=[{'role':t.get('role'),'model':t.get('model'),'image_input_count':t.get('image_input_count',0)} for t in package.get('model_calls',[])]
            main=[x for x in calls if x['role']=='main']
            expert=[x for x in calls if x['role']=='recognizer']
            doc=c.get('/portal/api/tasks/'+identity+'/artifacts/report.docx') if view['status']=='succeeded' else None
            report.update(status=view['status'],summary=view.get('summary'),error=view.get('error'),model_calls=calls,
                main_images_zero=bool(main) and all(x['image_input_count']==0 for x in main),
                expert_received_images=any(x['image_input_count']>0 for x in expert),
                sources_acquired=[a.get('metadata',{}).get('source') for a in package.get('assets',[])],
                science_requested=bool(package.get('scientific_evidence')),
                word_downloaded=doc is not None and doc.status_code==200 and doc.content.startswith(b'PK'),
                professional_review_status=(package.get('adaptive_review') or {}).get('main_visual_review',{}).get('status'))
            report['passed']=(view['status']=='succeeded' and report['main_images_zero'] and report['expert_received_images']
                and report['word_downloaded'] and not report['science_requested']
                and set(report['sources_acquired'])<={'user_upload'})
    except Exception as exc:
        report['verification_error']=type(exc).__name__
    finally:
        db.logout(token)
        (ROOT/'outputs/workspace-v3-live-20261024.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('V3_LIVE_ACCEPTANCE '+('PASS' if report['passed'] else 'FAIL'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
