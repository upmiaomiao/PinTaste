"""User-grounded optical time intent, distinct from image capture metadata.

The year/day in a request is a retrieval target. A reported discovery day is
an observation reference, never a satellite acquisition date.
"""
from __future__ import annotations

from datetime import date, timedelta
import calendar
import re


from .source_catalog import ANNUAL_SOURCE_YEARS, CENTURY_YEARS, HISTORICAL_OPTICAL, century_year_supported  # noqa: F401


def history_catalog_only(task):
    """A 246 date-list lookup does not authorize downloading a dated image."""
    from .intent import _clause_sources, _CLAUSE_SPLIT, _NEGATIVE, _CONDITIONAL
    clauses = []
    history_focus = 'history246' in task.selected_sources or task.optical_source == 'history246'
    for clause in re.split(_CLAUSE_SPLIT, task.objective):
        sources = _clause_sources(clause)
        if sources:
            history_focus = 'history246' in sources
        if history_focus:
            clauses.append(clause)
    # An explicit restriction wins even if an earlier clause asks for an
    # image. Scope it to history clauses, so excluding another annual provider
    # does not remove an independently authorised history-image request.
    for clause in clauses:
        if re.search(r'(?:只|仅)(?:需|需要)?(?:查询?|查看|列出|检索|核对)[^，,。；;]*(?:目录|日期|时相|列表|清单)',clause):
            return True
        if re.search(r'(?:不|不要|无需|不用|暂不|别)(?:再|实际|进行|去|要|需)?\s*(?:下载|取图|调图|获取[^，,。；;]*(?:影像|图片|图像))',clause):
            return True

    lookup = False
    for clause in clauses:
        if re.search(_NEGATIVE+'|'+_CONDITIONAL,clause,re.I):
            continue
        if re.search(r'(?:取|获取|调取|下载)(?:不到|不了|失败)|未(?:取到|获取|下载)|无法(?:获取|下载|取图)',clause):
            continue
        if re.search(r'取图|调图',clause):
            return False
        # Inspect the requested object over the complete clause. Long phrases
        # such as "获取246目录中最接近目标月份的真实影像作对照" must not be
        # truncated at a fixed character count. Image-date/catalogue objects
        # remain metadata requests rather than pixel-download consent.
        for match in re.finditer(r'影像|图片|图像|一景|一幅|一张',clause):
            prefix,suffix = clause[:match.start()],clause[match.end():]
            if re.match(r'(?:的)?(?:实际|拍摄|获取|采集|成像|可用)?(?:目录|日期|时间|时相|列表|清单|元数据)',suffix):
                continue
            if (re.search(r'下载|获取|调取|拿到|取得|提取|选取|取|分析|解读|判读',prefix)
                    or re.search(r'分析|解读|判读|作对照|进行对照|用于对照',suffix)):
                return False
        lookup = lookup or bool(re.search(r'目录|日期|时相|可得|可用|存档列表',clause))
    return lookup


def _years(text):
    years = []
    for match in re.finditer(r"(?<![\d.a-zA-Z])((?:19|20)\d{2})(?![\d.a-zA-Z])", text):
        # A current-source group name alone is not a requested capture year.
        if re.match(r"\s*(?:组(?:影像|高清)?|高清(?:图源|影像)?)", text[match.end():]):
            continue
        if re.match(r"\s*(?:吨|千克|公斤|平方米|平米|平方公里|立方米|立方|公顷|亩|㎡|栋|幢|元|万元|亿元|方|[kK]?[mM]\s*(?:[²³23]|\^[23])|[kK][gG])", text[match.end():]):
            continue
        value = int(match[1])
        if value not in years:
            years.append(value)
    return years


