"""Task-specific report sections derived only from the supplied evidence."""
from __future__ import annotations

import re

from .schemas import TASK_LABELS, TaskKind
from .strategy import task_strategy, temporal_readiness
from .evidence import candidate_presence_evidence, candidate_presence_judgment

REPORT_HEADINGS = {
    TaskKind.DISCOVERY: '疑点位置与核查重点', TaskKind.CLASSIFY: '候选物料与排除解释',
    TaskKind.EXTENT: '可见范围与测量条件', TaskKind.CHANGE: '逐期状态与变化依据',
    TaskKind.CLEANUP: '清理变化与验收缺口', TaskKind.RESIDUAL: '残留位置与不可见区域',
    TaskKind.RECURRENCE: '三阶段证据与重复倾倒假设', TaskKind.TRACKING: '点位时间轴与跟踪缺口',
    TaskKind.ENVIRONMENT: '环境受体与核查路径', TaskKind.RELATIONSHIP: '场地周边关系与边界依据',
}


_PILE_PRESENT = re.compile(r'(?:可见|存在|出现|新增|仍有|呈现|发现)[^，,。；;]{0,8}(?:疑似)?(?:堆存|堆体|堆物|堆放物|固废|垃圾|渣土|废料)')
_PILE_ABSENT = re.compile(r'(?:未见|未发现|没有|无明显|不存在)[^，,。；;]{0,8}(?:疑似)?(?:堆存|堆体|堆物|堆放物|固废|垃圾|渣土|废料)'
                          r'|(?:堆存|堆体|堆物|固废|垃圾|渣土|废料)[^，,。；;]{0,6}(?:已清|清空|已平整|消失)')


def _science_state(statement, evidence_type=None):
    """Map a dated science observation to a coarse state, conservatively.

    Quality notes never count, and only an explicit clause about a pile's
    presence or absence does; anything else is surface context only."""
    if evidence_type == 'quality':
        return 'surface_context_only'
    text = str(statement or '')
    for clause in re.split(r'[，,。；;]', text):
        if re.search(r'无法|不能|需排除|待核|不确定', clause):
            continue
        if _PILE_ABSENT.search(clause):
            return 'not_visible'
        if _PILE_PRESENT.search(clause):
            return 'clue_visible'
    return 'surface_context_only'


def period_states(timeline, scientific_evidence=None):
    """One row per acquisition date across Jianwei optical readings and
    scientific-analyst observations. Science rows are supporting context:
    they only count as visible/not visible when the analyst explicitly names
    a pile or waste together with its presence or absence."""
    rows = []
    for row in timeline:
        if not row.get('actual_capture_date'):
            continue
        state = ('clue_visible' if row.get('has_pending_candidates') else
                 'not_visible' if row.get('qualified_scene_negative') else 'unreadable')
        rows.append({'date': row['actual_capture_date'], 'source': row.get('source'), 'figure': row.get('figure'),
                     'evidence_kind': 'optical_jianwei', 'state': state,
                     'summary': '；'.join(o for o in row.get('observations', []) if o)[:200]})
    for item in scientific_evidence or []:
        analysis = item.get('analysis') if isinstance(item.get('analysis'), dict) else {}
        if analysis.get('status') != 'completed':
            continue
        dates = {sub.get('preview_asset_id'): str(sub.get('scene_acquired_at') or '')[:10]
                 for sub in item.get('subsets') or [] if sub.get('preview_asset_id')}
        for obs in analysis.get('observations') or []:
            stamp = dates.get(obs.get('asset_id')) or str(obs.get('statement', ''))[:10]
            if len(stamp) != 10 or stamp[4] != '-':
                continue
            rows.append({'date': stamp, 'source': item.get('source'), 'figure': None,
                         'evidence_kind': 'science_analysis', 'state': _science_state(obs.get('statement'), obs.get('evidence_type')),
                         'summary': str(obs.get('statement', ''))[:200]})
    rows.sort(key=lambda row: (row['date'], row['evidence_kind']))
    visible = [row['date'] for row in rows if row['state'] == 'clue_visible']
    first = visible[0] if visible else None
    before = [row['date'] for row in rows if row['state'] == 'not_visible' and (not first or row['date'] < first)]
    return {'periods': rows, 'first_visible': first, 'last_visible': visible[-1] if visible else None,
            'last_absent_before_first': before[-1] if before else None,
            'dated_period_count': len({row['date'] for row in rows}),
            'readable_period_count': len({row['date'] for row in rows if row['state'] in {'clue_visible', 'not_visible'}}),
            'context_period_count': len({row['date'] for row in rows if row['state'] == 'surface_context_only'}),
            'basis': '高分光学逐期由见微识别；科学影像（多光谱/雷达）为辅助观测，只有分析员明确描述堆体有无时才计入'}


