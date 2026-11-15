"""Run one explicitly labelled acceptance task using the configured live channels.

The managed portal must be stopped first; its engine lock enforces exclusivity.
Existing records and sessions are preserved. No credentials are printed.
"""
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from wuyu_harness.config import Settings
from wuyu_harness.portal import PortalRuntime
from wuyu_harness.schemas import TaskRequest
from wuyu_harness.store import now


async def main():
    load_dotenv(ROOT / '.env.wuyu', override=True)
    runtime = PortalRuntime(ROOT / 'outputs/portal', Settings.from_env())
    admins = [u for u in runtime.db.users() if u['role'] == 'admin' and u['active']]
    if len(admins) != 1:
        raise RuntimeError('Acceptance requires a single unambiguous active administrator.')
    user = admins[0]
    await runtime.start_engine()
    task = TaskRequest(objective='版本验收：核查 WGS84 经度104.461117、纬度30.858473 当前可取得影像是否存在疑似固废堆存。先检查同点高分的位置和形态；根据已取得线索决定是否补充其他高分、多光谱或SAR。说明实际图源、日期、参数和逐源观察，最后形成综合结论。',
                       task_types=['suspected_site_discovery', 'waste_type_screening'],
                       center=[104.461117, 30.858473], coordinate_system='wgs84',
                       acquire_optical=True, search_policy=False, consult_shensi=True)
    task_id = uuid.uuid4().hex
    runtime.db.put_task({'id': task_id, 'user_id': user['id'], 'status': 'planning',
                         'created_at': now(), 'updated_at': now(), 'task': task.model_dump(mode='json')})
    print(json.dumps({'task_id': task_id, 'status': 'planning'}, ensure_ascii=False), flush=True)
    try:
        await runtime.prepare(task_id, user, task)
        record = runtime.db.task(task_id, user['id'])
        if record.get('engine_run_id'):
            run_id = record['engine_run_id']
            future = runtime.service.pipeline.pending.get(run_id)
            last = None
            while future and not future.done():
                events = runtime.service.store.get(run_id, 'run').get('events', [])
                state = (events[-1]['step'], events[-1]['status']) if events else ('queued', 'queued')
                if state != last:
                    print(json.dumps({'step': state[0], 'status': state[1]}, ensure_ascii=False), flush=True)
                    last = state
                await asyncio.wait([future], timeout=10)
        view = runtime.task_view(task_id, user['id'])
        result = {'task_id': task_id, 'status': view['status'], 'error': view.get('error'),
                  'summary': view.get('summary'), 'image_count': len(view['images']),
                  'fusion': view.get('fusion'), 'evidence_summary': view.get('evidence_summary'),
                  'evidence_presentation': view.get('evidence_presentation'),
                  'adaptive_review': view.get('adaptive_review'),
                  'scientific_evidence': view.get('scientific_evidence')}
        destination = ROOT / 'outputs/adaptive-v1-evaluation/portal-live-result.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'task_id': task_id, 'status': view['status'], 'error': view.get('error'),
                          'images': len(view['images']), 'selected': view.get('evidence_presentation'),
                          'judgment': (view.get('fusion') or {}).get('judgment')}, ensure_ascii=False), flush=True)
    finally:
        await runtime.close()


if __name__ == '__main__':
    asyncio.run(main())