def relative_period(text, today=None):
    """Relative cues ("近3个月", "去年", "上月", "今年", "最近") as calendar bounds.

    This is the only place relative time is turned into dates; the planner and
    the conversation layer call it instead of keeping their own calendars.
    """
    today = today or date.today()
    match = re.search(r"(?:近|过去|最近)\s*(\d{1,4})\s*(?:个)?\s*([天日周月年])", text)
    if match:
        days = int(match[1]) * {"天": 1, "日": 1, "周": 7, "月": 31, "年": 366}[match[2]]
        return today - timedelta(days=min(days, 3660)), today
    if "前年" in text:
        return date(today.year - 2, 1, 1), date(today.year - 2, 12, 31)
    if "去年" in text:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if "上月" in text or "上个月" in text:
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if "本月" in text or "这个月" in text:
        return today.replace(day=1), today
    if "今年" in text:
        return date(today.year, 1, 1), today
    if re.search(r"最近一年|近一年", text) or (re.search(r"最近", text) and not re.search(r"(?:过去|以前|历史|早期|当年)\S{0,4}(?:到|至|和|与)\S{0,2}最近", text)):
        return today - timedelta(days=366), today
    return None


def _inherited_year(task):
    research = task.research
    anchors = [getattr(research, key, None) for key in ("anchor_start", "anchor_end")]
    anchors = [value for value in anchors if value]
    if anchors and len({value.year for value in anchors}) == 1:
        return anchors[0].year
    values = [value for value in (task.date_start, task.date_end) if value]
    return values[0].year if values and len({value.year for value in values}) == 1 else None


def requested_calendar_day(task):
    """Resolve an explicit day, including a month/day with a known parent year."""
    text = task.objective
    matches = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text))
    values = []
    for match in matches:
        try:
            values.append(date(*(int(value) for value in match.groups())))
        except ValueError:
            pass
    for match in re.finditer(r"(?<!\d)(?:((?:19|20)\d{2})\s*年\s*)?(1[0-2]|0?[1-9])\s*月\s*(3[01]|[12]\d|0?[1-9])\s*(?:日|号)", text):
        years = _years(text)
        year = int(match[1]) if match[1] else years[0] if len(years) == 1 else _inherited_year(task)
        if year is None:
            continue
        try:
            values.append(date(year, int(match[2]), int(match[3])))
        except ValueError:
            pass
    values = list(dict.fromkeys(values))
    return values[0] if len(values) == 1 else None


def requested_calendar_month(task):
    """Resolve a named month (or explicit month range), before a bare year.

    Other months mentioned as possible fallback examples do not expand the
    primary target. A month/day observation is handled by the day resolver.
    """
    text = task.objective
    pattern = r"(?<!\d)(?:((?:19|20)\d{2})\s*年\s*)?(十二|十一|十|[一二三四五六七八九]|1[0-2]|0?[1-9])\s*月(?:份)?"
    matches = list(re.finditer(pattern, text))
    matches += list(re.finditer(r"(?<![\d.])((?:19|20)\d{2})[-/](1[0-2]|0?[1-9])(?![-/\d])", text))
    matches.sort(key=lambda match: match.start())
    years = _years(text)
    known = years[0] if len(years) == 1 else _inherited_year(task)
    rows = []
    chinese = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'十一':11,'十二':12}
    for match in matches:
        clause_prefix = re.split(r'[，,。；;！？!?\n]',text[:match.start()])[-1]
        if re.search(r'(?:不能|不可|不得|不应|不要|不代表|并非)[^，,。；;]*(?:声称|当成|当作|写成|称为|证明|是)',clause_prefix):
            continue  # "年度版不能声称是12月拍摄" is not a new target month.
        if re.match(r"\s*\d{1,2}\s*(?:日|号)", text[match.end():]):
            continue
        year = int(match[1]) if match[1] else known
        if year is None:
            continue
        month = int(match[2]) if match[2].isdigit() else chinese[match[2]]
        if rows and not match[1] and month < rows[-1][0].month and re.search(r'到|至',text[rows[-1][3]:match.start()]):
            year = rows[-1][0].year + 1
        first = date(year,month,1)
        rows.append((first,date(year,month,calendar.monthrange(year,month)[1]),match.start(),match.end()))
        known = year
    if not rows:
        return None
    if len(rows)>1 and re.search(r'到|至',text[rows[0][3]:rows[-1][2]]):
        return rows[0][0],rows[-1][1]
    return rows[0][0],rows[0][1]


