"""Resolve the current research request without treating old answers as commands.

The planner supplies the structured contract. These conservative rules retain
explicit source questions and date references when a planner omits them.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import re

from .schemas import ResearchRequest, TaskRequest

SCIENCE_SOURCES = frozenset({"sentinel2", "landsat", "hls_s30", "hls_l30", "emit", "enmap",
    "sentinel1-rtc", "sentinel1-grd", "sentinel1-slc", "alos-palsar-rtc", "nisar-gcov", "jaxa-palsar-mosaic"})
_SOURCE_NAMES = (
    ("sentinel2", r"sentinel[\s_-]*2(?:[abc])?(?![a-z0-9])|哨兵[二2]号?"),
    ("sentinel1-rtc", r"sentinel[\s_-]*1(?:[abc])?(?![a-z0-9])|哨兵[一1]号?|(?<![a-z])SAR(?![a-z])|雷达"),
    ("landsat", r"landsat|陆地卫星"), ("hls_s30", r"hls[\s_-]*s30"),
    ("hls_l30", r"hls[\s_-]*l30"), ("emit", r"(?<![a-z0-9])EMIT(?![a-z0-9])"),
    ("enmap", r"(?<![a-z0-9])EnMAP(?![a-z0-9])"),
    ("history246", r"history246|(?<![\da-zA-Z.])246\s*(?:的\s*)?(?:历史|时序|目录|影像|日期|图源)|谷歌(?:历史|时序)|Google\s*(?:history|historical)"),
    ("century", r"世纪空间|世纪卫星|(?<![a-z0-9])century(?![a-z0-9])"),
    ("jilin2024", r"吉林一号(?:2024)?|jilin2024"),
    ("siwei2025", r"四维(?:地球)?\s*2025|siwei2025"),
    ("gaofen2026", r"2026(?:组)?高清|高清2026|gaofen2026"),
    ("google", r"(?<![a-z0-9])Google(?![a-z0-9])|谷歌"),
    ("bing", r"(?<![a-z0-9])Bing(?![a-z0-9])|必应"),
    ("tencent", r"腾讯"), ("amap", r"高德"),
    ("arcgis", r"(?<![a-z0-9])(?:Esri|ArcGIS)(?![a-z0-9])"),
    ("tianditu", r"天地图|(?<![a-z0-9])Tianditu(?![a-z0-9])"),
    ("mapbox", r"(?<![a-z0-9])Mapbox(?![a-z0-9])"),
    ("mapquest", r"(?<![a-z0-9])Mapquest(?![a-z0-9])"),
    ("siwei_daily", r"四维(?:地球)?(?:日新|每日)|siwei_daily"),
    ("siwei", r"四维(?!地球(?:日新|每日)|日新|每日)(?:地球)?|(?<![a-z0-9])Siwei(?![_a-z0-9])"),
)
_NEGATIVE = r"不用|不要(?!漏|遗漏|从|把|将|依据|根据)|不需|无需|暂不|别用|不(?:查|调用|使用|分析|获取)|\bwithout\b|\bdo\s+not\b"
_CONDITIONAL = r"按需|必要时|需要时|如有必要|如果需要|视情况|酌情|可选|决定是否|选择是否|是否需要|要不要|有无必要|可考虑|只有[^，。；;]{0,40}才(?:加|用|补|查|取|调)|if\s+needed|as\s+needed|optional"
_CLAUSE_SPLIT = r"[，,。；;！？!?\n]|但是|不过|而是"
_SOURCE_SWITCH = r"只(?:要|用|看|查|查询|获取|分析|检索)|仅(?:用|看|查|查询|获取|分析|检索)|改(?:用|查|看|查询|获取|检索)|换用|切换(?:到|为)"


def _clause_sources(clause: str) -> list[str]:
    matches = [source for source, pattern in _SOURCE_NAMES if re.search(pattern, clause, re.I)]
    if 'history246' in matches: matches = [s for s in matches if s != 'google']
    if 'siwei2025' in matches: matches = [s for s in matches if s != 'siwei']
    if 'tianditu' in matches and any(s in matches for s in ['mapbox', 'mapquest']) and re.search(r'天地图路网|天地路网',clause):
        matches.remove('tianditu')  # Overlay name, not an extra imagery request.
    if not any(source.startswith("sentinel") for source in matches) and re.search(r"(?<![a-z0-9])Sentinel(?![a-z0-9])|哨兵", clause, re.I):
        matches.append("sentinel2")
    if not matches and re.search(r"多光谱|NDVI|植被指数|近红外|反射率", clause, re.I):
        matches.append("sentinel2")
    if not matches and "高光谱" in clause:
        matches.extend(("enmap", "emit"))
    return matches


def named_research_sources(text: str) -> list[str]:
    """Only current user text is considered; no previous model narrative."""
    sources = []
    for clause in re.split(_CLAUSE_SPLIT, text):
        if not re.search(_NEGATIVE + "|" + _CONDITIONAL, clause, re.I):
            sources.extend(_clause_sources(clause))
    return list(dict.fromkeys(sources))


def _source_limits(text: str) -> tuple[set[str], bool]:
    """Keep exclusions/conditional permissions out of model-added sources."""
    restricted, explicit_switch = set(), False
    for clause in re.split(_CLAUSE_SPLIT, text):
        mentions = _clause_sources(clause)
        if re.search(_NEGATIVE + "|" + _CONDITIONAL, clause, re.I):
            restricted.update(mentions)
            if "sentinel1-rtc" in mentions:
                restricted.update(source for source in SCIENCE_SOURCES if source.startswith("sentinel1-"))
                if re.search(r"(?<![a-z])SAR(?![a-z])|雷达", clause, re.I):
                    restricted.update({"alos-palsar-rtc", "nisar-gcov", "jaxa-palsar-mosaic"})
        elif mentions and re.search(_SOURCE_SWITCH, clause, re.I):
            explicit_switch = True
    return restricted, explicit_switch


def _adjacent_months(text: str) -> int | None:
    # "这附近看看，12月1号发现" describes spatial surroundings, not a
    # request to expand the temporal window by two months.
    associated = (r"月(?:份)?[^，,。；;！？!?\n]{0,8}(?:附近|邻近|相邻|前后)|"
                  r"(?:附近|邻近|相邻|前后)\s*(?:[一二两三四五六七八九十几]|\d{1,2})\s*个?月|周边月份")
    if "月" not in text or not re.search(associated, text):
        return None
    number = re.search(r"(?:前后|附近|邻近|相邻)\s*([一二两三四五六七八九十]|\d{1,2})\s*个?月", text)
    if not number:
        return 2
    value = number[1]
    count = int(value) if value.isdigit() else {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}[value]
    return min(12, count)


def resolve_research_request(task: TaskRequest) -> ResearchRequest:
    """Prefer the latest objective over inherited intent; honor model structure.

    Open-ended analysis still belongs to the model. This fallback recognizes
    source names, existence questions and explicit temporal-analysis goals only.
    """
    if task.online_supplement == 'uploaded_only':
        return ResearchRequest(mode='auto', sources=[])
    request = (task.research or ResearchRequest()).model_copy(deep=True)
    text = task.objective
    named = named_research_sources(text)
    if task.selected_sources:
        named = list(task.selected_sources)
        request.sources = list(task.selected_sources)
    model_contract = "research" in task.model_fields_set and task.research is not None and task.research.mode != "auto"
    restricted, explicit_switch = _source_limits(text)
    if task.selected_sources:
        restricted, explicit_switch = set(), True
    had_model_sources = bool(request.sources)
    request.sources = [source for source in request.sources if source not in restricted]
    routine_auxiliary = bool(re.search(r"堆存|倾倒|固废|垃圾", text) and re.search(r"结合|辅助", text)
                             and not re.search(r"(?:用|通过|根据)(?:[^，。；;]{0,12})(?:多光谱|Sentinel|SAR|雷达)(?:[^，。；;]{0,12})(?:确认|判断|核查|分析)", text, re.I))
    if routine_auxiliary and not model_contract:
        named = [source for source in named if source not in SCIENCE_SOURCES]
    if named:
        if model_contract and not explicit_switch and (set(request.sources) & SCIENCE_SOURCES):
            # A current model plan may deliberately add science to highres
            # history. Only inherited plans are replaced wholesale by new names.
            request.sources = list(dict.fromkeys([*request.sources, *named]))
        else:
            request.sources = named
        request.sources = [source for source in request.sources if source not in restricted]
    science_named = any(source in SCIENCE_SOURCES for source in named)
    availability_question = bool(re.search(r"没有吗|都没有|有吗|有没有|有无|哪些(?:日期|时间|月份|影像|数据)|可用(?:日期|时间|影像|数据)|数据可得|(?:附近|相邻|邻近).*几?个?月", text, re.I))
    catalogue_subject = science_named or bool(re.search(r"(?:有没有|有无|哪些|可用).{0,8}(?:影像|数据|目录|日期)|(?:影像|数据|目录).{0,8}(?:没有吗|都没有|有吗)|数据可得",text))
    if model_contract:
        pass
    elif re.search(r"历史(?:变化|趋势)|变化趋势|时间序列|时序|逐月|每个月|全时[间相]|前后变化", text, re.I):
        request.mode = "timeseries"
    elif availability_question and catalogue_subject:
        request.mode = "availability"
    elif science_named and re.search(r"分析|辅助|证明|比较|看看|看一下|确认|判断|核查|研判|获取|调取|下载|查看|检索|查询", text, re.I):
        request.mode = "analysis"
    # Research modes execute scientific catalogues/windows. A planner choosing
    # only basemap providers still means ordinary optical work. Keep source
    # preferences, and leave source-free history and mixed science intact.
    basemap_only = bool(request.sources) and not (set(request.sources) & SCIENCE_SOURCES)
    conditional_only = had_model_sources and not request.sources and bool(restricted)
    if request.mode != "auto" and (basemap_only or conditional_only):
        # Dated high-resolution history is executed by the optical temporal
        # collector, separate from numerical scientific archives.
        request.mode = "auto"
        request.include_optical_context = False
    adjacent = _adjacent_months(text)
    if adjacent is not None:
        request.expand_adjacent_months = adjacent
    elif re.search(r"(?<!\d)(?:19|20)\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*(?:日|号)", text):
        # A specific year/day is not permission to add neighbouring months.
        # In particular, a reported discovery day must survive as a day cue.
        request.expand_adjacent_months = 0
    if request.mode == "availability" or re.search(r"全(?:部|历史|时间|时相)|完整(?:目录|序列)|所有(?:日期|月份|影像|时相)", text):
        request.full_catalog = True
    if not model_contract or "include_optical_context" not in request.model_fields_set:
        request.include_optical_context = bool(request.mode == "timeseries" and not science_named
            and not re.search(r"不要高分|不用高分|无需高分|只(?:查|看|用).*?(?:多光谱|Sentinel|SAR|雷达)",text,re.I)
            and re.search(r"(?:是否有|是否存在|有没有|核查).{0,45}(?:堆存|倾倒|固废)",text))
    period = _named_month_anchor(task)
    if period:
        request.anchor_start, request.anchor_end = period
    elif request.expand_adjacent_months and not request.anchor_start and not request.anchor_end:
        request.anchor_start, request.anchor_end = task.date_start, task.date_end
    # Generic hyperspectral requests need one primary catalogue, rather than
    # enumerating every supported sensor. Explicit EnMAP/EMIT names still win.
    if set(request.sources) == {'enmap', 'emit'} and not re.search(r'EnMAP|EMIT|全部|所有|对比|比较|都查', text, re.I):
        request.sources = ['enmap']
    if task.selected_sources and set(task.selected_sources) & SCIENCE_SOURCES:
        request.sources = list(task.selected_sources)
        if request.mode == 'auto':
            request.mode = 'analysis'
        request.include_optical_context = bool(set(task.selected_sources) - SCIENCE_SOURCES)
    return request


def _named_month_anchor(task: TaskRequest) -> tuple[date, date] | None:
    """Resolve one named month and its requested neighbours using inherited year.

    An unqualified month without any known year remains for the model/user to
    resolve; an old answer cannot invent a date or silently keep a one-month lock.
    """
    from .optical_time import requested_calendar_day, requested_calendar_month
    day = requested_calendar_day(task)
    month = requested_calendar_month(task)
    if day is not None and (month is None or month[0] <= day <= month[1]):
        return day, day
    if month is not None:
        return month
    matches = list(re.finditer(r"(?<!\d)(?:(19\d{2}|20\d{2})\s*年\s*)?(1[0-2]|0?[1-9])\s*月", task.objective))
    if len(matches) != 1:
        return None
    match = matches[0]
    years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", task.objective)
    inherited = (task.research.anchor_start or task.research.anchor_end) if task.research else None
    inherited = inherited or task.date_start or task.date_end
    year = int(match[1] or years[-1]) if match[1] or years else inherited.year if inherited else None
    if year is None:
        return None
    month = int(match[2])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(value: date, count: int, *, end: bool = False) -> date:
    year, month_index = divmod(value.year * 12 + value.month - 1 + count, 12)
    month = month_index + 1
    return date(year, month, calendar.monthrange(year, month)[1] if end else 1)


def requested_month_period(task: TaskRequest, research: ResearchRequest | None = None) -> tuple[date, date] | None:
    anchor = _named_month_anchor(task)
    if anchor is None:
        return None
    if re.search(r"(?:到|至|直到)\s*(?:现在|当前|今天|今日)|以来|至今",task.objective):
        return anchor[0], date.today()
    adjacent = (research or resolve_research_request(task)).expand_adjacent_months
    return _shift_month(anchor[0], -adjacent), _shift_month(anchor[1], adjacent, end=True)


def research_time_scope(task: TaskRequest, request: ResearchRequest | None = None) -> dict:
    """Expand the original anchor once, then union the planned retrieval scope."""
    request = request or resolve_research_request(task)
    named_period = requested_month_period(task, request)
    full_year_default = (task.date_start and task.date_end
        and task.date_start == date(task.date_start.year,1,1)
        and task.date_end == date(task.date_start.year,12,31))
    if named_period and not request.expand_adjacent_months and (not (task.date_start and task.date_end) or full_year_default):
        return {'start':named_period[0],'end':named_period[1],
                'requested_anchor_start':request.anchor_start,'requested_anchor_end':request.anchor_end,
                'time_basis':'current_named_month_before_broad_year_default'}
    if task.date_start and task.date_end and not request.expand_adjacent_months:
        return {'start':task.date_start,'end':task.date_end,
                'requested_anchor_start':request.anchor_start,'requested_anchor_end':request.anchor_end,
                'time_basis':'user_requested_period'}
    anchor = _named_month_anchor(task)
    anchor_start = anchor[0] if anchor else request.anchor_start
    anchor_end = anchor[1] if anchor else request.anchor_end
    if anchor_start is None and anchor_end is None and request.expand_adjacent_months:
        anchor_start, anchor_end = task.date_start, task.date_end
    starts = [value for value in (task.date_start, anchor_start) if value]
    ends = [value for value in (task.date_end, anchor_end) if value]
    if request.expand_adjacent_months:
        if anchor_start: starts.append(_shift_month(anchor_start, -request.expand_adjacent_months))
        if anchor_end: ends.append(_shift_month(anchor_end, request.expand_adjacent_months, end=True))
    today = date.today()
    end = max(ends) if ends else today
    start = min(starts) if starts else end - timedelta(days=366)
    if not starts and not ends:
        basis = "default_recent_12_months_not_full_archive"
    elif request.expand_adjacent_months:
        basis = "user_anchor_and_adjacent_months"
    else:
        basis = "user_requested_period" if task.date_start and task.date_end else "partial_user_period_with_recent_12_months_default"
    return {"start":start,"end":end,"requested_anchor_start":anchor_start,"requested_anchor_end":anchor_end,"time_basis":basis}