def _timeline_conclusion(states, registered):
    first, last, before = states['first_visible'], states['last_visible'], states['last_absent_before_first']
    tail = '' if registered else '；各期位置对应以目视为准，不计算面积'
    if first:
        text = f'{first}起可见疑似堆存线索'
        if before:
            text += f'（{before}未见），出现时间约在{before}至{first}之间'
        if last and last != first:
            text += f'，最近一期{last}仍可见'
        return text + tail
    if states['readable_period_count']:
        return f'已逐期判读{states["readable_period_count"]}期实际时相影像，均未见明确疑似堆存线索，不能据此排除未取得的时段' + tail
    if states['context_period_count']:
        return (f'已取得{states["context_period_count"]}期科学影像的逐期地表观测，但未直接描述堆体有无；'
                '需补充同期高分影像由见微逐期判读，才能确定堆存起止时间')
    return None


def task_report_title(task_types, *, batch=False):
    labels = [TASK_LABELS[TaskKind(value)] for value in task_types]
    prefix = '无隅见微多点' if batch else '无隅见微'
    return prefix + ('与'.join(labels[:2]) if labels else '遥感核查') + '报告'


def build_task_results(task, assets, recognition, assessments, fusion, *, temporal_collection=None, environmental_risk=None,
                       scientific_evidence=None):
    """No new visual claims, measured areas, cleanup rates or source attribution."""
    recognition = recognition.model_dump(mode='json') if hasattr(recognition, 'model_dump') else recognition
    by_id = {a['id']:a for a in assets}
    figures = {a['id']:f'图{i}' for i,a in enumerate(assets,1)}
    decisions = {row.get('asset_id'):row for row in assessments}
    from .fusion import requested_period_evidence
    period = requested_period_evidence(task, assets)
    presence = candidate_presence_evidence(assets, assessments, recognition, period, task=task)
    effective_by_id = {row['asset_id']: row for row in presence['assets']}
    period_by_id = {row['asset_id']: row for row in period['assets']}
    timeline = []
    for asset in assets:
        if asset.get('derived_from_asset_id'): continue
        meta = asset.get('metadata') or {}; api = asset.get('data_api_metadata') or {}
        row = decisions.get(asset['id'], {})
        effective = effective_by_id.get(asset['id'], {})
        stamp = meta.get('acquisition_time')
        period_row = period_by_id.get(asset['id'], {})
        actual = str(stamp)[:10] if stamp else None
        if (period['required'] or getattr(task, 'workflow_mode', 'auto') == 'temporal_change') and not period_row.get('actual_dates_verified'):
            actual = None
        timeline.append({'asset_id':asset['id'], 'figure':figures[asset['id']], 'source':meta.get('source'),
            'actual_capture_date':actual,
            'actual_capture_date_verified':period_row.get('actual_dates_verified') is True,
            'reported_capture_date':str(stamp)[:10] if stamp else None,
            'requested_period_eligible':period_row.get('eligible') if period['required'] else None,
            'reported_actual_dates':api.get('actual_dates') or ([api['actual_date']] if api.get('actual_date') else []),
            'date_scope':api.get('actual_date_scope') or api.get('metadata_scope') or meta.get('metadata_scope'),
            'product_year':api.get('product_year'), 'requested_date':api.get('requested_date'),
            'period_label':meta.get('period_label'), 'registration_verified':meta.get('registration_verified') is True,
            'decision':row.get('decision','unknown'),
            'effective_decision':effective.get('effective_decision', 'unknown'),
            'has_pending_candidates':bool(effective.get('pending_candidate_count')),
            'qualified_scene_negative':effective.get('qualified_scene_negative') is True,
            'observations':[o.get('statement','') for o in recognition.get('observations',[]) if o.get('asset_id')==asset['id']],
            'edition_is_capture_date':False})
    timeline.sort(key=lambda row:(row['actual_capture_date'] is None,row['actual_capture_date'] or '',row['figure']))
    temporal = temporal_readiness(task, assets, task_strategy(task))
    states = period_states(timeline, scientific_evidence)
    outputs = []
    candidates = recognition.get('candidates') or []
    available_observations = [figures.get(o.get('asset_id'),'关联影像')+'：'+o.get('statement','')
                              for o in recognition.get('observations',[]) if o.get('asset_id') in by_id]
    labels = list(dict.fromkeys(c.get('label','') for c in candidates if c.get('label'))) if presence['has_pending_candidates'] else []
    positive = presence['has_pending_candidates']
    for kind in task.task_types:
        result = {'task_type':kind.value, 'label':TASK_LABELS[kind], 'heading':REPORT_HEADINGS[kind],
            'status':'evidence_available' if available_observations else 'needs_more_data',
            'candidate_status':presence['status'], 'candidate_presence':presence,
            'requested_period_status':presence['requested_period_status'],
            'evidence':available_observations[:8], 'quantitative_result':None, 'next_steps':[]}
        if kind == TaskKind.DISCOVERY:
            result.update(conclusion=candidate_presence_judgment(presence) or fusion.get('judgment') or '尚无可复核疑点结论',
                next_steps=['核对候选位置、场地用途和正常施工或原料堆场等解释'] if positive else ['在目标可见的高分影像中复核覆盖与细节'])
        elif kind == TaskKind.CLASSIFY:
            result.update(conclusion=('保留候选类别：'+'、'.join(labels)+'；材料成分和废物属性仍需核验') if labels else '未形成具有可见证据的固废候选类别',
                alternatives=list(dict.fromkeys(value for c in candidates for value in c.get('alternative_explanations',[]))),
                next_steps=['核对物料来源、使用目的与现场成分，区分废物、商品原料和土方'])
        elif kind == TaskKind.EXTENT:
            result.update(status='qualitative_only' if available_observations else 'needs_more_data',
                conclusion='可引用图内候选范围；地面面积尚无经核验的测量结果' if available_observations else '尚无可复核的候选边界，当前不报告地面面积',
                next_steps=['复核堆体边缘并提供可靠地理变换和边界测量；原表面积仅保留为资料记录'])
        elif kind in {TaskKind.CHANGE,TaskKind.CLEANUP,TaskKind.RECURRENCE,TaskKind.TRACKING}:
            readable = bool(available_observations or states['readable_period_count'])
            result.update(status='qualitative_comparison_ready' if temporal['ready'] and readable else 'needs_more_data',
                timeline=timeline, temporal_evidence=temporal, period_states=states)
            if not temporal['ready']:
                noun={TaskKind.CHANGE:'变化方向',TaskKind.CLEANUP:'清理进度',TaskKind.RECURRENCE:'清后重复倾倒',TaskKind.TRACKING:'连续状态'}[kind]
                result['conclusion']='现有证据尚不能确认'+noun+'；需补齐至少'+str(temporal['required_distinct_dates'])+'期带实际拍摄日期的影像'
            elif kind in {TaskKind.CHANGE,TaskKind.TRACKING} and _timeline_conclusion(states, temporal.get('registration_verified') is True):
                result['conclusion']=_timeline_conclusion(states, temporal.get('registration_verified') is True)
                if not readable:
                    result['status']='needs_more_data'
            elif not readable:
                result['conclusion']='已取得多期实际时相影像，但尚无有效的逐期识别结果，暂不能比较可见状态'
            elif kind == TaskKind.CHANGE:
                result['conclusion']=('已具备有序实际时相，可逐期比较可见状态' +
                    ('；位置对应以目视为准' if not temporal.get('registration_verified') else '') + '；不计算变化面积或变化归因')
            elif kind == TaskKind.CLEANUP:
                result['conclusion']='可比较整改前后可见状态；影像外观变化不能单独证明清运完成或验收达标'
            elif kind == TaskKind.RECURRENCE:
                result['conclusion']='已具备三期比较条件；仍需核实中间阶段确已清空，才能支持再次堆放假设'
            else:
                result['conclusion']='逐期记录已取得的点位状态；未读取或质量不足的阶段保持缺口'
            result['next_steps'] = {
                TaskKind.CHANGE:['补齐目标范围内带实际日期且可靠配准的前后影像，排除物候、阴影与施工扰动'],
                TaskKind.CLEANUP:['补充整改时间、原堆边界与清理后影像，区分清运、覆土、遮盖和平整'],
                TaskKind.RECURRENCE:['核对原堆存、已清空、再次出现三个独立阶段，缺清空基线时不确认复发'],
                TaskKind.TRACKING:['按实际日期补缺失阶段；本次历史回溯不自动创建未来定时监控'],
            }[kind]
        elif kind == TaskKind.RESIDUAL:
            result.update(conclusion='按整改范围保留可见残留候选与边角遮挡；小目标未检出不能作为全部清零的依据' if available_observations else '缺少可判读的整改后近景，尚不能确认残留或全部清零',
                next_steps=['提供原堆边界和整改后近景，重点核对树下、围挡、出入口和零散堆体'])
        elif kind == TaskKind.ENVIRONMENT:
            result.update(conclusion='依据可见受体建立待核查的源与迁移路径；尚不能据影像确认污染状态或法定风险等级' if available_observations else '尚无足够影像证据描述目标周边受体与迁移路径，环境影响保持待核查',
                next_steps=['核实排水、坡向、水体和居民点关系，再确定径流、扬尘等现场核查重点'])
            if presence['priority_factors']:
                result.update(conclusion='已记录未排除候选与水体的可见邻接，宜优先核查物料、排水与岸边防护；尚未测量距离，也不能据此确认污染',
                    priority_factors=presence['priority_factors'])
                result['next_steps'].insert(0, '优先现场核对临水候选、岸边排水与防护，确认是否存在实际迁移路径')
        else:
            result.update(conclusion='保留道路、厂界、水体和农田的可见关系；地籍归属、保护边界和责任主体尚需资料核对' if available_observations else '尚无可复核的场地周边关系，需补充同范围场景与相应边界资料',
                next_steps=['叠加经核验的地块、河道或耕地保护边界；距离量化须有可靠地理参考'])
        if period['required']:
            result['requested_period_evidence'] = period
            if not period['ready'] or presence['conditional_pending_asset_ids']:
                result['status'] = 'needs_more_data'
                result['conditional_image_observations'] = available_observations[:8]
                if kind == TaskKind.DISCOVERY:
                    result['conclusion'] = ((candidate_presence_judgment(presence) or '存在待核查风险点') + '；尚不能确认其属于该年份，日期缺口不删除候选'
                        if positive else '现有影像的实际拍摄日期尚未满足请求时段，不能据其未检出排除该年份的疑点')
                else:
                    result['conclusion'] = '当前仅保留图内观察，尚不能归入请求时段。' + result['conclusion']
                result['next_steps'] = ['补充请求时段内实际拍摄日期已核实、位置与质量合格的影像；版次和发现日期不能代替拍摄日期', *result['next_steps']]
        outputs.append(result)
    result = {'schema_version':'task-results-v1','results':outputs,'timeline':timeline,
        'candidate_presence':presence,
        'temporal_collection':temporal_collection or {},'requested_period_evidence':period,
        'period_states':states,
        'recorded_areas_are_measurements':False}
    from .task_outputs import mapping, presentation_contract
    if environmental_risk is not None:
        result['environmental_risk'] = mapping(environmental_risk)
    presentation = presentation_contract({'task': task, 'task_results': result, 'environmental_risk': environmental_risk})
    if presentation['enabled']:
        result['presentation'] = presentation
        result['workflow_mode'] = presentation['workflow_mode']
        if presentation['workflow_mode'] == 'environmental_risk':
            result['results'] = [{'task_type': 'environmental_context', 'label': '环境风险研判',
                'heading': presentation['heading'], 'conclusion': presentation['lead'],
                'status': 'evidence_available' if presentation.get('risk_label') != '证据不足' else 'needs_more_data',
                'next_steps': mapping(environmental_risk).get('next_steps', [])}]
    return result
