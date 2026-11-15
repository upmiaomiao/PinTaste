"""Labelled product acceptance with real planning, data and answer channels."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from wuyu_harness.config import Settings
from wuyu_harness.portal import PortalRuntime
from wuyu_harness.schemas import TaskRequest
from wuyu_harness.store import now


async def main():
    load_dotenv(ROOT/'.env.wuyu',override=True)
    runtime=PortalRuntime(ROOT/'outputs/portal',Settings.from_env())
    admins=[u for u in runtime.db.users() if u['role']=='admin' and u['active']]
    if len(admins)!=1:raise RuntimeError('One existing administrator required')
    user=admins[0]
    cases=[
      ('sentinel2_dates','验收测试：WGS84坐标104.461117,30.858473，2026年10月1日至22日Sentinel-2有哪些拍摄日期？只查目录可用性，不要下载、不取高分、不查其他星。','availability','sentinel2'),
      ('sar_dates','验收测试：WGS84坐标104.461117,30.858473，2026年10月1日至22日SAR哨兵1 RTC有哪些拍摄日期？只查可用性，不要高分和其他卫星，也不从雷达亮暗推断物料。','availability','sentinel1-rtc'),
      ('small_stockpile','验收测试：WGS84坐标104.461117,30.858473附近有没有疑似小堆体？先看一份高分影像及位置；足够就停止，不能确认就说明。只有确有帮助才补多光谱或SAR，不列全部卫星。','auto',None),
      ('missing_period','验收测试：WGS84坐标104.461117,30.858473，请比较清理前后有没有变化。请先确认我没有提供的关键信息。','needs_input',None),
    ]
    results=[]
    await runtime.start_engine()
    try:
        for name,question,mode,expected_source in cases:
            params={'objective':question,'task_types':['cleanup_progress' if name=='missing_period' else 'suspected_site_discovery'],
                'center':[104.461117,30.858473],'coordinate_system':'wgs84','search_policy':False,'consult_shensi':False}
            if mode=='availability':params.update(date_start='2026-10-01',date_end='2026-10-22')
            task=TaskRequest.model_validate(params)
            identity=uuid.uuid4().hex
            runtime.db.put_task({'id':identity,'user_id':user['id'],'status':'planning','created_at':now(),'updated_at':now(),'task':task.model_dump(mode='json')})
            print(json.dumps({'case':name,'task_id':identity,'phase':'planning'}),flush=True)
            try:
                await asyncio.wait_for(runtime.prepare(identity,user,task),timeout=240)
                record=runtime.db.task(identity,user['id'])
                run_id=record.get('engine_run_id')
                if run_id:
                    future=runtime.service.pipeline.pending.get(run_id)
                    elapsed=0
                    while future and not future.done() and elapsed<720:
                        await asyncio.wait([future],timeout=10)
                        elapsed+=10
                        if elapsed%30==0:
                            events=runtime.service.store.get(run_id,'run').get('events',[])
                            last=events[-1] if events else {}
                            print(json.dumps({'case':name,'phase':last.get('step'),'status':last.get('status'),'elapsed_upper_bound':elapsed}),flush=True)
                    if future and not future.done():
                        future.cancel();await asyncio.gather(future,return_exceptions=True)
                view=runtime.task_view(identity,user['id'])
                rows=view.get('scientific_evidence') or []
                used=[s.get('source') for s in rows]
                selection=view.get('selection') or {}
                ranked=selection.get('ranking') or []
                answer=view.get('summary') or view.get('error') or ''
                passed=(view['status']=='needs_input' and bool(view.get('clarification_questions'))) if mode=='needs_input' else (
                    view['status']=='succeeded' and bool(answer) and (set(used)<={expected_source} and expected_source in used if expected_source else len(ranked)<=3))
                if mode == 'availability':
                    passed = passed and all(str(r.get('time_range','')).endswith('/2026-10-22') for r in rows)
                if name == 'small_stockpile':
                    passed = passed and not used
                result={'case':name,'task_id':identity,'question':question,'status':view['status'],'passed':passed,
                    'selected_sources':[r['source'] for r in ranked],'scientific_sources':used,
                    'answer':answer,'clarification_questions':view.get('clarification_questions'),
                    'image_count':len(view.get('images',[])),'result_status':view.get('result_status'),
                    'fusion':view.get('fusion')}
                results.append(result)
                print(json.dumps({'case':name,'status':view['status'],'passed':passed,'selected_sources':result['selected_sources']}),flush=True)
            except Exception as error:
                results.append({'case':name,'task_id':identity,'passed':False,'error_type':type(error).__name__})
                print(json.dumps({'case':name,'passed':False,'error_type':type(error).__name__}),flush=True)
            out=ROOT/'outputs/demand-acceptance-20261022.json'
            out.write_text(json.dumps({'tested_at':datetime.now(timezone.utc).isoformat(),'real_channels':True,
                'passed':all(r['passed'] for r in results),'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        await runtime.close()
    return 0 if all(r['passed'] for r in results) else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
