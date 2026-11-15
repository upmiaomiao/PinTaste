"""Small, question-specific evidence plans; capability catalogues stay internal."""
from __future__ import annotations
import re
from .schemas import TaskRequest


def primary_science_sources(task: TaskRequest) -> list[str]:
    text = task.objective
    if re.search(r'高光谱|材料光谱|吸收特征', text):
        return ['enmap']
    if re.search(r'SAR|雷达|云雨|阴雨|多云|全天候|后向散射', text, re.I):
        return ['sentinel1-rtc']
    if (task.date_start and task.date_start.year < 2015) or re.search(r'(?:19\d{2}|200\d|201[0-4])年', text):
        return ['landsat']
    return ['sentinel2']


def demand_questions(task: TaskRequest) -> list[str]:
    """Prerequisites asked before submission (no quota, no planner call)."""
    from .intent import resolve_research_request
    from .task_modes import TEMPORAL_KINDS
    request = resolve_research_request(task)
    questions = []
    temporal = request.mode == 'timeseries' or any(str(t) in TEMPORAL_KINDS for t in task.task_types)
    if temporal and not (task.date_start or task.date_end):
        from .optical_time import optical_time_intent
        if not optical_time_intent(task)['constrained'] and not re.search(r'过去|以前|历史|历年|从.{0,6}到', task.objective):
            questions.append('希望比较哪两个日期或哪个起止时段？')
    if task.coordinate_system == 'unknown' and (task.center or task.bbox) and not re.search(r'WGS\s*84|EPSG\s*:\s*4326',task.objective,re.I):
        questions.append('这些坐标来自 GPS/WGS84，还是高德、腾讯等地图？')
    return questions[:3]


def demand_brief(task: TaskRequest) -> dict:
    """Explain the next evidence step, without inventing observations."""
    from .intent import named_research_sources, resolve_research_request, SCIENCE_SOURCES
    request = resolve_research_request(task)
    named = named_research_sources(task.objective)
    sources = list(request.sources)
    science = [s for s in sources if s in SCIENCE_SOURCES]
    if request.mode != 'auto':
        initial = science or primary_science_sources(task)
        purpose = ('先核对指定区域和时段有哪些真实场景；目录查询不等于已分析影像' if request.mode == 'availability'
                   else '先取得能回答本轮问题的真实场景，再检查局部质量和原生波段')
    else:
        from .optical_time import optical_source_preferences, optical_time_intent
        time_intent = optical_time_intent(task)
        initial = optical_source_preferences(task) or ([task.optical_source] if task.optical_source else [s for s in named if s not in SCIENCE_SOURCES][:2] or ['tianditu', 'arcgis'])
        purpose = ('我先查你指定时段的年度产品和该点可用历史日期，优先取得接近目标时间的影像；'
                   '实际日期不符合或未知时保留缺口，当前底图只作有说明的背景参考'
                   if time_intent['constrained'] else
                   '先看一份覆盖目标的高分光学影像，核对位置、边界与场地上下文；存在明确缺口时才补其他来源')
    return {'initial_sources': initial, 'purpose': purpose, 'questions': [],
        'selection_policy': 'minimum_relevant_evidence_then_triggered_supplement',
        'fallback_policy': 'only_after_coverage_quality_permission_or_observation_gap',
        'resolution_rule': '小目标形态由高分光学核查；10米多光谱和SAR不能替代细碎边界证据'}


def concise_ranking(ranking: list[dict], task: TaskRequest, strategy: dict) -> list[dict]:
    """Auto screening starts optical-only; scientific requests have their own path."""
    from .intent import named_research_sources, SCIENCE_SOURCES
    names = [s for s in named_research_sources(task.objective) if s not in SCIENCE_SOURCES]
    required = strategy['optical_source_contract']['required_sources']
    chosen = [task.optical_source] if task.optical_source else names
    from .optical_time import optical_source_preferences, prioritize_optical_period
    period_first = prioritize_optical_period(task)
    if period_first:
        chosen = optical_source_preferences(task)
    optical = [r for r in ranking if r['group'] == 'optical']
    if not chosen:
        # Preserve the user-facing source sequence after unavailable/forbidden
        # providers have been removed by the real capability boundary.
        priority = strategy['initial_optical_priority']
        optical.sort(key=lambda row: priority.index(row['source']) if row['source'] in priority else len(priority))
    if not period_first and len(chosen) >= 2 and any(r['source'] == 'arcgis' for r in optical):
        # A permitted georeferenced anchor is a prerequisite for relative
        # comparison, and never substitutes for a named provider in the pair.
        chosen = ['arcgis', *[s for s in chosen if s != 'arcgis']]
    if chosen:
        optical.sort(key=lambda r: chosen.index(r['source']) if r['source'] in chosen else len(chosen))
    limit = max(2, len(required), len(chosen))
    result = optical[:limit]
    for index, row in enumerate(result):
        row = dict(row)
        row['status'] = 'planned' if index == 0 or row['source'] in required or row['source'] in chosen else 'standby'
        row['reason'] = ('本轮问题的首选高分来源；先检查位置与目标可见性' if index == 0 else
            '用户明确要求的来源对照' if row['source'] in required or row['source'] in chosen else
            '仅当前一来源覆盖、质量、权限或空间核验不足时回退，不预先下载')
        if period_first and row['source'] in chosen:
            row['reason'] = '优先检索用户指定时段的产品或历史日期；版次不是拍摄时间，须核对实际日期和位置'
        result[index] = row
    return result
