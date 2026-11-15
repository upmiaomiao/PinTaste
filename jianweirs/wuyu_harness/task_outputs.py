"""Task-shaped presentation derived from saved evidence, with no tool calls."""
from collections.abc import Mapping
from datetime import date
import re
from .answer_layout import concise_lead, normalize_answer, public_review_text


TITLES = {
    'site_investigation': ('点位核查与多源证据报告', '点位核查结论'),
    'complaint_investigation': ('举报线索核实报告', '举报核实结论'),
    'environmental_risk': ('固废堆存环境风险研判报告', '环境风险结论'),
    'temporal_change': ('固废堆存时序变化报告', '时序变化结论'),
    'contextual_qa': ('个性化问题答复', '答复'),
}
LEVELS = {'high': '高', 'medium': '中', 'low': '低', 'insufficient': '证据不足', 'insufficient_evidence': '证据不足'}
COMPLIANCE = {'potential_noncompliance': '可能不符合，需补证核实', 'verified_noncompliance': '已有原始依据支持不符合',
              'not_assessable': '目前无法判定', 'no_visible_issue': '可见范围未见对应异常', 'missing_evidence': '缺少判定依据'}
SCOPES = {'national': '国家', 'provincial': '省级', 'local': '地方'}
PATHWAYS = {'surface_runoff': '地表径流', 'infiltration': '下渗迁移', 'dust': '扬尘',
            'smoke': '烟气扩散', 'slope_instability': '边坡失稳', 'fire': '火灾', 'contact': '接触暴露', 'unknown': '路径待核实'}


