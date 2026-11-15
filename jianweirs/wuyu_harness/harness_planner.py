"""User-scoped task planning through the official DSH profile/plugin runtime.

Only the validated final plan crosses into the product API. Provider reasoning,
raw session events and infrastructure diagnostics never become UI progress.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .schemas import TASK_LABELS, TaskKind, TaskRequest, ResearchRequest, OpticalPeriod
from .intent import SCIENCE_SOURCES, named_research_sources, requested_month_period, resolve_research_request
from .task_contracts import planner_task_guidance

ROOT = Path(__file__).resolve().parent.parent
STAGES = [("task_analysis", "任务理解与大纲"), ("source_selection", "按任务选星取图"),
          ("recognition_review", "见微识别与追加复核"), ("synthesis", "最终研判与资料补充")]


class PlannerError(RuntimeError):
    pass


class OutlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    title: str | None = Field(default=None, min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=300)


class PlannedFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_types: list[TaskKind] = Field(min_length=1, max_length=10)
    workflow_mode: str = Field(default='auto', max_length=40)
    center: tuple[float, float] | None = None
    date_start: str | None = None
    date_end: str | None = None
    jurisdiction: str = Field(default="中国", max_length=200)
    search_policy: bool = True
    research: ResearchRequest | None = None
    optical_periods: list[OpticalPeriod] = Field(default_factory=list,max_length=6,
        description='仅填写用户明确指定的年度版次或历史请求日期。例如世纪2024与2025用century+year两项，history246使用date。不得把版次年份当实际拍摄日期，不推测未指定的产品年份。')


class PlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: PlannedFields
    task_outline: list[OutlineItem] = Field(min_length=1, max_length=8)
    needs_input: str | None = Field(default=None, max_length=500)


def merge_dates(task: dict, original: TaskRequest, plan: PlanResult, explicit: set[str]) -> None:
    """Only accept inferred periods when the current question supplies a time cue."""
    research = ResearchRequest.model_validate(task.get("research") or {})
    from .optical_time import optical_time_intent, requested_calendar_day, requested_calendar_month, _years
    day = requested_calendar_day(original)
    month = requested_calendar_month(original)
    if day is not None and (month is None or month[0] <= day <= month[1] or re.search(r'发现|观察|举报|记录',original.objective)):
        intent = optical_time_intent(original)
        observation = intent['reference_role'] == 'user_reported_observation'
        # A discovery/complaint day guides nearest-date retrieval; it is not a
        # claim that an image was acquired that day or a mandatory single-day lock.
        bounds = (intent['requested_start'], intent['requested_end']) if observation else (day.isoformat(), day.isoformat())
        for key, value in zip(('date_start', 'date_end'), bounds):
            if key not in explicit:
                task[key] = value
        research.anchor_start = research.anchor_end = day
        from .intent import _adjacent_months
        if _adjacent_months(original.objective) is None:
            research.expand_adjacent_months = 0
        task['research'] = research.model_dump(mode='json')
        return
    month_period = requested_month_period(original, research)
    if month_period is not None:
        full_year_default = (original.date_start and original.date_end
            and original.date_start == date(original.date_start.year,1,1)
            and original.date_end == date(original.date_start.year,12,31)
            and month_period[0].year == original.date_start.year
            and month_period[1].year == original.date_end.year)
        for key, value in zip(("date_start", "date_end"), month_period):
            if key not in explicit or full_year_default:
                task[key] = value.isoformat()
        return
    from .optical_time import relative_period
    objective = original.objective
    years = set(_years(objective))
    relative = relative_period(objective)
    if not years and not relative and not re.search(r"当前|现在|最新|今天", objective):
        return  # Retain an inherited period, or None; never invent a search year.
    if years:
        earliest, latest = date(min(years), 1, 1), date(max(years), 12, 31)
    elif relative:
        earliest, latest = relative
    else:
        earliest = latest = date.today()
    for key in ("date_start", "date_end"):
        if key in explicit:
            continue
        value = getattr(plan.task, key)
        if value is None:
            if years:
                task[key] = (earliest if key == 'date_start' else latest).isoformat()
                continue
            if any(word in objective for word in ("当前", "现在", "最新", "今天")):
                task[key] = None
            continue
        try:
            inferred = date.fromisoformat(value)
        except ValueError:
            raise PlannerError("规划日期格式无法核对，请明确起止日期") from None
        if not earliest <= inferred <= latest:
            raise PlannerError("规划日期与本轮问题的时间范围不一致，请明确起止日期")
        task[key] = value


def validate_plan(raw: str, original: TaskRequest) -> dict:
    """Preserve explicit scope and owned identifiers; validate inferred fields."""
    cleaned = raw.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)[:-3].strip()
    try:
        plan = PlanResult.model_validate_json(cleaned)
    except ValueError:
        raise PlannerError("任务大纲格式校验未通过，请重新提交任务") from None
    task = original.model_dump(mode="json")
    explicit = original.model_fields_set
    if "task_types" not in explicit:
        task["task_types"] = [item.value for item in plan.task.task_types]
    if "research" not in explicit and plan.task.research is not None:
        task["research"] = plan.task.research.model_dump(mode="json")
    research_task = TaskRequest.model_validate(task)
    research_task.model_fields_set.intersection_update(explicit)
    if plan.task.research is not None:
        research_task.model_fields_set.add("research")
    research = resolve_research_request(research_task)
    task["research"] = research.model_dump(mode="json")
    # A model may only extract coordinates actually present in the user's text.
    if not ({"center", "bbox"} & explicit) and plan.task.center and plan.task.center != original.center:
        numbers = [float(value) for value in re.findall(r"(?<![\d.])-?\d{1,3}(?:\.\d+)?(?![\d.])", original.objective)]
        lon, lat = plan.task.center
        if not any(abs(lon - a) < 1e-7 and abs(lat - b) < 1e-7
                   for a, b in zip(numbers, numbers[1:])):
            raise PlannerError("规划得到的坐标无法在任务中核对，请明确经度和纬度")
        task.update(center=[lon, lat], bbox=None, coordinate_system="unknown")
    merge_dates(task, original, plan, explicit)
    if 'optical_periods' not in explicit and plan.task.optical_periods:
        from .optical_time import ANNUAL_SOURCE_YEARS, _years, annual_retrieval_years, history_catalog_only, optical_source_preferences, optical_time_intent, prioritize_optical_period
        if original.selected_sources:
            named=set(original.selected_sources)
        elif original.optical_source:
            named={original.optical_source}
        else:
            named=set(named_research_sources(original.objective))
            # A user can request a year without knowing product IDs. Defaults
            # may bind that year to an annual product; selected-source consent
            # and science-only requests remain separate restrictions.
            if prioritize_optical_period(original):
                named.update(optical_source_preferences(original))
        declared_years=set(optical_time_intent(original)['requested_years'])
        declared_years.update(_years(original.objective))
        if optical_time_intent(original)['nearest_context_allowed']:
            declared_years.update(annual_retrieval_years(original))
        accepted_periods = []
        for period in plan.task.optical_periods:
            # A model may encode a date-list lookup as a dated image request.
            # Keep the lookup intent, but do not turn an observation day or a
            # model-proposed archive date into third-provider download consent.
            if period.source == 'history246' and history_catalog_only(original):
                continue
            if period.source not in named:
                raise PlannerError('规划的年度或历史图源未在本轮请求中明确指定')
            fixed_selected_year=ANNUAL_SOURCE_YEARS.get(period.source) if period.source in named else None
            if period.year is not None and period.year not in declared_years and period.year != fixed_selected_year:
                raise PlannerError('规划的图源版次年份缺少用户请求依据')
            if period.date is not None:
                stamp=period.date
                iso=stamp.isoformat()
                chinese=f'{stamp.year}年{stamp.month}月{stamp.day}日'
                requested_day = optical_time_intent(original).get('requested_capture_date')
                if iso not in original.objective and chinese not in original.objective and iso != requested_day:
                    if optical_time_intent(original)['nearest_context_allowed'] and requested_day is None:
                        # A month/nearest-date request authorises a real archive
                        # lookup, not a model-invented capture day. The collector
                        # will select only returned catalogue dates itself.
                        continue
                    raise PlannerError('规划的历史请求日期缺少用户请求依据')
            accepted_periods.append(period)
        task['optical_periods']=[item.model_dump(mode='json',exclude_none=True) for item in accepted_periods]
    if 'optical_periods' not in explicit and not task['optical_periods']:
        from .optical_time import infer_optical_periods
        task['optical_periods'] = infer_optical_periods(TaskRequest.model_validate(task))
    if 'coordinate_system' not in explicit and re.search(r'WGS\s*84|EPSG\s*:\s*4326', original.objective, re.I):
        task['coordinate_system'] = 'wgs84'
    for key in ("jurisdiction", "search_policy"):
        if key not in explicit:
            task[key] = getattr(plan.task, key)
    directed_science = bool(named_research_sources(original.objective)) and bool(research.sources) and set(research.sources) <= SCIENCE_SOURCES
    if "acquire_optical" not in explicit:
        task["acquire_optical"] = bool(task.get("center") or task.get("bbox")) and research.mode != "availability" and not directed_science
    if research.mode == "availability":
        for key in ("consult_shensi", "search_policy"):
            if key not in explicit:
                task[key] = False
    from .task_modes import apply_workflow
    apply_workflow(task)
    if not task.get("center") and not task.get("bbox") and not task["asset_ids"] and task['workflow_mode'] != 'contextual_qa':
        raise PlannerError(plan.needs_input or "请提供经纬度、研究范围或待分析影像，再开始选星取图")
    try:
        verified = TaskRequest.model_validate(task)
    except ValueError:
        raise PlannerError("任务时间或空间范围需要补充，请核对任务输入") from None
    legacy = len(plan.task_outline) == len(STAGES) and all(item.id is None for item in plan.task_outline)
    outline, used_ids = [], set()
    for index, item in enumerate(plan.task_outline):
        key, legacy_title = STAGES[index] if legacy else (f"plan_{index + 1}", f"核查动作 {index + 1}")
        key = item.id or key
        if key in used_ids:
            key = f"plan_{index + 1}"
            while key in used_ids:
                key += "_next"
        used_ids.add(key)
        outline.append({"id": key, "title": item.title or legacy_title, "summary": item.summary})
    # Scope statements are rendered from validated values, not model prose.
    scope = (f"中心点 {verified.center[0]}, {verified.center[1]}" if verified.center else
             "用户指定范围" if verified.bbox else f"{len(verified.asset_ids)}张用户影像")
    if verified.coordinate_system == "unknown" and (verified.center or verified.bbox):
        scope += "，坐标系待核对"
    from .demand import demand_brief
    demand = demand_brief(verified)
    for item in outline:
        if item['id'] == 'task_analysis':
            item['summary'] = "、".join(TASK_LABELS[kind] for kind in verified.task_types) + "：" + scope + "。按用户目标组织后续核查。"
        elif item['id'] == 'source_selection':
            item['summary'] = demand['purpose']
    return {"task": verified.model_dump(mode="json"), "task_outline": outline, "demand": demand, "warnings": []}


def node_runtime() -> Path:
    configured = os.getenv("WUYU_NODE_PATH")
    if configured:
        return Path(configured)
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if bundled.is_file():
        return bundled
    import shutil
    candidate = shutil.which("node")
    if not candidate:
        raise PlannerError("尚未配置 Harness 所需的 Node 运行时")
    return Path(candidate)


async def plan_task(user: dict, task: TaskRequest, settings: Settings) -> dict:
    model = settings.models["main"]
    if not model.ready:
        raise PlannerError("请由管理员完成主模型配置后开始分析")
    task_id = uuid.uuid4().hex
    user_key = hashlib.sha256(str(user["id"]).encode()).hexdigest()[:24]
    folder = settings.output_dir / "planner" / user_key / task_id
    folder.mkdir(parents=True)
    # No data/search/specialist credentials are inherited by the planning profile.
    child_env = {key: value for key, value in os.environ.items()
                 if not re.search(r"(API.?KEY|TOKEN|SECRET|PASSWORD|^WUYU_|^DEEPSEEK_|^METASO_)", key, re.I)}
    if not model.trust_env:
        child_env = {key: value for key, value in child_env.items()
                     if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    child_env.update(DSH_HOME=str(folder / "harness-home"), WUYU_PLANNER_WORKDIR=str(folder),
                     WUYU_MAIN_API_KEY=model.api_key, WUYU_MAIN_BASE_URL=model.base_url,
                     WUYU_MAIN_MODEL=model.model, WUYU_MAIN_INPUT="text", WUYU_MAIN_MAX_TOKENS=str(model.max_tokens),
                     DSH_TELEMETRY_DISABLED="1")
    node = node_runtime()
    research_guidance = resolve_research_request(task)
    child_env["PATH"] = str(node.parent) + os.pathsep + child_env.get("PATH", "")
    command = [str(node), "--import", "tsx/esm", "apps/cli/src/bin.ts", "--profile", "headless",
               "--patch", str(ROOT / "harness-config/wuyu-main-compatible.cordis.yml"),
               "--patch", str(ROOT / "harness-config/wuyu-planner.cordis.yml"), "--json", "-"]
    payload = {"instruction": "只输出符合schema的JSON计划。先按用户问题选择一个主来源，仅在明确的覆盖、云量、分辨率、权限或证据缺口下才增加来源；不要枚举所有卫星或每次默认组合高分、多光谱、高光谱和SAR。不让用户回答已经提供的信息。task_outline按当前问题生成1至8项必要动作，可提供简短id和title；目录或元数据问题可只写一项检索，不强制取图、判读或生成报告。不输出推理过程。最新用户目标优先，父任务仅提供点位、参考年份及已有检索事实。research是执行契约：询问某卫星有没有数据、有哪些日期用availability；要求获取并分析用analysis；历史趋势用timeseries；未指定用auto。sources填真实接口来源ID。用户指定Sentinel、多光谱、SAR或高光谱时应直接安排对应目录检索，不能被底图配准或固废判读阻塞；不必重跑高分。若本轮同时要求确认是否存在堆存及历史趋势、需要先看清目标形态，则research.include_optical_context=true；单纯科学图源追问或目录问题保持false。附近几个月需expand_adjacent_months，并使用继承年份解析跨年范围；已知有云不等于没有目录。完整可得性需full_catalog=true。只填有用户或父任务时间依据的日期。缺位置也无影像则needs_input给简短补充项。涉及报告/监管依据时search_policy=true；单纯影像目录可得性不用法规检索。",
               "available_capabilities": "工具按当前证据缺口迭代选择：可在WGS84任务范围独立检索Sentinel-2、Landsat、HLS多光谱，EMIT、EnMAP高光谱和Sentinel-1等SAR目录；可跨月份及较大时间范围检索并逐景记录实际日期与获取状态。目录、预览、科学数据下载权限和点位有效质量需分别核对。已接通Sentinel-2局部科学波段、SCL质量筛查、元数据定标及SAR数据窗口，并由科学模型分析；目录有场景不代表下载成功或完成像元分析。高分可按需多来源裁图、局部相对配准、识别标注与交叉复核。高分相对配准失败不阻塞独立坐标上的科学目录查询，但不能把未知底图目标自动转移到科学影像。根据当前观察追加时相、范围或来源，最后综合实际调用结果。不承诺未经实际执行的定量变化、面积/体积/距离测量、小模型验证或持续监测。用自然中文写业务动作，不把四阶段变成每次必做的固定取证流程。",
               "task_contract_guidance": planner_task_guidance(
                   research_guidance.mode, research_guidance.include_optical_context),
               "schema": PlanResult.model_json_schema(),
               "current_date": date.today().isoformat(),
               "explicit_fields": sorted(task.model_fields_set), "user_task": task.model_dump(mode="json")}
    from .task_modes import stored_workflow, LABELS
    workflow = stored_workflow(task)
    payload['workflow_contract'] = {'mode':workflow, 'label':LABELS[workflow], 'waste_status':task.waste_status}
    payload['instruction'] += ('本轮主范式为'+LABELS[workflow]+'。不得把环境风险、时序或当前追问改写成通用疑点扫描。'
        '环境风险直接围绕已知堆存场景、源—路径—受体、风险等级与国家地方标准安排见微初评和Gemini终审；'
        '用户确认仅是用户提供的事实依据，不等于法定固废或污染鉴别。时序需夹定最早出现与最后仍存在的实际日期。'
        '输出结构要服务当前提问，不强制每次填满相同四步；仅对当前问题所需数据安排工具。')
    payload["instruction"] += (
        "research的非auto模式仅执行科学卫星目录和原生窗口分析；普通高分形态、类别、边界或周边核查必须用auto。"
        "Google、Bing、Esri/ArcGIS、高德、腾讯、四维等底图不能单独作为analysis、availability或timeseries的科学来源。"
        "科学research.sources应选Sentinel-2、Landsat、HLS、EnMAP、EMIT、Sentinel-1等真实科学来源ID；"
        "合法的未指定来源历史问题可留sources为空，由后续按缺口选科学数据。"
        "同时需要高分形态与科学时序时用include_optical_context表达高分需求，不将高分底图冒充科学来源。"
        "用户给出年份或具体日期时，先安排该点该时段的年度产品与历史目录，不能先取多个当前底图后才说没查历史。"
        "高分年度源世纪空间用century+year、四维2025用siwei2025+year2025、吉林2024用jilin2024+year2024；年份须来自用户或继承时间。"
        "如用户说12月1号发现且已知父任务年份，保留该年的12月1日作为用户观察日期，并以其排序附近历史拍摄日期；发现日期不等于拍摄日期。"
        "未明确要求前后月份，不自行增加expand_adjacent_months；年度版次和2026组名不能冒充实际拍摄年份。"
        "按当前任务和有效证据决定是否补源，不设默认至少两源；不同底图渠道不是独立卫星。"
        "只查询246目录或最接近历史日期时，由执行器查询optical.times，不要给history246填optical_periods；"
        "目录查询不是历史影像下载授权，尤其不能把用户发现日直接写成history246取图日期。"
        "明确月份须保留完整年月，例如2026年1月不能扩成2026全年。取不到该月时，按已授权图源查询并实际读取前后最近目录候选，跨年日期需列差值。"
        "四维2025和世纪空间year参数只选择年度版次，不支持按12月等月份定向选图；可作邻近参考，但必须核对返回实际日期，不能说已取得该月实拍。"
    )
    if task.complaint_scan or re.search(r'举报|线索核查|覆盖.*扫描|扫描.*区域',task.objective):
        skill = ROOT / 'harness-config/skills/complaint-scan/SKILL.md'
        if skill.is_file():
            payload['complaint_scan_guidance'] = skill.read_text(encoding='utf-8')[:16000]
        payload['instruction'] += ('举报地点必须来自真实检索候选或用户明确坐标，不从匿名企业猜位置。'
            '仅在用户确认范围和窗口预算后安排分块扫描；保留未覆盖、不可观测和未知时相。'
            '区域窗口的bbox和complaint_scan由服务端计划确定，规划器不得扩大或改动。')
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT / "deepseek-harness", env=child_env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, limit=4 * 1024 * 1024)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(payload, ensure_ascii=False).encode()), model.timeout + 30)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except TimeoutError:
        process.kill()
        await process.wait()
        raise PlannerError("任务规划超时，请稍后重试或检查主模型服务") from None
    if len(stdout) > 4 * 1024 * 1024 or process.returncode:
        diagnostic = (stderr + stdout[:65536]).decode('utf-8', 'replace')
        if re.search(r'\b402\b|insufficient\s+balance|insufficient_quota', diagnostic, re.I):
            raise PlannerError('主模型账户余额不足（HTTP 402），请管理员充值或切换已配置通道；本次没有执行取图。')
        if re.search(r'\b401\b|invalid_api_key|authentication_error', diagnostic, re.I):
            raise PlannerError('主模型认证未通过（HTTP 401），请管理员检查通道配置；密钥未显示。')
        raise PlannerError("Harness 任务规划未完成，请由管理员检查主模型连接")
    final, session_id, events = None, None, []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        events.append(event.get("type"))
        if event.get("type") == "session":
            session_id = event.get("sessionId") or event.get("session_id")
        if event.get("type") == "final":
            final = event.get("text")
    if not final:
        raise PlannerError("Harness 未返回可执行大纲，请重新提交任务")
    result = validate_plan(final, task)
    result.update(engine="deepseek-harness", session_id=session_id,
                  elapsed_seconds=round(time.monotonic() - started, 2))
    # Only the plan and coarse trace are made public; no raw reasoning or tokens.
    (folder / "validated-plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "trace.json").write_text(json.dumps({"engine": "deepseek-harness", "events": events,
        "session_id": session_id, "elapsed_seconds": result["elapsed_seconds"]}), encoding="utf-8")
    return result
