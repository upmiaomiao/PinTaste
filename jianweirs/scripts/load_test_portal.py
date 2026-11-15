"""多用户压测：真实门户（真实 HTTP、登录、数据库、调度、事件循环）+ 可控时长的模拟分析引擎。

用法（在项目根目录）：
    python scripts/load_test_portal.py --analysis-users 20 --qa-users 20
    python scripts/load_test_portal.py --legacy ...      # 复现旧的"单通道"行为作对照
    python scripts/load_test_portal.py --sync-block-ms 1000 --block-in-loop ...   # 复现"报告导出阻塞事件循环"

被测的是门户本身（登录、限流、容量、调度、任务状态、会话列表、数据库、事件循环延迟）。
模型与图源不在被测范围内：它们是外部服务，容量取决于供应商配额，另见 docs/CAPACITY.md。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'A-long-load-test-password-42'
INTERACTIVE = {'contextual_qa', 'information_search'}


# ============================================================ 被测服务端
def serve(args):
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from wuyu_harness import scheduler
    from wuyu_harness.config import ModelConfig, Settings
    from wuyu_harness.portal import create_portal_app
    from wuyu_harness.store import Store, now

    if args.legacy:
        scheduler.LaneSet.INTERACTIVE_MODES = frozenset()   # 所有任务走同一个通道
    state_dir = Path(args.state)
    origin = f'http://127.0.0.1:{args.port}'

    class LoadPipeline:
        def __init__(self, service):
            self.service, self.pending, self.settings = service, {}, service.settings

        def submit(self, task):
            run_id = uuid.uuid4().hex
            store = self.service.store
            record = {'id': run_id, 'status': 'queued', 'created_at': now(), 'updated_at': now(), 'events': [],
                      'task': task.model_dump(mode='json')}
            store.put('run', record)
            seconds = args.qa_seconds if task.workflow_mode in INTERACTIVE else args.analysis_seconds

            async def run():
                record = store.get(run_id, 'run'); record.update(status='running', updated_at=now()); store.put('run', record)
                await asyncio.sleep(seconds)
                if args.sync_block_ms and task.workflow_mode not in INTERACTIVE:
                    if args.block_in_loop:
                        time.sleep(args.sync_block_ms / 1000)        # 旧行为：同步生成报告，阻塞所有人
                    else:
                        await asyncio.to_thread(time.sleep, args.sync_block_ms / 1000)   # 新行为：放到线程
                folder = store.run_dir(run_id)
                package = {'task': record['task'], 'assets': [], 'recognition': {'candidates': []},
                           'narrative': {'summary': '压测占位结果'}, 'workflow_stages': [], 'warnings': [],
                           'fusion': {'level': 'medium', 'probability': None}}
                (folder / 'finding-package.json').write_text(json.dumps(package, ensure_ascii=False), encoding='utf-8')
                record = store.get(run_id, 'run')
                record.update(status='succeeded', result_status='draft_pending_human_review', updated_at=now(), artifacts=['finding-package.json'])
                store.put('run', record)

            self.pending[run_id] = asyncio.create_task(run())
            self.pending[run_id].add_done_callback(lambda _: self.pending.pop(run_id, None))
            return record

    class LoadService:
        def __init__(self, settings):
            self.settings, self.store = settings, Store(settings.output_dir)
            self.pipeline = LoadPipeline(self)

        async def start(self):
            return self

        async def close(self):
            pass

        def workbook_records(self, *a, **k):
            return self.store.workbook_records(*a, **k)

        def workbook_point_assets(self, *a, **k):
            return self.store.workbook_point_assets(*a, **k)

    async def planner(user, task, settings):
        await asyncio.sleep(0.05)   # 规划本身的耗时（真实环境由 Harness 子进程决定）
        return {'engine': 'load-test', 'task': task.model_dump(mode='json'), 'warnings': [],
                'task_outline': [{'id': 'task_analysis', 'title': '任务大纲', 'summary': '压测'}]}

    settings = Settings(state_dir / 'engine', {r: ModelConfig(r, 'test', '', '') for r in ('main', 'selector', 'recognizer', 'reasoner')},
                        concurrency=args.analysis_slots, qa_concurrency=args.qa_slots)
    app = create_portal_app(output_dir=state_dir / 'portal', base_settings=settings, planner_adapter=planner,
                            service_factory=LoadService, origin=origin)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


# ============================================================ 压测驱动
class Stats:
    def __init__(self):
        self.latency: dict[str, list[float]] = {}
        self.status: dict[str, dict[int, int]] = {}
        self.e2e: dict[str, list[float]] = {'analysis': [], 'qa': []}

    def record(self, kind, seconds, status):
        self.latency.setdefault(kind, []).append(seconds)
        self.status.setdefault(kind, {}).setdefault(status, 0)
        self.status[kind][status] += 1


def pct(values, fraction):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))]


async def drive(args):
    import httpx
    base = f'http://127.0.0.1:{args.port}'
    stats = Stats()
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=400)

    async def call(client, kind, method, path, **kw):
        started = time.perf_counter()
        try:
            response = await client.request(method, path, **kw)
            stats.record(kind, time.perf_counter() - started, response.status_code)
            return response
        except Exception:
            stats.record(kind, time.perf_counter() - started, -1)
            return None

    admin = httpx.AsyncClient(base_url=base, limits=limits, timeout=60, headers={'Origin': base})
    boot = await call(admin, 'bootstrap', 'POST', '/portal/api/bootstrap', json={'username': 'administrator', 'password': PASSWORD})
    admin_csrf = boot.json()['csrf_token']
    total = args.analysis_users + args.qa_users
    for i in range(total):
        await call(admin, 'create_user', 'POST', '/portal/api/users', headers={'X-CSRF-Token': admin_csrf},
                   json={'username': f'user{i:03d}', 'password': PASSWORD, 'display_name': f'用户{i}'})

    async def login_user(i):
        client = httpx.AsyncClient(base_url=base, limits=limits, timeout=120, headers={'Origin': base})
        response = await call(client, 'login', 'POST', '/portal/api/login', json={'username': f'user{i:03d}', 'password': PASSWORD})
        if response is None or response.status_code != 200:
            return None
        client.csrf = response.json()['csrf_token']
        return client

    started = time.perf_counter()
    clients = await asyncio.gather(*(login_user(i) for i in range(total)))   # 同时登录：检验登录风暴
    login_wall = time.perf_counter() - started
    clients = [c for c in clients if c is not None]

    async def refresher(client, stop):
        """模拟侧栏/页面的周期刷新：会话列表、任务列表。"""
        while not stop.is_set():
            await call(client, 'list_conversations', 'GET', '/portal/api/conversations?limit=100&offset=0')
            await call(client, 'list_tasks', 'GET', '/portal/api/tasks?limit=50&offset=0')
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    async def user_loop(client, kind):
        stop = asyncio.Event()
        refresh = asyncio.create_task(refresher(client, stop))
        for _ in range(args.rounds):
            body = ({'objective': '成都市固废堆存监管的国家和地方政策有哪些', 'search_policy': False} if kind == 'qa' else
                    {'objective': '核查这个点位有没有固废堆存', 'center': [104.06, 30.67], 'coordinate_system': 'wgs84',
                     'task_types': ['suspected_site_discovery'], 'search_policy': False})
            began = time.perf_counter()
            for attempt in range(60):
                response = await call(client, f'submit_{kind}', 'POST', '/portal/api/tasks', json=body, headers={'X-CSRF-Token': client.csrf})
                if response is not None and response.status_code in (429, 503):
                    await asyncio.sleep(min(5, float(response.headers.get('Retry-After', 2))))
                    continue
                break
            if response is None or response.status_code != 202:
                continue
            task_id = response.json()['id']
            while time.perf_counter() - began < args.timeout:
                await asyncio.sleep(args.poll_seconds)
                view = await call(client, 'poll', 'GET', f'/portal/api/tasks/{task_id}')
                if view is not None and view.status_code == 200 and view.json().get('status') in ('succeeded', 'completed', 'failed', 'planning_failed', 'interrupted'):
                    break
            stats.e2e['qa' if kind == 'qa' else 'analysis'].append(time.perf_counter() - began)
            await asyncio.sleep(args.think_seconds)
        stop.set(); await refresh

    plan = [(c, 'analysis') for c in clients[:args.analysis_users]] + [(c, 'qa') for c in clients[args.analysis_users:]]
    began = time.perf_counter()
    await asyncio.gather(*(user_loop(c, k) for c, k in plan))
    wall = time.perf_counter() - began
    metrics = (await call(admin, 'metrics', 'GET', '/portal/api/admin/metrics')).json()
    for c in clients: await c.aclose()
    await admin.aclose()
    return stats, metrics, wall, login_wall, len(clients)


def report(args, stats, metrics, wall, login_wall, logged_in):
    mode = '旧行为（单通道，名额 1）' if args.legacy else f'新调度（分析 {args.analysis_slots} 名额 + 问答 {args.qa_slots} 名额）'
    print(f'\n=== 压测结果：{mode} ===')
    print(f'并发用户：分析 {args.analysis_users} + 问答 {args.qa_users}；每人 {args.rounds} 轮；分析耗时 {args.analysis_seconds}s，问答耗时 {args.qa_seconds}s'
          + (f'；同步阻塞 {args.sync_block_ms}ms（{"事件循环内" if args.block_in_loop else "线程内"}）' if args.sync_block_ms else ''))
    print(f'登录：{logged_in} 人同时登录用时 {login_wall:.1f}s；总用时 {wall:.1f}s')
    print('\n端到端（提交→完成，含排队）：')
    for name, label in (('qa', '追问/资料检索'), ('analysis', '影像分析')):
        values = stats.e2e[name]
        if values:
            print(f'  {label:<12} 次数 {len(values):>3}  p50 {pct(values, .5):6.1f}s  p95 {pct(values, .95):6.1f}s  最长 {max(values):6.1f}s')
    print('\n接口延迟（毫秒）与状态码：')
    for kind in sorted(stats.latency):
        v = [x * 1000 for x in stats.latency[kind]]
        print(f'  {kind:<20} 次数 {len(v):>5}  p50 {pct(v, .5):7.0f}  p95 {pct(v, .95):7.0f}  最长 {max(v):7.0f}  状态 {stats.status[kind]}')
    lag = metrics['event_loop_lag_ms']
    print(f"\n事件循环延迟（越小越好；大了说明有同步代码在阻塞所有人）：p50 {lag['p50']}ms  p95 {lag['p95']}ms  最大 {lag['max']}ms")
    for name, lane in metrics['lanes'].items():
        print(f"通道 {name:<11} 名额 {lane['capacity']}  已完成 {lane['completed']}  排队等待 p50 {lane['wait_seconds_p50']}s  p95 {lane['wait_seconds_p95']}s")
    errors = sum(count for kind in stats.status.values() for code, count in kind.items() if code >= 500 or code == -1)
    print(f'\n服务端错误/连接失败（5xx 或超时）：{errors}')
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=3197)
    parser.add_argument('--state')
    parser.add_argument('--analysis-users', type=int, default=20)
    parser.add_argument('--qa-users', type=int, default=20)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--analysis-seconds', type=float, default=6.0)
    parser.add_argument('--qa-seconds', type=float, default=1.0)
    parser.add_argument('--analysis-slots', type=int, default=3)
    parser.add_argument('--qa-slots', type=int, default=8)
    parser.add_argument('--refresh-seconds', type=float, default=3.0)
    parser.add_argument('--poll-seconds', type=float, default=1.0)
    parser.add_argument('--think-seconds', type=float, default=0.5)
    parser.add_argument('--timeout', type=float, default=600.0)
    parser.add_argument('--sync-block-ms', type=int, default=0)
    parser.add_argument('--block-in-loop', action='store_true')
    parser.add_argument('--legacy', action='store_true')
    args = parser.parse_args()
    if args.serve:
        return serve(args)
    if args.legacy:
        args.analysis_slots = 1
    state = tempfile.mkdtemp(prefix='wuyu-load-')
    command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--serve', '--port', str(args.port), '--state', state,
               '--analysis-seconds', str(args.analysis_seconds), '--qa-seconds', str(args.qa_seconds),
               '--analysis-slots', str(args.analysis_slots), '--qa-slots', str(args.qa_slots), '--sync-block-ms', str(args.sync_block_ms)]
    command += (['--block-in-loop'] if args.block_in_loop else []) + (['--legacy'] if args.legacy else [])
    server = subprocess.Popen(command, cwd=ROOT)
    try:
        import httpx
        for _ in range(100):
            try:
                if httpx.get(f'http://127.0.0.1:{args.port}/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        errors = report(args, *asyncio.run(drive(args)))
        return 0 if errors == 0 else 1
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == '__main__':
    sys.exit(main())
