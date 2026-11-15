"""Run a labelled archive-research acceptance task on configured live channels.

Stop the portal first. Existing users, sessions and tasks are preserved. This
script prints only coarse progress and public acquisition/analysis facts.
"""
import argparse
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
from wuyu_harness.schemas import ResearchRequest, TaskRequest
from wuyu_harness.store import now


async def main(mode):
    load_dotenv(ROOT / '.env.wuyu', override=True)
    runtime = PortalRuntime(ROOT / 'outputs/portal', Settings.from_env())
    admins = [u for u in runtime.db.users() if u['role'] == 'admin' and u['active']]
    if len(admins) != 1:
        raise ValueError('Live acceptance requires one unambiguous active administrator.')
    user = admins[0]
    await runtime.start_engine()
    try:
        objective = ('版本验收：1月附近几月的Sentinel数据都没有吗？请实际检索104.097927、30.914227的存档，按月列出场景日期与可得性，直接回答本轮问题。'
            if mode == 'availability' else
            '版本验收：请查看104.097927、30.914227在2026年1月及前后两个月的Sentinel-2影像，选择实际可用时相分析地表变化，重点比较1月8日和1月18日前后的表现。先检查目录、质量和真实影像，再根据结果决定补景；不要重复补高分底图。')
        task = TaskRequest(objective=objective,
            task_types=['suspected_site_discovery' if mode == 'availability' else 'stockpile_change'],
            center=[104.097927, 30.914227], coordinate_system='wgs84',
            date_start='2026-01-01', date_end='2026-01-31', acquire_optical=False,
            search_policy=False, consult_shensi=False,
            research=ResearchRequest(mode=mode, sources=['sentinel2'], expand_adjacent_months=2,
                anchor_start='2026-01-01', anchor_end='2026-01-31', full_catalog=True))
        task_id = uuid.uuid4().hex
        runtime.db.put_task({'id': task_id, 'user_id': user['id'], 'status': 'planning',
            'created_at': now(), 'updated_at': now(), 'task': task.model_dump(mode='json')})
        print(json.dumps({'task_id': task_id, 'mode': mode, 'status': 'planning'}), flush=True)
        await runtime.prepare(task_id, user, task)
        record = runtime.db.task(task_id, user['id'])
        run_id = record.get('engine_run_id')
        if not run_id:
            raise ValueError('Live planning did not create an engine run.')
        print(json.dumps({'run_id': run_id, 'status': 'running'}), flush=True)
        future = runtime.service.pipeline.pending.get(run_id)
        last = None
        while future and not future.done():
            events = runtime.service.store.get(run_id, 'run').get('events', [])
            if events:
                state = tuple(events[-1].get(k) for k in ('step', 'status', 'source', 'period'))
                if state != last:
                    print(json.dumps(dict(zip(('step', 'status', 'source', 'period'), state))), flush=True)
                    last = state
            await asyncio.wait([future], timeout=10)
        view = runtime.task_view(task_id, user['id'])
        folder = ROOT / 'outputs/research-v2-evaluation'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (mode + '-portal-result.json')).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding='utf-8')
        package_path = runtime.service.store.run_dir(run_id) / 'finding-package.json'
        package = json.loads(package_path.read_text(encoding='utf-8')) if package_path.exists() else {}
        print(json.dumps({'task_id': task_id, 'run_id': run_id, 'status': view['status'], 'error': view.get('error'),
            'result_status': package.get('status'), 'narrative_status': package.get('narrative_status'),
            'images': len(view.get('images', [])), 'sources': [
                {key: item.get(key) for key in ('source', 'scene_count', 'status', 'science_pixels_analyzed', 'monthly_availability')}
                for item in package.get('scientific_evidence', [])], 'research_result': package.get('research_result')}, ensure_ascii=False), flush=True)
    finally:
        await runtime.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['availability', 'timeseries'], default='availability')
    args = parser.parse_args()
    asyncio.run(main(args.mode))