def optical_time_intent(task):
    """Return JSON-safe target bounds and an independently labelled day cue."""
    text = task.objective
    years = _years(text)
    day = requested_calendar_day(task)
    month = requested_calendar_month(task)
    if not month and not day and (not years or re.search(r'目标月份|原定月份|该月|当月',text)):
        # A short follow-up can inherit the already confirmed month without
        # repeating its name. Prefer its anchor over expanded retrieval bounds.
        candidates = [(getattr(task.research, 'anchor_start', None),
                       getattr(task.research, 'anchor_end', None)),
                      (task.date_start, task.date_end)]
        for first, last in candidates:
            if (first and last and first.day == 1 and first.year == last.year
                    and first.month == last.month
                    and last.day == calendar.monthrange(last.year,last.month)[1]):
                month = (first,last)
                break
    observation = bool(day and re.search(r"发现|看见|看到|举报|反映|拍到|记录|观察|上报", text))
    start, end = task.date_start, task.date_end
    if years:
        # A bare user year has priority over stale inherited recent-period plans.
        start, end = date(min(years), 1, 1), date(max(years), 12, 31)
        if task.date_start and task.date_end and start <= task.date_start <= task.date_end <= end:
            start, end = task.date_start, task.date_end
    from .intent import _adjacent_months, research_time_scope, resolve_research_request
    if (day and observation and task.research and task.research.expand_adjacent_months
            and _adjacent_months(text) is None):
        # Recover the known observation year from an older month-only plan that
        # expanded a day cue into neighbouring months without user permission.
        start, end = date(day.year, 1, 1), date(day.year, 12, 31)
    if day and not (start or end):
        start, end = date(day.year, 1, 1), date(day.year, 12, 31)
    relative_span = False
    if not (start or end or day or month or years):
        relative = relative_period(text)
        if relative:
            start, end = relative
            relative_span = True
    if month:
        start, end = month
        if day and not observation and start <= day <= end:
            start = end = day
        if re.search(r'(?:到|至|直到)\s*(?:现在|当前|今天|今日)|以来|至今',text):
            end = date.today()
        years = list(range(start.year,end.year+1))
    if not years and start and end:
        years = list(range(start.year, end.year + 1))
    constrained = bool(start or end or day)
    day_is_target = bool(day and (not month or start <= day <= end))
    reference = day if day_is_target else (start + (end - start) // 2 if start and end else start or end)
    precision = 'day' if day and not month or day and start==end==day else 'month' if month and start.year==end.year and start.month==end.month else 'range' if month or relative_span else 'year' if constrained else 'unspecified'
    # A restriction on *claiming* a date is not a ban on retrieving context:
    # “不要把异时参考当成一月证据” still permits the requested adjacent scan.
    nearest_restricted = re.search(r'(?:严格|仅限|只(?:要|取|查|用|看))[^，,。；;]{0,12}(?:该月|当月|指定月份|指定日期|(?:19|20)\d{2}年\d{1,2}月)|'
        r'(?:不要|不用|无需|禁止|不允许|别)(?:再)?(?:获取|调取|下载|使用|采用|查询|补充|取用|取)?\s*(?:相邻|邻近|异时|其他月份|其他年份)',text)
    nearest_allowed = precision in {'month','day'} and not nearest_restricted
    # Keep the question's anchor separate from an authorised adjacent search
    # window. Expanding January by two months must not turn the evidence target
    # into November--March (or all of the year).
    research = resolve_research_request(task)
    scope = research_time_scope(task, research) if constrained else {}
    return {
        "constrained": constrained,
        "requested_start": start.isoformat() if start else None,
        "requested_end": end.isoformat() if end else None,
        "requested_years": years,
        "reference_date": reference.isoformat() if reference else None,
        "observation_date": day.isoformat() if observation else None,
        "requested_capture_date": day.isoformat() if day_is_target and not observation else None,
        "reference_role": "user_reported_observation" if observation and day_is_target else "requested_capture_date" if day_is_target else "requested_period" if constrained else "general_history",
        "calendar_precision": precision,
        "requested_month": start.strftime('%Y-%m') if precision=='month' else None,
        "nearest_context_allowed": nearest_allowed,
        "requested_anchor_start": start.isoformat() if start else None,
        "requested_anchor_end": end.isoformat() if end else None,
        "retrieval_start": scope['start'].isoformat() if scope.get('start') else None,
        "retrieval_end": scope['end'].isoformat() if scope.get('end') else None,
        "retrieval_time_basis": scope.get('time_basis'),
        "adjacent_months": research.expand_adjacent_months,
        "nearest_candidate_search_bounded": bool(nearest_allowed and research.expand_adjacent_months),
        "requested_date_is_capture_evidence": False,
        "edition_is_capture_date": False,
    }


def annual_retrieval_years(task):
    """Annual product selector only; adjacent editions are labelled context."""
    intent=optical_time_intent(task)
    supported=list(CENTURY_YEARS)
    exact=[year for year in intent['requested_years'] if year in supported]
    if exact:
        return exact
    if intent['nearest_context_allowed'] and intent['reference_date']:
        reference=date.fromisoformat(intent['reference_date'])
        return [min(supported,key=lambda year:(abs(year-reference.year),-year))]
    return []


def optical_source_preferences(task):
    """Prefer named products; otherwise use editions matching the requested year."""
    from .intent import SCIENCE_SOURCES, named_research_sources
    named = [value for value in (task.selected_sources or named_research_sources(task.objective)) if value not in SCIENCE_SOURCES]
    if task.optical_source:
        named = [task.optical_source]
    intent = optical_time_intent(task)
    years = annual_retrieval_years(task) or intent["requested_years"]
    if named:
        return list(dict.fromkeys("siwei2025" if source == "siwei" and years == [2025] else source for source in named))
    if not intent["constrained"]:
        return []
    products = []
    if any(century_year_supported(year) for year in years):
        products.append("century")
    products.extend(source for source, year in ANNUAL_SOURCE_YEARS.items() if year in years)
    # Query this location's real archive dates before planning image downloads.
    # collect_temporal_optical places out-of-period archive context after the
    # matching annual products, so an old archive cannot steal the target year.
    return ["history246", *products]


def infer_optical_periods(task):
    """Bind named annual products to grounded years without making capture dates."""
    if task.optical_periods:
        rows = [period.model_dump(mode="json", exclude_none=True) for period in task.optical_periods]
        for row in rows:
            if row["source"] in ANNUAL_SOURCE_YEARS:
                row.setdefault("year", ANNUAL_SOURCE_YEARS[row["source"]])
        return rows
    from .intent import named_research_sources
    named = task.selected_sources or named_research_sources(task.objective)
    if task.optical_source:
        named = [task.optical_source]
    intent = optical_time_intent(task)
    years = annual_retrieval_years(task) or intent["requested_years"]
    rows = []
    for source in named:
        if source == "siwei" and years == [2025]:
            source = "siwei2025"
        if source == "century":
            rows.extend({"source": source, "year": year} for year in years if century_year_supported(year))
        elif source in ANNUAL_SOURCE_YEARS:
            rows.append({"source": source, "year": ANNUAL_SOURCE_YEARS[source]})
        elif source == "history246" and intent["requested_capture_date"] and not history_catalog_only(task):
            rows.append({"source": source, "date": intent["requested_capture_date"]})
    return rows[:6]


def prioritize_optical_period(task):
    from .intent import SCIENCE_SOURCES, resolve_research_request
    request = resolve_research_request(task)
    science_only = request.mode != "auto" and not request.include_optical_context and bool(request.sources) and set(request.sources) <= SCIENCE_SOURCES
    return bool(not science_only and (optical_time_intent(task)["constrained"] or task.optical_periods))


def rank_history_dates(values, intent, limit=8):
    """Nearest archive dates with explicit signed/interval gaps, not global ends."""
    def parse(value):
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    start, end, reference = (parse(intent.get(key)) for key in ("requested_start", "requested_end", "reference_date"))
    rows = []
    for stamp in sorted({parsed for value in values if (parsed := parse(value))}):
        distance = (start - stamp).days if start and stamp < start else (stamp - end).days if end and stamp > end else 0
        signed = (stamp - reference).days if reference else None
        rows.append({"date": stamp.isoformat(), "in_requested_interval": distance == 0,
                     "distance_to_requested_interval_days": distance,
                     "distance_to_reference_days": abs(signed) if signed is not None else None,
                     "signed_days_from_reference": signed,
                     "reference_role": intent.get("reference_role", "general_history"),
                     "catalog_date_is_analyzed_pixel_evidence": False})
    rows.sort(key=lambda row: (row["distance_to_requested_interval_days"], row["distance_to_reference_days"] or 0, row["date"]))
    return rows[:limit]
