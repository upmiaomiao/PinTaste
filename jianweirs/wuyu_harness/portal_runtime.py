"""User-facing, same-origin product portal with server-enforced resource isolation.

The engineering MCP/API stays separate. Each portal owns an independent engine
output directory; no account or document is imported from another application.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import re
import time

from fastapi import HTTPException
from pydantic import ValidationError

from .auth_portal import PortalDB
from .identity import WuyuIdentity
from .user_limits import UserLimits
from .usage_context import user_scope
from .config import Settings
from .data import DataClient, DataClientError
from .portal_config import PortalConfig, TASK_POLICY_CAPACITY_DEFAULTS
from .scheduler import LaneSet
from .schemas import TaskRequest, ResearchRequest
from .intent import resolve_research_request
from .service import Service
from .source_registry import SourceRegistry
from .store import now

PROJECT = Path(__file__).resolve().parents[1]
PHASES = [{"id":"task_analysis","label":"任务理解与大纲"},
          {"id":"source_selection","label":"按任务选星取图"},
          {"id":"recognition_review","label":"见微识别与追加复核"},
          {"id":"synthesis","label":"最终研判与资料补充"}]


def restored_task_request(payload, explicit_fields=None):
    """Restore values and their input provenance across durable batch queues."""
    task = TaskRequest.model_validate(payload)
    if explicit_fields is not None:
        if (not isinstance(explicit_fields,list)
                or any(not isinstance(key,str) or key not in TaskRequest.model_fields for key in explicit_fields)):
            raise ValueError("任务字段来源记录无效，请重新提交")
        task.model_fields_set.intersection_update(explicit_fields)
    return task


def batch_execution_task(task, policy):
    """Apply missing administrative defaults and canonical research intent.

    Explicit false flags win. Normalization happens before the planning guard,
    so no declared scope or execution condition is silently relaxed there.
    """
    explicit = set(task.model_fields_set)
    for key in ("selector_mode","adaptive_review","search_policy","consult_shensi"):
        if key not in explicit and key in policy:
            setattr(task,key,policy[key])
            explicit.add(key)
    task.research = resolve_research_request(task)
    task.model_fields_set.intersection_update(explicit)
    return task


class PortalRuntime:
    def __init__(self, folder: Path, base_settings: Settings, planner_adapter=None, service_factory=None):
        folder = Path(folder).resolve()
        self.db = PortalDB(folder)
        self.identity = WuyuIdentity(folder)
        self.limits = UserLimits(self.db)
        self.config = PortalConfig(self.db,replace(base_settings,output_dir=folder / "engine"))
        self.planner = planner_adapter
        self.service_factory = service_factory
        self.service = None
        self.jobs = {}
        # 两个通道：追问/资料检索 与 影像分析，通道内按用户公平轮转。
        self.lanes = LaneSet(base_settings.concurrency, getattr(base_settings, 'qa_concurrency', 8))
        self.draining = False          # 维护/发布时只停收新任务，已有任务照常完成
        from .ratelimit import RateLimiter
        self.rate_limiter = RateLimiter()
        self.started_at = time.monotonic()
        self._loop_lag = deque(maxlen=480)   # 最近约 2 分钟的事件循环延迟（秒）
        self._background = []
        self._policy = dict(TASK_POLICY_CAPACITY_DEFAULTS)
        self.incoming_uploads = 0
        from .request_replay import RequestReplay
        self.replays = RequestReplay(self.db)
        self.configuration_lock = asyncio.Lock()
        self.location_searches = {}
        self.conversation_report_cache = OrderedDict()

    async def start_engine(self):
        settings = self.config.settings()
        self.lanes.resize(analysis=settings.concurrency, interactive=settings.qa_concurrency)
        self._policy = {**TASK_POLICY_CAPACITY_DEFAULTS, **self.config.read()["task_policy"]}
        if self.service_factory:
            service = self.service_factory(settings)
            if inspect.isawaitable(service):
                service = await service
        else:
            service = Service(settings)
            await service.data.aclose()
            data = self.config.read()["data"]
            service.data = DataClient(data["base_url"],data["api_key"],data["mode"],
                                      source_registry=SourceRegistry(settings.output_dir/"source-registry.json"),
                                      source_policy=self.config.read()['sources'],
                                      unified_config=self.config.read()['unified_data'])
            service.pipeline.data = service.data
        try:
            self.service = await service.start()
        except Exception:
            await service.close()
            raise
        if not self._background:
            self._background = [asyncio.create_task(self._monitor_loop()), asyncio.create_task(self._reconcile_loop())]
        # A process restart cannot safely replay external paid planning requests.
        interrupted_before_engine = []
        with self.db.connect() as conn:
            for row in conn.execute("SELECT body FROM tasks").fetchall():
                task = json.loads(row[0])
                if task["status"] in ("planning","queued","running"):
                    task.update(status="interrupted",error="服务重启中断，请检查已有材料后重新提交",updated_at=now())
                    conn.execute("UPDATE tasks SET body=? WHERE id=?",(json.dumps(task,ensure_ascii=False),task["id"]))
                    if not task.get('engine_run_id'):
                        interrupted_before_engine.append(task)
        for task in interrupted_before_engine:
            self.refund_task(task)

    def refund_task(self, record):
        """Return the QA debit of a single task that produced no answer.

        Only tasks that never reached the analysis engine are refunded; an
        engine run may already have spent paid model calls.
        """
        if record.get('engine_run_id') or record.get('quota_refunded'):
            return
        event = 'task_' + record['id']
        with self.db.connect() as conn:
            row = conn.execute('SELECT amount FROM usage_events WHERE id=?', (event,)).fetchone()
        if not row or row[0] != 1:
            return  # batch / scan debits cover several tasks and stay as charged
        self.limits.refund(event)
        record['quota_refunded'] = True
        self.db.put_task(record)

    async def _monitor_loop(self):
        """测量事件循环延迟：延迟变大说明有同步代码在阻塞所有人。"""
        interval = 0.25
        while True:
            started = time.monotonic()
            await asyncio.sleep(interval)
            self._loop_lag.append(max(0.0, time.monotonic() - started - interval))

    async def _reconcile_loop(self):
        """定期把仍在运行的任务状态与引擎对齐，不依赖有人在刷新页面。"""
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(self._reconcile_active)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def _reconcile_active(self):
        for record in self.db.active_tasks()[:200]:
            if record.get('engine_run_id'):
                self.sync_task_status(record)

    def apply_capacity(self):
        """不重启引擎、不中断任务，实时应用容量与超时配置。"""
        settings = self.config.settings()
        self._policy = {**TASK_POLICY_CAPACITY_DEFAULTS, **self.config.read()["task_policy"]}
        self.lanes.resize(analysis=settings.concurrency, interactive=settings.qa_concurrency)
        if self.service is not None:
            self.service.settings = settings
            pipeline = self.service.pipeline
            pipeline.settings = settings
            pipeline.semaphore = asyncio.Semaphore(settings.concurrency)
            pipeline.qa_semaphore = asyncio.Semaphore(max(1, settings.qa_concurrency))

    def metrics(self):
        """给管理员和监控看的运行指标（不含任何用户内容）。"""
        lag = sorted(self._loop_lag)
        def pick(fraction):
            return round(lag[min(len(lag) - 1, int(len(lag) * fraction))] * 1000, 1) if lag else 0.0
        counts = self.db.active_counts()
        return {'uptime_seconds': int(time.monotonic() - self.started_at), 'draining': self.draining,
                'lanes': self.lanes.stats(), 'event_loop_lag_ms': {'p50': pick(.5), 'p95': pick(.95), 'max': round(max(lag) * 1000, 1) if lag else 0.0},
                'active_tasks': sum(counts.values()), 'active_users': len(counts), 'job_groups': len(self.jobs),
                'uploads_in_progress': self.incoming_uploads, 'limits': {key: self._policy.get(key) for key in TASK_POLICY_CAPACITY_DEFAULTS}}

    async def close(self):
        for job in self._background:
            job.cancel()
        self._background = []
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        if self.service:
            await self.service.close()
        await self.identity.close()
        self.db.close()      # 关闭本进程各线程复用的数据库连接

    def check_task_ownership(self, task: TaskRequest, user_id):
        for resource_id in task.asset_ids:
            self.db.require_owner("image",resource_id,user_id)
        for resource_id in task.document_ids:
            self.db.require_owner("document",resource_id,user_id)

    def safe_error(self, error):
        text = str(error)
        config = self.config.read()
        for section in [*config["models"].values(),config["metaso"],config["data"],config["web_search"],config["wechat"],config['geography'],config['unified_data']]:
            if section.get("api_key"):
                text = text.replace(section["api_key"],"[已隐藏]")
        return text[:600] or "任务执行未完成，请核对输入与服务配置"

    def admin_overview(self):
        """Platform counters and a small administrative task ledger, never run contents."""
        users = {user["id"]:user for user in self.db.users()}
        with self.db.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT body FROM tasks ORDER BY rowid DESC")]
        counts = {key:0 for key in ("planning","running","queued","completed","failed","interrupted","cancelled")}
        recent = []
        for record in records:
            status, result_status, updated = record["status"], record.get("result_status"), record.get("updated_at")
            if record.get("engine_run_id"):
                try:
                    engine = self.service.store.get(record["engine_run_id"],"run")
                    status, result_status, updated = engine["status"], engine.get("result_status"), engine.get("updated_at",updated)
                except KeyError:
                    status = "interrupted"
            category = "completed" if status in ("succeeded","completed") else "failed" if status in ("failed","planning_failed") else status
            if category in counts:
                counts[category] += 1
            if len(recent)<10:
                user = users.get(record["user_id"],{})
                recent.append({"id":record["id"],"user_display_name":user.get("display_name","账号已移除"),
                    "username":user.get("username",""),"objective":record.get("task",{}).get("objective","")[:180],
                    "status":status,"result_status":result_status,"created_at":record.get("created_at"),"updated_at":updated})
        counts.update(registered_users=len(users),active_users=sum(user["active"] for user in users.values()),total_tasks=len(records))
        models = [{"role":role,"model":model.model,"configured":model.ready,
                   "input_modalities":list(model.input_modalities),"max_tokens":model.max_tokens}
                  for role,model in self.service.settings.models.items()]
        return {"counts":counts,"models":models,"recent_tasks":recent,"updated_at":now(),"scope":"all_portal_users",
                "execution":{"mode":"single_instance", "concurrency":self.service.settings.concurrency,
                    "qa_concurrency":self.service.settings.qa_concurrency,
                    "max_pending_tasks":self._policy["max_pending_tasks"], "max_job_groups":self._policy["max_job_groups"],
                    "max_user_interactive":self._policy["max_user_interactive"],
                    "max_user_batch":self._policy["max_user_batch"], "max_parallel_uploads":2,
                    "active_job_groups":len(self.jobs), "active_uploads":self.incoming_uploads,
                    "lanes":self.lanes.stats(), "draining":self.draining}}

    def sync_task_status(self, record):
        """Status-only refresh: never parse report/image payloads for navigation."""
        run_id = record.get('engine_run_id')
        if run_id and record['status'] in ('planning', 'queued', 'running'):
            self.db.require_owner('run', run_id, record['user_id'])
            try:
                engine = self.service.store.get(run_id, 'run')
                patch = {key: engine[key] for key in ('status','result_status','updated_at','error') if key in engine}
            except KeyError:
                patch = {'status':'interrupted', 'error':'任务记录暂不可用，请联系管理员'}
            if any(record.get(k) != v for k,v in patch.items()):
                record.update(patch)
                self.db.put_task(record)
        return record

    def task_summaries(self, user_id, limit=100, offset=0):
        summaries = []
        for record in self.db.tasks(user_id, limit=limit, offset=offset):
            record = self.sync_task_status(record)
            summaries.append({**{key: record[key] for key in (
                'id','status','created_at','updated_at','parent_task_id','batch_id','scan_id') if key in record},
                'objective': record.get('task',{}).get('objective','')[:240], 'list_summary':True})
        return summaries

    def conversation_index(self, user_id):
        from .conversation_state import ConversationIndex
        return ConversationIndex(self.db, user_id)

    def conversation_summaries(self, user_id, limit=100, offset=0):
        index = self.conversation_index(user_id)
        groups = index.groups()
        summaries = []
        for root, members in groups[offset:offset+limit]:
            first, last = members[0], members[-1]
            record = self.sync_task_status(self.db.task(last['id'], user_id))
            summaries.append({'id': last['id'], 'latest_task_id': last['id'],
                'conversation_id': root, 'title': first.get('objective') or '新会话',
                'objective': first.get('objective') or '新会话', 'turn_count': len(members),
                'status': record['status'], 'created_at': first.get('created_at'),
                'updated_at': record.get('updated_at'), 'list_summary': True})
        return {'items': summaries, 'limit': limit, 'offset': offset,
                'has_more': offset+limit < len(groups), 'total': len(groups)}

    def _conversation_scope(self, identity, user_id, *, through_task_id=None, index=None):
        index = index or self.conversation_index(user_id)
        root, members = index.members(identity)
        if through_task_id:
            # A snapshot cursor must belong to this exact owned conversation.
            if index.root(through_task_id) != root:
                raise HTTPException(404, '会话轮次不存在')
            position = next(i for i,row in enumerate(members) if row['id'] == through_task_id)
            members = members[:position+1]
        return index, root, members

    def _core_report(self, root, members, user_id):
        from .living_report import build_core_report
        records, signatures = [], []
        for item in members:
            record = self.sync_task_status(self.db.task(item['id'], user_id))
            records.append(record)
            run_id = record.get('engine_run_id')
            fingerprint = None
            if run_id:
                self.db.require_owner('run', run_id, user_id)
                path = self.service.store.run_dir(run_id) / 'finding-package.json'
                if path.is_file() and not path.is_symlink():
                    info = path.stat()
                    fingerprint = [info.st_mtime_ns, info.st_size]
            signatures.append([record['id'], record['status'], record.get('updated_at'), run_id, fingerprint])
        signature = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode()).hexdigest()
        key = (user_id, root, members[-1]['id'], signature)
        cached = self.conversation_report_cache.get(key)
        if cached is not None:
            self.conversation_report_cache.move_to_end(key)
            return cached
        by_id = {row['id']:row for row in records}
        def saved_package(task_id):
            record = by_id[task_id]
            if not record.get('engine_run_id') or record['status'] != 'succeeded':
                return {}
            run_id = record['engine_run_id']
            self.db.require_owner('run', run_id, user_id)
            path = self.service.store.run_dir(run_id) / 'finding-package.json'
            if not path.is_file() or path.is_symlink():
                return {}
            return json.loads(path.read_text(encoding='utf-8'))
        turns = [{key:value for key,value in record.items() if key in {
            'id','status','created_at','updated_at','parent_task_id','result_status'}} |
            {'task':{key:value for key,value in record['task'].items() if key not in {'context_package','prompt'}}}
            for record in records]
        report = build_core_report(root, turns, packages=saved_package)
        from .conversation_state import report_links
        report = {**report, 'as_of_task_id': members[-1]['id'], 'artifacts': report_links(root, members[-1]['id'])}
        self.conversation_report_cache[key] = report
        while len(self.conversation_report_cache) > 16:
            self.conversation_report_cache.popitem(last=False)
        return report

    def core_report(self, identity, user_id, *, through_task_id=None):
        _, root, members = self._conversation_scope(identity, user_id, through_task_id=through_task_id)
        return self._core_report(root, members, user_id)

    def core_report_image_files(self, identity, user_id, *, through_task_id=None):
        """Server-owned original/annotated image paths per asset for the derived report views."""
        _, root, members = self._conversation_scope(identity, user_id, through_task_id=through_task_id)
        files = {}
        for item in members:
            record = self.db.task(item['id'], user_id)
            run_id = record.get('engine_run_id')
            if not run_id or record.get('status') != 'succeeded':
                continue
            self.db.require_owner('run', run_id, user_id)
            folder = self.service.store.run_dir(run_id)
            package_path = folder / 'finding-package.json'
            if not package_path.is_file() or package_path.is_symlink():
                continue
            try:
                package = json.loads(package_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            for asset in package.get('assets', []):
                aid = asset.get('id') if isinstance(asset, dict) else None
                if not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', aid):
                    continue
                source = next((folder / f'source-{aid}{suffix}' for suffix in ('.png', '.jpg', '.jpeg', '.webp')
                               if (folder / f'source-{aid}{suffix}').is_file()), None)
                marked = folder / f'annotated-{aid}.png'
                files[aid] = {'src': source, 'ann': marked if marked.is_file() else None}
        return files

    def core_report_descriptor(self, identity, user_id):
        _, root, members = self._conversation_scope(identity, user_id, through_task_id=identity)
        from .conversation_state import snapshot_descriptor, report_context
        descriptor = snapshot_descriptor(root, members[-1], len(members), title=(members[0].get('objective') or '会话')+' · 核心报告')
        try:
            report = self._core_report(root, members, user_id)
            descriptor.update(report_context(report))
        except (ValueError, OSError, KeyError):
            descriptor.update(status='unavailable', summary='本次核心报告仍在整理，原始各轮资料保留。')
        return descriptor

    def conversation_view(self, identity, user_id, *, limit=50, before=None, through_task_id=None):
        index, root, members = self._conversation_scope(identity, user_id, through_task_id=through_task_id)
        end = len(members)
        if before:
            if index.root(before) != root:
                raise HTTPException(404, '会话轮次不存在')
            end = next((i for i,row in enumerate(members) if row['id'] == before), -1)
            if end < 0:
                raise HTTPException(404, '会话轮次不存在')
        start = max(0, end-limit)
        page = members[start:end]
        from .conversation_state import snapshot_descriptor
        turns = []
        title = members[0].get('objective') or '新会话'
        for position, row in enumerate(page, start+1):
            view = self.task_view(row['id'], user_id, include_conversation=False)
            view.update(conversation_id=root, conversation_title=title,
                core_report=snapshot_descriptor(root, view, position, title=title+' · 核心报告'))
            turns.append(view)
        try:
            report = self._core_report(root, members, user_id)
        except (ValueError, OSError, KeyError):
            report = {'status':'unavailable', 'summary':'核心报告仍在整理，原始各轮资料保留。',
                      'conversation_id':root, 'as_of_task_id':members[-1]['id']}
        return {'id':root, 'conversation_id':root, 'title':title,
                'latest_task_id':members[-1]['id'], 'turn_count':len(members),
                'turns':turns, 'has_more':start > 0,
                'before_cursor':page[0]['id'] if page and start > 0 else None,
                'core_report':report}

    def check_capacity(self, user_id, count=1, *, batch=False):
        if self.draining:
            raise HTTPException(503, '系统正在维护，暂不接收新任务，已有任务不受影响，请稍后再试', headers={'Retry-After': '60'})
        if self.configuration_lock.locked():
            raise HTTPException(409, '管理员正在应用配置，请稍后提交')
        policy = self._policy
        counts = self.db.active_counts()   # 单条聚合查询；任务状态由完成回调与后台对账维护
        own = counts.get(user_id, 0)
        total = sum(counts.values())
        if (len(self.jobs) >= policy['max_job_groups'] or total + count > policy['max_pending_tasks']
                or own + count > (policy['max_user_batch'] if batch else policy['max_user_interactive'])):
            wait = max(self.lanes.analysis.estimated_wait_seconds(), 5)
            raise HTTPException(429, '当前使用人数较多，任务队列暂满，请稍后重试', headers={'Retry-After': str(wait)})

    async def prepare(self, task_id, user, task, *, priority=0):
        # 通道按任务类别区分：追问/资料检索走 interactive，影像分析走 analysis；
        # 通道内按用户公平轮转，批量/区域任务（priority=1）让位于交互任务。
        # 名额覆盖规划与执行全程，所以新增批量不会成倍放大容量。
        lane = self.lanes.lane_for(getattr(task, 'workflow_mode', 'auto'))
        async with lane.slot(user['id'], priority=priority, key=task_id):
            record = self.db.task(task_id, user['id'])
            if record.get('cancel_requested_at'):
                return
            record.update(status='planning', updated_at=now())
            self.db.put_task(record)
            await self._prepare(task_id, user, task)
            record = self.db.task(task_id, user['id'])
            pending = self.service.pipeline.pending.get(record.get('engine_run_id'))
            try:
                if pending:
                    await pending
            finally:
                self.sync_task_status(self.db.task(task_id, user['id']))

    async def _prepare(self, task_id, user, task):
        record = self.db.task(task_id,user["id"])
        try:
            planner = self.planner
            if planner is None:
                from .harness_planner import plan_task
                planner = plan_task
            from .search import information_query
            if task.workflow_mode == 'contextual_qa':
                plan = {'engine':'adaptive-question','task':task.model_dump(mode='json'),'warnings':[],
                        'task_outline':[{'id':'contextual_plan','title':'理解本轮追问','summary':'复用已授权的历史证据，按当前问题选择必要工具。'}]}
            elif information_query(task):
                planned_task = task.model_copy(update={"search_only": True, "acquire_optical": False})
                plan = {"engine": "network-search", "task": planned_task.model_dump(mode="json"), "warnings": [],
                        "task_outline": [{"id": key, "title": title, "summary": detail} for key, title, detail in [
                            ("task_analysis", "问题理解", "围绕当前问题收集公开资料"),
                            ("source_selection", "联网检索", "网页使用DeepSeek，学术使用秘塔，并补充公众号资料"),
                            ("recognition_review", "正文阅读", "读取重点公众号正文并保存来源"),
                            ("synthesis", "回答与引用", "综合资料回答问题，保留出处与不确定性")]]}
            else:
                plan = await planner(user,task,self.service.settings)
            # A cancellation may arrive while the planner adapter is awaiting a
            # remote response. Even an adapter swallowing cancellation cannot
            # authorize a new execution after the user's stop request.
            if self.db.task(task_id,user['id']).get('cancel_requested_at'):
                return
            if not isinstance(plan,dict) or not isinstance(plan.get("task"),dict):
                raise ValueError("Harness未返回有效任务大纲")
            planned = TaskRequest.model_validate(plan["task"])
            if task.online_supplement == 'uploaded_only':
                planned = planned.model_copy(update={'acquire_optical': False,
                    'research': None, 'selected_sources': []})
                plan = {**plan, 'task': planned.model_dump(mode='json')}
            changed_scope = any(getattr(planned,key)!=getattr(task,key) for key in ("center","bbox","date_start","date_end"))
            if changed_scope and "asset_ids" in record.get("inherited_fields",[]) and "asset_ids" not in task.model_fields_set:
                planned = TaskRequest.model_validate({**planned.model_dump(mode="json"),"asset_ids":[]})
                plan = {**plan,"task":planned.model_dump(mode="json"),
                        "warnings":[*plan.get("warnings",[]),"范围或时段发生变化，未自动沿用上一任务影像"]}
            # Keep the complete explicit user contract, even with a custom planner adapter.
            original_values, planned_values = task.model_dump(mode="json"),planned.model_dump(mode="json")
            for key in task.model_fields_set:
                if original_values[key] != planned_values[key]:
                    raise ValueError("任务规划改动了用户明确提供的条件，请重新提交")
            self.check_task_ownership(planned,user["id"])
            questions = plan.get('demand', {}).get('questions', [])
            if questions:
                record.update(plan=plan, task=planned.model_dump(mode='json'), status='needs_input',
                    clarification_questions=questions,
                    clarification_summary='为选取合适的数据，还需确认：\n'+'\n'.join(f'{i+1}. {q}' for i,q in enumerate(questions)),
                    updated_at=now())
                self.db.put_task(record)
                return
            record.update(plan=plan,task=planned.model_dump(mode="json"),status="queued",updated_at=now())
            with user_scope(self.limits, user['id']):
                engine = self.service.pipeline.submit(planned)
            record["engine_run_id"] = engine["id"]
            self.db.own("run",engine["id"],user["id"])
            self.db.put_task(record)
        except asyncio.CancelledError:
            saved = self.db.task(task_id,user['id'])
            cancelled = bool(saved.get('cancel_requested_at'))
            record.update(status="cancelled" if cancelled else "interrupted",
                error="已按要求停止；已获取材料保留，可修改后继续" if cancelled else "任务准备中断，请重新提交",updated_at=now())
            if cancelled:
                record['cancel_requested_at'] = saved['cancel_requested_at']
            self.db.put_task(record)
            if not cancelled:
                self.refund_task(record)
            raise
        except Exception as error:
            from .harness_planner import PlannerError
            message = self.safe_error(error) if isinstance(error,(ValueError,DataClientError,PlannerError,HTTPException)) else "任务准备未完成，请由管理员检查规划与模型服务"
            record.setdefault('events',[]).append({'step':'task_planning','status':'failed',
                'time':now(),'code':'planning_failed'})
            record.update(status="planning_failed",error=message,updated_at=now())
            self.db.put_task(record)
            self.refund_task(record)

    async def run_batch(self, task_ids, user):
        lane = asyncio.Semaphore(2)
        async def point(task_id):
            async with lane:
                record = self.db.task(task_id, user['id'])
                if record.get('cancel_requested_at') or record['status'] == 'cancelled':
                    return
                record.update(status='planning', updated_at=now())
                self.db.put_task(record)
                try:
                    task = restored_task_request(record['task'],record.get('explicit_fields'))
                    task = batch_execution_task(task,self.config.read()['task_policy'])
                    if record.get('workbook_record_id') and task.document_ids:
                        imported = await asyncio.to_thread(self.service.workbook_point_assets,
                            task.document_ids[0], record['workbook_record_id'])
                        asset_ids = []
                        for asset in imported.get('assets', []):
                            self.db.own('image', asset['id'], user['id'])
                            asset_ids.append(asset['id'])
                        task = task.model_copy(update={'asset_ids':asset_ids})
                        record.update(task=task.model_dump(mode='json'),explicit_fields=sorted(task.model_fields_set), updated_at=now())
                        self.db.put_task(record)
                    else:
                        record.update(task=task.model_dump(mode='json'),explicit_fields=sorted(task.model_fields_set), updated_at=now())
                        self.db.put_task(record)
                    await self.prepare(task_id, user, task, priority=1)
                    record = self.db.task(task_id, user['id'])
                    run_id = record.get('engine_run_id')
                    pending = self.service.pipeline.pending.get(run_id) if run_id else None
                    if pending:
                        await asyncio.gather(pending, return_exceptions=True)
                except asyncio.CancelledError:
                    record = self.db.task(task_id, user['id'])
                    if not record.get('engine_run_id'):
                        record.update(status='cancelled' if record.get('cancel_requested_at') else 'interrupted', updated_at=now())
                        self.db.put_task(record)
                    raise
        await asyncio.gather(*(point(task_id) for task_id in task_ids))

    def task_view(self, task_id, user_id, *, include_conversation=True):
        record = self.db.task(task_id,user_id)
        engine_id = record.get("engine_run_id")
        package, folder = {}, None
        if engine_id:
            self.db.require_owner("run",engine_id,user_id)
            engine = self.service.store.get(engine_id,"run")
            folder = self.service.store.run_dir(engine_id)
            record.update(status=engine["status"],result_status=engine.get("result_status"),updated_at=engine["updated_at"],events=engine.get("events",[]))
            if engine.get("error"):
                record["error"] = engine["error"]
            if engine["status"] == "succeeded":
                path = self.service.store.run_dir(engine_id) / "finding-package.json"
                package = json.loads(path.read_text(encoding="utf-8"))
                for asset in package.get("assets",[]):
                    self.db.own("image",asset["id"],user_id)
            saved = self.db.task(task_id, user_id)
            if record != saved:
                self.db.put_task(record)
        stages = package.get("workflow_stages") or [{**phase,"status":"running" if index==0 and record["status"]=="planning" else "pending"} for index,phase in enumerate(PHASES)]
        if not package and engine_id:
            stage_events = {event["step"]:event for event in record.get("events",[]) if event.get("step") in {phase["id"] for phase in PHASES}}
            stages = [{**phase,"status":stage_events.get(phase["id"],{}).get("status","pending"),
                       "time":stage_events.get(phase["id"],{}).get("time")} for phase in PHASES]
        if record["status"] in ("planning_failed","interrupted") and not engine_id:
            stages[0]["status"] = "failed" if record["status"]=="planning_failed" else "interrupted"
        if record["status"] in ("failed","interrupted"):
            for stage in stages:
                if stage.get("status") in ("running","started"):
                    stage["status"] = record["status"]
        image_records = package.get("assets",[])
        if not package:
            available = list(record["task"].get("asset_ids",[]))
            available += [event["asset_id"] for event in record.get("events",[]) if event.get("step") in {"acquisition", "image_crop"} and event.get("status")=="completed" and event.get("asset_id")]
            available += [identity for event in record.get("events",[]) if event.get("step")=="source_selection" and event.get("status")=="completed"
                          for identity in event.get("registered_asset_ids",[]) if isinstance(identity,str)]
            image_records = []
            for asset_id in dict.fromkeys(available):
                try:
                    raw = self.service.store.get(asset_id,"image")
                    if self.service.store.asset_path(raw).is_file():
                        self.db.own("image",asset_id,user_id)
                        image_records.append(self.service.store.public_asset(raw))
                except KeyError:
                    pass
        images = [{"id":asset["id"],"url":f"/portal/api/assets/{asset['id']}",
                   "annotated_url":f"/portal/api/tasks/{task_id}/artifacts/annotated-{asset['id']}.png" if package and folder and (folder/f"annotated-{asset['id']}.png").is_file() else None,
                   "role":asset.get("metadata",{}).get("role"),"name":asset.get("name"),"metadata":asset.get("metadata"),
                   "analysis_scope":asset.get("analysis_scope"),"target_pixel_sufficiency_reason":asset.get("target_pixel_sufficiency_reason")} for asset in image_records]
        artifacts = [{"name":name,"url":f"/portal/api/tasks/{task_id}/artifacts/{name}"}
                     for name in ("report.docx","report.html","report.md","finding-package.json","annotations.geojson") if package and folder and (folder/name).is_file()]
        keys = ("id","status","result_status","created_at","updated_at","error","task","parent_task_id","clarification_questions")
        planner_outline = record.get("plan",{}).get("task_outline",[])
        outline = package.get("task_outline") or {"objective":record["task"]["objective"],
            "questions":[item.get("summary","") for item in planner_outline if isinstance(item,dict)]}
        selection = package.get("selection")
        if not selection:
            for event in reversed(record.get("events",[])):
                candidate = event.get("selection") if event.get("step")=="source_selection" else event if event.get("step")=="selection" else None
                if isinstance(candidate,dict) and isinstance(candidate.get("ranking"),list):
                    selection = {"method":candidate.get("method"),"missing_requirements":candidate.get("missing_requirements",[]),
                        "ranking":[{key:item[key] for key in ("group","source","reason","status","limitations") if key in item}
                                   for item in candidate["ranking"][:20] if isinstance(item,dict)]}
                    break
        from .activity import activity_entries
        documents = []
        for document_id in record['task'].get('document_ids',[])[:12]:
            try:
                self.db.require_owner('document',document_id,user_id)
                documents.append(self.service.store.get(document_id,'document'))
            except (KeyError,HTTPException):
                continue
        entries=activity_entries(record,assets=image_records,documents=documents,sanitize=self.safe_error)
        from .consumer_conclusion import package_consumer_conclusion
        consumer_conclusion = package_consumer_conclusion(package) if package else {}
        task_results = dict(package.get('task_results') or {})
        if consumer_conclusion:
            task_results['consumer_conclusion'] = consumer_conclusion
        if record.get('status') == 'planning_failed' and not engine_id:
            # Also make pre-upgrade failures visible: legacy records may have
            # no event, but the durable terminal state is an actual failure.
            entries.append({'id':'task-planning-failed','step':'task_planning','status':'failed',
                'title':'我暂时没能完成任务规划',
                'summary':'我这次没有完成任务规划，尚未开始获取影像；请查看错误提示，修改后再继续。',
                'created_at':record.get('updated_at') or record.get('created_at')})
        public_task = {key:value for key,value in record['task'].items() if key != 'context_package'}
        public_plan = dict(record.get('plan') or {})
        if isinstance(public_plan.get('task'), dict):
            public_plan['task'] = {key:value for key,value in public_plan['task'].items() if key != 'context_package'}
        result = {**{key:record[key] for key in keys if key in record},"task":public_task,"stages":stages,"workflow_stages":package.get("workflow_stages",stages),
                "activity":public_task_activity(record.get("events",[])),
                "activity_entries":entries,
                "task_outline":outline,"planner_outline":planner_outline,
                "plan":public_plan or None,"summary":package.get("narrative",{}).get("summary",record.get('clarification_summary','')),
                "narrative":{key:value for key,value in package.get("narrative",{}).items() if key in ("summary","recommendations","cited_policy_ids","answer_model","reasoning")},
                "findings":package.get("recognition",{}).get("candidates",[]),"recognition":package.get("recognition"),
                "fusion":package.get("fusion"),"strategy":package.get("strategy"),"selection":selection,
                "task_results":task_results or None,"consumer_conclusion":consumer_conclusion,
                "workflow_mode": record['task'].get('workflow_mode','auto'),
                "queue_position": self.lanes.position(task_id) if record.get('status') in ('queued','planning') else None,
                "contextual_answer":package.get('contextual_answer'),
                "environmental_risk":package.get('environmental_risk'),
                "batch_id":record.get('batch_id'),"workbook_record_id":record.get('workbook_record_id'),
                "scan_id":record.get('scan_id'),"scan_window_id":record.get('scan_window_id'),
                "adaptive_review":package.get("adaptive_review"),"image_assessments":package.get("image_assessments",[]),
                "research_result":public_research_result(package.get("research_result")),
                "evidence_summary":package.get("evidence_summary",[]),"evidence_presentation":package.get("evidence_presentation"),
                "policy":public_search_sources(package.get("policy"), task_id),"scientific_evidence":public_scientific_evidence(package.get("scientific_evidence",[]),task_id,folder),
                "warnings":[*record.get("plan",{}).get("warnings",[]),*package.get("warnings",[])],"artifacts":artifacts,"images":images}
        if include_conversation and re.fullmatch(r'[0-9a-f]{32}', task_id):
            index, root, members = self._conversation_scope(task_id, user_id)
            result.update(conversation_id=root, conversation_title=members[0].get('objective') or '新会话',
                latest_task_id=members[-1]['id'], core_report=self.core_report_descriptor(task_id, user_id))
            result['core_report_context'] = {key:value for key,value in result['core_report'].items() if key != 'artifacts'}
        return result


def public_search_sources(policy, task_id):
    if not isinstance(policy, dict):
        return policy
    result = {**policy, "sources": []}
    for source in policy.get("sources", []):
        row = {key: value for key, value in source.items() if key != "article_text"}
        if source.get("article_text"):
            row["snapshot_url"] = f"/portal/api/tasks/{task_id}/sources/{source['id']}"
        result["sources"].append(row)
    return result


def public_task_activity(events):
    """Only coarse live progress is public, never raw prompts or provider payloads."""
    result=[]
    reasons={"missing_visual_or_SCL","local_SCL_surface_screen_passed","incomplete_requested_task_bbox",
             "target_SCL_cloud_shadow_nodata_or_unclassified"}
    for event in events[-40:]:
        if not isinstance(event,dict) or not isinstance(event.get("step"),str):
            continue
        public={key:value[:160] for key,value in event.items()
                if key in ("step","status","time","source") and isinstance(value,str)}
        scene=event.get("scene_id")
        if isinstance(scene,str) and re.fullmatch(r"(?:S[12][ABC]_[A-Za-z0-9_.-]+|L[CEOT][0-9]{2}_[A-Za-z0-9_.-]+|scene-[A-Za-z0-9_-]+)",scene) and len(scene)<=160:
            public["scene_id"]=scene
        if event["step"]=="science_quality_probe":
            if event.get("quality_status") in {"screened_usable","obscured_or_unusable","unknown"}:
                public["quality_status"]=event["quality_status"]
            if event.get("reason") in reasons:
                public["reason"]=event["reason"]
        result.append(public)
    return result


def _public_scalar_facts(value, keys):
    if not isinstance(value,dict):
        return {}
    return {key:(value[key][:4000] if isinstance(value[key],str) else value[key]) for key in keys
        if key in value and (value[key] is None or isinstance(value[key],(str,int,float,bool)))}


def public_scientific_evidence(records,task_id,folder):
    """Expose bounded acquisition facts, excluding upstream URLs and audit payloads."""
    def artifact_link(name):
        if not isinstance(name,str) or not name.startswith("science-") or Path(name).name!=name or "\\" in name or not folder:
            return None
        path=(folder/name).resolve()
        if path.parent!=folder.resolve() or not path.is_file() or path.suffix.lower() not in (".tif",".tiff",".xml",".json"):
            return None
        return {"artifact":name,"artifact_url":f"/portal/api/tasks/{task_id}/artifacts/{name}"}
    def subset_view(subset):
        public={key:subset[key] for key in ("status","operation","source","scene_id","sha256","bytes","bbox","search_bbox","full_task_aoi_covered","scene_acquired_at","numeric_material_classification","dimensions","bands","valid_fraction","crs","raster_bounds","raster_bounds_crs","actual_coverage_verified","limitation","preview_status","preview_asset_id","fallback_scene_used","reason","source_globally_unavailable") if key in subset}
        public["attempts"]=[{key:attempt[key] for key in ("scene_id","status","reason") if key in attempt}
                            for attempt in subset.get("attempts",[])[:2] if isinstance(attempt,dict)]
        calibration=subset.get("calibration") or {}
        public['calibration']={key:calibration[key] for key in ('polarization','radiometry','input_units','display_units','native_pixel_spacing_m','effective_resolution_m','display_limits_db') if key in calibration}
        link=artifact_link(subset.get("artifact"))
        if link:public.update(link)
        artifacts={}
        def walk(value):
            if isinstance(value,dict):
                link=artifact_link(value.get('artifact'))
                if link:artifacts[link['artifact']]=link
                for key,item in value.items():
                    if key not in {'artifact','source_artifact'}:walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(subset)
        public['artifacts']=list(artifacts.values())[:24]
        return public
    result=[]
    for item in records[:12]:
        if not isinstance(item,dict):continue
        public={key:item[key] for key in ("group","source","operation","bbox","time_range","time_basis","selection_reason","fallback_for","status","reason","scene_count","science_pixels_analyzed","catalog_complete","full_history_complete","completeness_scope","download_status") if key in item}
        public["scenes"]=[{key:scene[key] for key in ("id","acquired_at","bbox","source") if key in scene}
                          for scene in item.get("scenes",[])[:10] if isinstance(scene,dict)]
        analysis=item.get('analysis') or {}
        if analysis:
            public['analysis']={key:analysis[key] for key in ('status','summary','observations','candidates','uncertainties','recommended_next_actions','stop_recommendation','analyst_role','science_pixels_analyzed') if key in analysis}
        if isinstance(item.get('subset'),dict):public['subset']=subset_view(item['subset'])
        if isinstance(item.get('subsets'),list):public['subsets']=[subset_view(subset) for subset in item['subsets'][:8] if isinstance(subset,dict)]
        public['monthly_availability']=[{**_public_scalar_facts(row,('period','time_range','scene_count','catalog_complete','status','quality_checked_scenes','usable_windows')),
            'acquisition_dates':[value for value in row.get('acquisition_dates',[])[:50] if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value)]}
            for row in item.get('monthly_availability',[])[:120] if isinstance(row,dict)]
        public['catalog_queries']=[_public_scalar_facts(row,('period','time_range','start','end','limit','status','complete','returned','more_available','reason'))
            for row in item.get('catalog_queries',[])[:160] if isinstance(row,dict)]
        public['quality_probes']=[{**_public_scalar_facts(row,('scene_id','scene_acquired_at','status')),
            'quality_screening':_public_scalar_facts(row.get('quality_screening'),('status','usable','reason'))}
            for row in item.get('quality_probes',[])[:64] if isinstance(row,dict)]
        public['download_attempts']=[_public_scalar_facts(row,('source','scene_id','status','reason_code','asset_id'))
            for row in item.get('download_attempts',[])[:64] if isinstance(row,dict)]
        result.append(public)
    return result


def public_research_result(value):
    """A research result is a bounded outcome, never a tool transcript."""
    if not isinstance(value,dict):
        return None
    result=_public_scalar_facts(value,('mode','status','support','availability_answered','analysis_completed',
        'science_pixels_analyzed','science_item_count','stop_reason','stop_reason_code'))
    for key in ('asset_ids','new_scientific_asset_ids'):
        result[key]=[identity for identity in value.get(key,[])[:8] if isinstance(identity,str) and re.fullmatch(r'[0-9a-f]{32}',identity)]
    if isinstance(value.get('request'),dict):
        try:
            request={key:item for key,item in value['request'].items() if key in ResearchRequest.model_fields}
            result['request']=ResearchRequest.model_validate(request).model_dump(mode='json')
        except ValidationError:
            pass
    return result