def mapping(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else dict(value) if isinstance(value, Mapping) else {}


def rows(value):
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def text(value, limit=1800):
    return public_review_text(str(value).strip())[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def texts(value):
    return [text(item) for item in value if text(item)] if isinstance(value, list) else [text(value)] if text(value) else []


def resolve_output_mode(task):
    # Rendering never re-classifies: an upgraded lexicon must not change the
    # title or structure of an already produced report.
    from .task_modes import stored_workflow
    return stored_workflow(task)


def output_enabled(package):
    task = mapping(package.get('task'))
    return task.get('workflow_mode') in TITLES or bool(package.get('environmental_risk') or package.get('contextual_answer'))


def _section(identity, heading, paragraphs=(), columns=(), records=()):
    return {'id': identity, 'heading': heading, 'paragraphs': [text(v, 32000) for v in paragraphs if text(v, 32000)],
            'columns': list(columns), 'records': [list(record) for record in records]}


def place_section(place):
    """One shared table for the page, HTML report and Word export."""
    place = mapping(place)
    if place.get('status') not in ('completed', 'partial'):
        return None
    admin = mapping(place.get('administrative'))
    region = ' '.join(text(admin.get(k)) for k in ('province', 'city', 'district', 'township') if text(admin.get(k)))
    lines = [f'所在区域：{region}' + (f'（{text(admin.get("formatted_address"))}）' if admin.get('formatted_address') else '')] if region else []
    zones = [text(z) for z in place.get('zone_characteristics') or [] if text(z)]
    if zones:
        lines.append('周边特征：' + '、'.join(zones) + '（依据周边名称归纳，不是法定用地或保护区认定）')
    records = []
    for category in rows(place.get('categories')):
        for item in rows(category.get('items'))[:6]:
            distance = item.get('distance_m')
            records.append([text(category.get('label')), text(item.get('name')),
                            text(item.get('type')).split(';')[-1] or '—',
                            f'约{distance}米' + (f'，{text(item.get("direction"))}' if item.get('direction') else '') if distance is not None else '未知'])
    lines.append(text(place.get('notice'), 600))
    return _section('place-context', f'所在区域与周边设施（{place.get("radius_m", 2000)}米范围）', lines,
                    ['类别', '名称', '类型', '直线距离与方位'] if records else (), records)


def temporal_facts(timeline):
    """Only verified dates at a supported same point may bracket an onset."""
    def valid_date(value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)):
            return False
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    dated = [dict(row) for row in rows(timeline) if valid_date(row.get('actual_capture_date'))
             and row.get('actual_capture_date_verified') is not False
             and row.get('registration_verified') is True and row.get('requested_period_eligible') is not False]
    dated.sort(key=lambda row: row['actual_capture_date'])
    positives = [row for row in dated if row.get('effective_decision', row.get('decision')) == 'positive'
                 or row.get('has_pending_candidates') is True]
    negatives = [row for row in dated if row.get('effective_decision', row.get('decision')) == 'negative'
                 and row.get('qualified_scene_negative') is True]
    first, last = (positives[0] if positives else None), (positives[-1] if positives else None)
    prior = next((row for row in reversed(negatives) if first and row['actual_capture_date'] < first['actual_capture_date']), None)
    return {'first_positive_date': first['actual_capture_date'] if first else None,
            'latest_positive_date': last['actual_capture_date'] if last else None,
            'prior_negative_date': prior['actual_capture_date'] if prior else None,
            'onset_interval': [prior['actual_capture_date'], first['actual_capture_date']] if prior and first else None,
            'continuous_between_observations_verified': False,
            'dated_same_point_observations': len(dated)}


def _risk_contract(package, task_results):
    risk = mapping(package.get('environmental_risk') or task_results.get('environmental_risk'))
    level = risk.get('final_level', risk.get('risk_level', 'insufficient'))
    final_state = mapping(risk.get('final_review'))
    if risk.get('status') != 'completed' or final_state.get('status') != 'completed':
        level = 'insufficient'
    label = LEVELS.get(level, '证据不足')
    conclusion = text(risk.get('conclusion'), 24000)
    lead = f'环境风险筛查等级：{label}。' + (conclusion if conclusion else '现有证据尚不足以完成环境风险分级，需先核实堆体、迁移路径与敏感受体。')
    lead, lead_detail = concise_lead(lead)
    sections = []
    figures = {asset.get('id'): '图' + str(i+1) for i, asset in enumerate(rows(package.get('assets')))}
    documents = {doc.get('id'): '用户资料' + str(i+1) for i, doc in enumerate(rows(package.get('documents')))}
    evidence_names = {**figures, **documents}
    def references(row):
        values = []
        for ref in rows(row.get('evidence_refs') or row.get('evidence')):
            label = evidence_names.get(ref.get('asset_id') or ref.get('document_id'), '关联依据待核实')
            values.append(label + ('：' + text(ref.get('statement'), 300) if ref.get('statement') else ''))
        for identity in texts(row.get('evidence_ids') or row.get('supporting_evidence_ids')):
            values.append(evidence_names.get(identity, '关联依据待核实'))
        return '；'.join(dict.fromkeys(values)) or '尚无可定位的影像引用'
    pathways = rows(risk.get('source_pathway_receptors') or risk.get('pathways'))
    sections.append(_section('pathways', '污染源 迁移路径与环境受体',
        () if pathways else ['尚未形成有证据引用的源—路径—受体链。'],
        ['潜在源', '迁移路径', '环境受体', '影像依据', '仍需核实'],
        [[text(row.get('source_hypothesis') or row.get('source')), PATHWAYS.get(row.get('pathway'), text(row.get('pathway'))), text(row.get('receptor')),
          references(row),
          '；'.join(texts(row.get('missing_verification') or row.get('uncertainty')))] for row in pathways]))
    attribution = rows(risk.get('attribution') or risk.get('causal_analysis'))
    sections.append(_section('attribution', '综合归因与竞争解释',
        [text(row.get('hypothesis') or row.get('description')) +
         ('；其他解释：' + '、'.join(texts(row.get('competing_explanations'))) if row.get('competing_explanations') else '') +
         ('；待核实：' + '、'.join(texts(row.get('missing_verification'))) if row.get('missing_verification') else '') for row in attribution]
        or ['物料来源、迁移过程和污染归因尚无充分原始依据。']))
    initial = mapping(risk.get('preliminary_review') or risk.get('initial_assessment'))
    final = mapping(risk.get('final_review'))
    initial_level = initial.get('preliminary_level') or initial.get('risk_level') or initial.get('final_level')
    sections.append(_section('review', '见微识别与独立复核', [
        '见微初步意见：' + (('风险参考等级 ' + LEVELS.get(initial_level, '证据不足') + '；') if initial_level else '') + (text(initial.get('scene_description') or initial.get('conclusion') or initial.get('summary') or initial.get('assessment'), 12000) or '未形成可用初判'),
        '独立复核意见：' + (text(final.get('conclusion') or final.get('summary') or final.get('assessment'), 12000) or '尚未形成已完成的全面复核'),
        *([lead_detail] if lead_detail else []),
    ]))
    checks = rows(risk.get('compliance_checks') or risk.get('compliance'))
    sections.append(_section('compliance', '国家及地方管理标准对照',
        () if checks else ['尚无足够的辖区、有效标准与事实证据完成逐条对照；不据此判定合规或违法。'],
        ['适用层级', '管理要求', '已掌握事实', '对照意见', '缺少的证据'],
        [[SCOPES.get(row.get('jurisdiction') or row.get('scope'), text(row.get('jurisdiction') or row.get('scope')) or '待核实'),
          text(row.get('requirement')) + ('（来源 ' + text(row.get('source_id')) + '）' if row.get('source_id') else ''), '；'.join(texts(row.get('observed_fact') or row.get('observed_facts'))),
          COMPLIANCE.get(row.get('status'), '目前无法判定'),
          '；'.join(texts(row.get('missing_evidence')))] for row in checks]))
    sections.append(_section('actions', '分级防控与现场核查重点', texts(risk.get('next_steps') or risk.get('action_plan'))
        or ['补充堆体性质、排水连通、敏感目标及防护措施的现场证据后复核等级。']))
    if risk.get('limitations'):
        sections.append(_section('limits', '本次风险判断的适用范围', texts(risk['limitations'])))
    sections.append(_section('grading-scope', '风险等级用途', ['该等级用于确定核查与防控优先级，不代替法定风险鉴定。']))
    return lead, sections, label, risk


def presentation_contract(package, *, base_lead=''):
    package = mapping(package); task = mapping(package.get('task')); result = mapping(package.get('task_results'))
    mode = resolve_output_mode(task)
    title, heading = TITLES.get(mode, TITLES['site_investigation'])
    lead = text(base_lead or mapping(package.get('consumer_conclusion')).get('text'), 5000)
    sections, risk_label = [], None
    result_rows = rows(result.get('results'))
    if mode == 'environmental_risk':
        lead, sections, risk_label, _ = _risk_contract(package, result)
    elif mode == 'contextual_qa':
        answer = mapping(package.get('contextual_answer'))
        adaptive = normalize_answer(text(answer.get('summary') or mapping(package.get('narrative')).get('summary'), 24000),
            [{'heading':text(row.get('heading')), 'content':'\n\n'.join(texts(row.get('content'))) if isinstance(row.get('content'),list) else text(row.get('content'),24000)} for row in rows(answer.get('sections'))])
        lead = adaptive['summary']
        sections = [_section('answer-' + str(i), text(row.get('heading')) or '补充说明', [text(row.get('content'), 24000)])
                    for i, row in enumerate(adaptive['sections'])]
        place = place_section(answer.get('place_context'))
        if place:
            sections.append(place)
    elif mode == 'temporal_change':
        timeline = rows(result.get('timeline'))
        facts = temporal_facts(timeline)
        first, last, prior = (facts[key] for key in ('first_positive_date', 'latest_positive_date', 'prior_negative_date'))
        if first:
            lead = f'已核对的同点影像中，最早于 {first} 可见堆存线索，最近于 {last} 仍可见。'
            lead += (f'此前 {prior} 的可比影像未见对应堆存，出现时间可夹定在两次观测之间；尚不能确定具体开始日期。'
                     if prior else '尚无更早且有效的未堆存基线，不能把最早检出日直接当作开始堆放日。')
            lead += '各次观测之间的连续状态仍需更多时相支持。'
        else:
            lead = '现有记录尚不能确定该点位何时开始堆存或最后何时仍存在；需补齐有实际拍摄日期、同点对应合格的逐期判读。'
        sections.append(_section('time-anchors', '堆存起始区间与最后可见时点', (), ['时间锚点', '已核对结果'], [
            ['前一期有效未见基线', prior or '尚未取得'], ['最早可见堆存线索', first or '尚未确定'],
            ['最近仍可见堆存线索', last or '尚未确定'],
            ['可能出现区间', f'{prior} 之后至 {first} 之间' if prior and first else '缺少有效基线，尚不能夹定'],
        ]))
        sections.append(_section('timeline', '过去至最近的逐期证据', (), ['影像', '实际拍摄日期', '逐期可见状态', '同点对应'],
            [[text(row.get('figure') or row.get('asset_id')), text(row.get('actual_capture_date')) or '未核实',
              '保留堆存线索' if row.get('has_pending_candidates') is True else
              '有效未见基线' if row.get('effective_decision', row.get('decision')) == 'negative' and row.get('qualified_scene_negative') is True else
              '未形成合格未见基线' if row.get('effective_decision', row.get('decision')) == 'negative' else
              {'positive': '保留堆存线索', 'unknown': '暂不能判断'}.get(row.get('effective_decision', row.get('decision')), '暂不能判断'),
              '已核对' if row.get('registration_verified') is True else '待核对'] for row in timeline]))
        sections.append(_section('time-limits', '未覆盖时段与变化归因限制', [
            '无有效影像的日期不推定为无堆存；年度版次不代替实际拍摄日期。',
            '清理、覆土、物候、阴影及空间偏移需分别排除；未实测的面积和体积不报告为变化量。']))
    elif mode == 'complaint_investigation':
        spec = mapping(task.get('complaint_scan'))
        if lead and not lead.startswith('举报核查'):
            lead = '举报核查结果：' + lead
        sections.append(_section('claim', '举报主张与本次核实目标', [text(spec.get('text') or task.get('objective'), 1800)]))
        sections.append(_section('findings', '核查发现与举报线索对照',
            [text(row.get('conclusion')) for row in result_rows] or ['本次尚无足够影像证据核实举报中的具体堆存主张。']))
        scope = '本任务分析的是已确认区域内的窗口 ' + text(spec['window_id']) + '；其余区域以覆盖账本为准。' if spec.get('window_id') else '核查范围仅限已确认区域及实际可判读的影像；未覆盖部分保持待核实。'
        sections.append(_section('coverage', '核查范围 覆盖与盲区', [scope, '影像未检出不能单独证明举报不实，室内、遮挡和短时行为需其他原始资料核查。']))
    else:
        sections = [_section('site-' + str(i), text(row.get('heading') or row.get('label')) or '点位证据',
                             [text(row.get('conclusion')), *texts(row.get('evidence'))[:4]]) for i, row in enumerate(result_rows)]
    answer_meta = mapping(package.get('contextual_answer')) if mode == 'contextual_qa' else {}
    return {'schema': 'task-presentation-v13', 'enabled': output_enabled(package), 'workflow_mode': mode,
            'report_title': '无隅见微' + title, 'heading': heading, 'lead': lead, 'sections': sections,
            'risk_label': risk_label, 'temporal_facts': temporal_facts(result.get('timeline')) if mode == 'temporal_change' else None,
            'answer_model': answer_meta.get('answer_model'),
            'reasoning': text(answer_meta.get('reasoning'), 12000) if answer_meta.get('answer_model') == 'shensi' else ''}
