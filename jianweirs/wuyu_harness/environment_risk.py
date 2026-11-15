"""Scene-specific environmental screening, with Jianwei then Gemini review.

Risk levels are qualitative product screening judgments, not calibrated
probabilities, pollution measurements or statutory/legal determinations.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .evidence import reject_unsupported_claims
from .models import ProviderError
from .schemas import Record, Recognition


RiskLevel = Literal['high', 'medium', 'low', 'insufficient']
RISK_LABELS = {'high': '高', 'medium': '中', 'low': '低', 'insufficient': '证据不足'}
BOUNDARY = '本等级为当前证据支持的环境风险筛查判断，不是法定风险等级；影像本身不证明已经污染、超标或违法。'


class RiskEvidence(Record):
    asset_id: str | None = Field(default=None, max_length=80)
    document_id: str | None = Field(default=None, max_length=80)
    statement: str = Field(min_length=1, max_length=650)
    pixel_window: tuple[int, int, int, int] | None = None

    @model_validator(mode='after')
    def exactly_one_source(self):
        if bool(self.asset_id) == bool(self.document_id):
            raise ValueError('风险事实需引用一幅登记影像或一份用户资料')
        if self.document_id and self.pixel_window:
            raise ValueError('资料文字不能携带影像像素框')
        return self


class RiskPathway(Record):
    id: str = Field(min_length=1, max_length=40)
    source_hypothesis: str = Field(min_length=1, max_length=400)
    pathway: Literal['surface_runoff', 'infiltration', 'dust', 'smoke', 'slope_instability', 'fire', 'contact', 'unknown']
    receptor: str = Field(min_length=1, max_length=300)
    connectivity: Literal['visually_suggested', 'documented', 'unknown'] = 'unknown'
    priority: RiskLevel = 'insufficient'
    evidence_refs: list[RiskEvidence] = Field(default_factory=list, max_length=12)
    missing_verification: list[str] = Field(default_factory=list, max_length=8)


class RiskAttribution(Record):
    hypothesis: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    competing_explanations: list[str] = Field(min_length=1, max_length=6)
    missing_verification: list[str] = Field(default_factory=list, max_length=8)


class ComplianceCheck(Record):
    jurisdiction: Literal['national', 'provincial', 'local']
    source_id: str | None = Field(default=None, max_length=60)
    requirement: str = Field(min_length=1, max_length=650)
    observed_fact: str = Field(min_length=1, max_length=650)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    status: Literal['potential_noncompliance', 'verified_noncompliance', 'not_assessable', 'no_visible_issue', 'missing_evidence']
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)


class PreliminaryRiskAssessment(Record):
    scene_description: str = Field(min_length=1, max_length=1800)
    preliminary_level: RiskLevel
    observations: list[RiskEvidence] = Field(default_factory=list, max_length=30)
    risk_factors: list[RiskPathway] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class EnvironmentalRiskReview(Record):
    final_level: RiskLevel
    conclusion: str = Field(min_length=1, max_length=1800)
    scene_quality_sufficient: bool
    source_review_complete: bool
    receptor_review_complete: bool
    pathway_review_complete: bool
    source_pathway_receptors: list[RiskPathway] = Field(default_factory=list, max_length=16)
    attribution: list[RiskAttribution] = Field(default_factory=list, max_length=10)
    compliance_checks: list[ComplianceCheck] = Field(default_factory=list, max_length=16)
    review_rationale: str = Field(min_length=1, max_length=1500)
    disagreements: list[str] = Field(default_factory=list, max_length=10)
    next_steps: list[str] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)


def _public_config(config, *, timeout=60, tokens=5000):
    return replace(config, timeout=min(config.timeout, timeout), max_tokens=min(config.max_tokens, tokens), enable_thinking=False)


def _safe_failure(error):
    # Provider exception text may contain internal endpoints or response bodies.
    return getattr(error, 'failure_kind', 'invalid_response')


def _check_claims(text):
    reject_unsupported_claims(text)
    assertions = re.finditer(r'(?:已经|确已|已证实|正在)(?:造成|导致)?污染', text)
    pollution_asserted = any(not re.search(r'不能|未能|无法|尚未|不证明|不代表|不说明|不等于|是否', re.split(r'[，,。；;\n]', text[:m.start()])[-1][-24:]) for m in assertions)
    distances = [m for m in re.finditer(r'(?:距离|距|相距)[^。；;\n]{0,12}\d+(?:\.\d+)?\s*(?:米|公里|m\b|km\b)', text, re.I)
                 if not re.search(r'用户(?:提供|资料|上传|填写)|资料(?:记载|显示|中)|据(?:用户|资料|举报)|无法|未经|未测量|不能',
                                  re.split(r'[。；;\n]', text[:m.start()])[-1] + m.group(0))]
    if pollution_asserted or distances:
        raise ValueError('风险分析不能新增污染事实或未经测量的受体距离')


def _validate_refs(refs, assets, documents):
    by_asset = {a['id']: a for a in assets}
    by_document = {d['id']: d for d in documents}
    for ref in refs:
        value = ref.model_dump(mode='json') if hasattr(ref, 'model_dump') else ref
        _check_claims(value.get('statement', ''))
        if value.get('asset_id'):
            image = by_asset.get(value['asset_id'])
            if image is None:
                raise ValueError('风险分析引用了未提供的影像')
            box = value.get('pixel_window')
            if box:
                x1, y1, x2, y2 = box
                if (any(type(v) is not int for v in box) or not 0 <= x1 < x2 <= image['width']
                        or not 0 <= y1 < y2 <= image['height']):
                    raise ValueError('风险分析引用了越界的影像像素框')
        elif value.get('document_id') not in by_document:
            raise ValueError('风险分析引用了未提供的资料')


def _authoritative(source):
    try:
        host = (urlsplit(source.get('url', '')).hostname or '').lower()
    except ValueError:
        return False
    return bool(host and (host.endswith('.gov.cn') or host.endswith('.gov') or host == 'legislation.gov.uk'
                         or host.endswith('.europa.eu') or host == 'eur-lex.europa.eu'))


def _verified_record(document):
    # No ordinary upload field can grant this status. It must have been added
    # by an authorized evidence-validation workflow, outside this model call.
    audit = document.get('verification') or {}
    return (document.get('provenance_kind') == 'authorized_verified_record'
            and audit.get('status') == 'verified' and audit.get('authorized_verification') is True
            and bool(audit.get('authority')) and bool(audit.get('record_id')))


def _compliance_rows(review, policy, documents, evidence_ids, warnings):
    sources = {row['id']: row for row in policy.get('sources', []) if isinstance(row, dict) and row.get('id')}
    records = {d['id']: d for d in documents}
    rows = []
    for check in review.compliance_checks:
        row = check.model_dump(mode='json')
        if not set(row['evidence_ids']) <= evidence_ids:
            raise ValueError('标准对照引用了未提供的事实证据')
        source = sources.get(row['source_id'])
        if policy.get('status') == 'disabled_by_user' or not source or not _authoritative(source):
            row.update(source_id=None, requirement='本轮尚未取得可用于该项对照的官方标准或政策原文',
                       status='not_assessable' if policy.get('status') == 'disabled_by_user' else 'missing_evidence')
            row['missing_evidence'] = list(dict.fromkeys([*row['missing_evidence'], '补充官方依据及其施行版本、适用辖区和适用条件']))
            if check.source_id and not source:
                warnings.append('标准对照中的未知来源编号已剔除，未作为违规依据')
        else:
            legal_verified = source.get('legal_status') == 'verified' and source.get('applicability_verified') is True
            fact_verified = any(_verified_record(records[i]) for i in row['evidence_ids'] if i in records)
            row['legal_applicability_verified'] = legal_verified
            row['verified_fact_record_available'] = fact_verified
            if row['status'] == 'verified_noncompliance' and not (legal_verified and fact_verified):
                row['status'] = 'potential_noncompliance' if row['evidence_ids'] else 'missing_evidence'
                row['missing_evidence'] = list(dict.fromkeys([*row['missing_evidence'], '现场事实或检测、许可台账及法规现行适用性尚未完成授权核验']))
            if not legal_verified:
                row['missing_evidence'] = list(dict.fromkeys([*row['missing_evidence'], '官方来源已检索，但现行版本和本点位适用性仍需核验']))
            if row['status'] == 'no_visible_issue':
                row['missing_evidence'] = list(dict.fromkeys([*row['missing_evidence'], '仅表示该项可见条件未见问题，不代表许可、检测或全部管理要求合规']))
        _check_claims(row['observed_fact'])
        rows.append(row)
    existing = {row['jurisdiction'] for row in rows}
    for scope, label in [('national', '国家'), ('provincial', '省级'), ('local', '市县')]:
        if scope not in existing:
            rows.append({'jurisdiction': scope, 'source_id': None, 'requirement': f'{label}层面的适用标准与管理要求尚未形成可核验对照',
                         'observed_fact': '不以影像外观代替条款适用条件、许可和现场事实', 'evidence_ids': [],
                         'status': 'not_assessable' if policy.get('status') == 'disabled_by_user' else 'missing_evidence',
                         'missing_evidence': ['本轮用户关闭联网检索，未擅自检索法规'] if policy.get('status') == 'disabled_by_user' else ['补充辖区及官方政策、标准的现行原文与事实证据']})
    return rows


def validate_final_review(review, *, assets, documents, policy, preliminary):
    """Evidence gates can qualify a proposed grade; no model may certify itself."""
    warnings = []
    evidence_ids = {a['id'] for a in assets} | {d['id'] for d in documents}
    refs = [ref for row in review.source_pathway_receptors for ref in row.evidence_refs]
    _validate_refs(refs, assets, documents)
    for row in review.attribution:
        if not set(row.evidence_ids) <= evidence_ids:
            raise ValueError('归因引用了未提供的证据')
        _check_claims(row.hypothesis)
        for explanation in row.competing_explanations:
            _check_claims(explanation)
    for row in review.source_pathway_receptors:
        _check_claims(row.source_hypothesis)
        _check_claims(row.receptor)
    _check_claims(review.conclusion)
    _check_claims(review.review_rationale)
    rows = _compliance_rows(review, policy, documents, evidence_ids, warnings)
    level = review.final_level
    limitations = list(review.limitations)
    complete_preliminary = preliminary.get('status') == 'completed'
    has_refs = bool(refs)
    pathways = review.source_pathway_receptors
    if not complete_preliminary:
        limitations.append('见微环境初评未完整形成，最终双阶段风险审查尚未闭合')
    if not assets or not review.scene_quality_sufficient or not complete_preliminary:
        level = 'insufficient'
    elif level in {'high', 'medium'} and not has_refs:
        limitations.append('风险等级缺少可复核的源—路径—受体证据引用')
        level = 'insufficient'
    elif level == 'high' and not any(row.priority == 'high' and row.connectivity != 'unknown'
                                     and len({ref.statement for ref in row.evidence_refs}) >= 2 for row in pathways):
        limitations.append('高等级建议尚缺清晰的受体与迁移连通证据，未直接采用为最终高等级')
        level = 'insufficient'
    elif level == 'low' and not (review.source_review_complete and review.receptor_review_complete and review.pathway_review_complete):
        limitations.append('源、受体或迁移路径未完整审查，不能以缺数据给出低风险')
        level = 'insufficient'
    elif level == 'low' and any(row.priority in {'high', 'medium'} and row.evidence_refs for row in pathways):
        limitations.append('仍有中高优先级风险路径，低等级建议与证据不一致')
        level = 'insufficient'
    conclusion = review.conclusion
    if level != review.final_level:
        conclusion = '最终环境风险等级暂列为证据不足。' + limitations[-1] + '。' + '已保留初评、复核意见与具体风险路径，先补齐会改变判断的关键证据。'
    return {'status': 'completed' if level != 'insufficient' else 'needs_more_data',
            'final_level': level, 'risk_label': RISK_LABELS[level], 'conclusion': conclusion,
            'source_pathway_receptors': [row.model_dump(mode='json') for row in pathways],
            'attribution': [row.model_dump(mode='json') for row in review.attribution],
            'compliance_checks': rows, 'next_steps': list(review.next_steps),
            'limitations': list(dict.fromkeys([*limitations, BOUNDARY])), 'warnings': warnings,
            'review_proposed_level': review.final_level}


async def initial_risk_assessment(pipeline, run_id, task, assets, documents, quality, traces):
    from .adaptive import image_parts, model_asset_context
    config = pipeline.settings.models.get('recognizer')
    if not config or not config.ready or 'image' not in config.input_modalities:
        return {'status': 'not_configured', 'preliminary_level': 'insufficient', 'scene_description': '', 'observations': [],
                'limitations': ['见微环境描述与初评模型尚未配置图像能力']}
    config = _public_config(config, timeout=60, tokens=3500)
    if not assets:
        return {'status': 'needs_more_data', 'preliminary_level': 'insufficient', 'scene_description': '', 'observations': [],
                'limitations': ['未取得可分析影像，尚未开展环境初评']}
    base = ('本轮任务是已知堆存场景的环境风险初评，不是重新搜索有没有固废。用户的堆存前提是其提供的信息，不是模型新作的法定认定。'
            '重点描述堆体、覆盖/围挡、排水、坡地、水体、居民或农田等可见关系，分析径流、扬尘、渗入、失稳、火灾等可能路径。'
            '给出高/中/低/证据不足的初步环境筛查等级，并区分直接观察、推断和缺失资料。不能仅凭颜色推断化学成分、危废、已经污染或违法；不编造米制距离、面积和日期。')
    pipeline.event(run_id, 'environmental_risk_initial', 'running', {'title': '我先描述堆存场景并做环境风险初评', 'summary': '围绕源、迁移路径和受体查看影像，不重复套用疑点发现结论。'})
    try:
        if pipeline.settings.rs_output_format != 'xml':
            result, trace = await pipeline.models.complete(config, base + '所有观察必须引用给定asset_id；低等级需要充分审查受体和路径，缺图或看不清不算低风险。',
                {'task': task.model_dump(mode='json'), 'assets': [model_asset_context(a) for a in assets], 'quality': quality,
                 'documents': [{'id': d['id'], 'text': d.get('text', '')[:6000]} for d in documents[:6]]}, PreliminaryRiskAssessment, image_parts(pipeline, assets))
            _validate_refs(result.observations, assets, documents)
            _validate_refs([r for row in result.risk_factors for r in row.evidence_refs], assets, documents)
            _check_claims(result.scene_description)
            for factor in result.risk_factors:
                _check_claims(factor.source_hypothesis)
                _check_claims(factor.receptor)
            trace['purpose'] = 'environmental_risk_preliminary'; traces.append(trace)
            outcome = {'status': 'completed', **result.model_dump(mode='json')}
        else:
            from .rs_xml import NATIVE_XML_PROMPT, parse_rs_xml
            sem = asyncio.Semaphore(2)
            async def one(asset):
                async with sem:
                    raw, trace = await pipeline.models.complete_native(config,
                        NATIVE_XML_PROMPT + '\n本次任务优先级：' + base +
                        '保留训练的answer字段：risk_level只作环境初评；conclusion写当前场景环境事实、路径和受体，不写通用扫描结论。decision与regions仍遵循该XML的一致性要求，不能用未检出改写用户提供的堆存前提。',
                        {'task': task.model_dump(mode='json'), 'asset': model_asset_context(asset), 'quality': quality,
                         'waste_status_source': 'user_reported_not_independently_verified'}, image_parts(pipeline, [asset]))
                    parsed = parse_rs_xml(raw, asset_id=asset['id'], width=asset['width'], height=asset['height'])
                    _check_claims(parsed.native['conclusion'])
                    trace['purpose'] = 'environmental_risk_preliminary'; traces.append(trace)
                    return {'asset_id': asset['id'], 'scene_description': parsed.native['conclusion'],
                            'preliminary_level': {'高': 'high', '中': 'medium', '低': 'low'}.get(parsed.native['risk_level'], 'insufficient'),
                            'observations': [{'asset_id': asset['id'], 'document_id': None, 'statement': parsed.native['conclusion'], 'pixel_window': None}],
                            'native_risk_scope': 'model_self_report_not_statutory'}
            results = await asyncio.wait_for(asyncio.gather(*(one(asset) for asset in assets), return_exceptions=True), timeout=120)
            good = [row for row in results if isinstance(row, dict)]
            levels = [row['preliminary_level'] for row in good]
            rank = {'insufficient': 0, 'low': 1, 'medium': 2, 'high': 3}
            level = max(levels, key=lambda value: rank[value]) if levels else 'insufficient'
            outcome = {'status': 'completed' if len(good) == len(assets) else 'partial' if good else 'failed',
                       'preliminary_level': level, 'scene_description': '；'.join(row['scene_description'] for row in good),
                       'observations': [ref for row in good for ref in row['observations']], 'per_image': good,
                       'limitations': ['初评等级为见微原生自报值，需由综合复核和证据门禁审查'],
                       'failed_asset_ids': [a['id'] for a, result in zip(assets, results) if not isinstance(result, dict)]}
        pipeline.event(run_id, 'environmental_risk_initial', outcome['status'], {'summary': outcome.get('scene_description', '')[:1200], 'preliminary_level': outcome['preliminary_level']})
        return outcome
    except (ProviderError, ValueError, OSError, TimeoutError) as error:
        pipeline.event(run_id, 'environmental_risk_initial', 'failed', {'summary': '环境初评未返回有效结果，保留已取得影像，不将失败解释为低风险。', 'failure_kind': _safe_failure(error)})
        return {'status': 'failed', 'preliminary_level': 'insufficient', 'scene_description': '', 'observations': [],
                'limitations': ['见微环境初评未完成，尚未形成可采用的初步等级'], 'failure_kind': _safe_failure(error)}


def _empty_result(task, preliminary, status='needs_more_data', reason='尚未完成环境风险综合复核'):
    return {'schema': 'environmental-risk-v13', 'status': status, 'final_level': 'insufficient', 'risk_label': '证据不足',
            'conclusion': reason + '。现阶段保留具体环境描述和数据缺口，不能以未完成分析给出低风险。',
            'grading_basis': 'product_screening_not_statutory', 'waste_status_source': 'user_reported' if getattr(task, 'waste_status', '') == 'user_confirmed' else 'unverified',
            'preliminary_review': preliminary, 'final_review': {'status': status}, 'source_pathway_receptors': [],
            'attribution': [], 'compliance_checks': [], 'next_steps': ['补齐可判读的场景影像，并完成初评和综合复核'],
            'limitations': [reason, BOUNDARY], 'warnings': []}


async def assess_environmental_risk(pipeline, run_id, task, assets, documents, quality, policy, traces, scientific_evidence=None):
    from .adaptive import image_parts, model_asset_context
    from .search import source_evidence
    optical = [a for a in assets if a.get('metadata', {}).get('modality') == 'optical' and not a.get('science_preview')]
    # The model only sees these bounded documents; an unseen seventh document
    # cannot become a supporting citation merely because its ID was requested.
    review_documents = documents[:6]
    preliminary = await initial_risk_assessment(pipeline, run_id, task, optical, review_documents, quality, traces)
    config = pipeline.settings.models.get('science_analyst')
    if not optical:
        result = _empty_result(task, preliminary, reason='本轮没有可判读的场景光学影像')
    elif not config or not config.ready or 'image' not in config.input_modalities:
        result = _empty_result(task, preliminary, status='not_configured', reason='Gemini环境综合复核尚未配置图像能力')
    else:
        config = _public_config(config, timeout=90, tokens=6500)
        pipeline.event(run_id, 'environmental_risk_review', 'running', {'title': '我正在复核环境风险等级和标准适用性', 'summary': '综合原图、环境初评、用户资料和本轮实际取得的政策来源，逐项检查路径与受体。'})
        try:
            review, trace = await pipeline.models.complete(config,
                '你负责固废堆存环境风险的最终综合复核，不是投票判断有没有堆存。依据本次原图、见微初评和资料，给出高/中/低/证据不足的环境筛查等级。'
                '逐条写源—路径—受体、直接证据引用、综合归因与正常竞争解释、关键缺口。高风险需要清晰的敏感受体及迁移连通依据；低风险需要完整审查源、受体和路径，缺图和不清楚不得当作低风险。'
                '对见微初评可上调或下调，但必须用实际证据写review_rationale和disagreements。不得仅按初评等级照抄，也不得以其他图未检出抹去已有路径。'
                '国家、省级、市县标准逐条对照：只引用本轮给出的source_id，分别写requirement、observed_fact、事实证据evidence_ids、潜在不符合或缺证状态。'
                '官方网页也不自动证明现行适用，缺版本、辖区、许可、检测或现场条件时明确缺证；仅影像外观不能作确定违法/污染/危废认定。'
                'verified_noncompliance只能引用已授权核实的文书事实且有经核实的现行适用依据；用户说已确认堆存只是任务前提，不等于已确认污染或违法。'
                '用户关闭联网时不能补写未检索的法条，三层标准对照均说明未检索。不能虚构距离、面积、拍摄日期、材料成分或内部推理。'
                '所有影像事实引用给出的asset_id或资料document_id；像素框只能在该原图内，不能当米制测量。',
                {'task': task.model_dump(mode='json'), 'assets': [model_asset_context(a) for a in optical],
                 'preliminary_review': preliminary, 'quality': quality,
                 'documents': [{'id': d['id'], 'name': d.get('name'), 'text': d.get('text', '')[:6000],
                                'verification': d.get('verification'), 'provenance_kind': d.get('provenance_kind')} for d in review_documents],
                 'policy_status': policy.get('status'), 'policy_sources': source_evidence(policy),
                 'scientific_support': [{key: row[key] for key in ('source', 'group', 'status', 'analysis', 'calibration', 'catalog_complete') if key in row}
                                        for row in (scientific_evidence or [])[:8]],
                 'grading_boundary': BOUNDARY}, EnvironmentalRiskReview, image_parts(pipeline, optical))
            trace['purpose'] = 'environmental_risk_final_review'; traces.append(trace)
            result = validate_final_review(review, assets=optical, documents=review_documents, policy=policy, preliminary=preliminary)
            result.update(schema='environmental-risk-v13', grading_basis='product_screening_not_statutory',
                          waste_status_source='user_reported' if getattr(task, 'waste_status', '') == 'user_confirmed' else 'unverified',
                          preliminary_review=preliminary, final_review={'status': 'completed',
                              'proposed_level': review.final_level, 'accepted_level': result['final_level'],
                              'conclusion': result['conclusion'], 'review_rationale': review.review_rationale,
                              'disagreements': review.disagreements, 'compliance_checks': result['compliance_checks']})
            pipeline.event(run_id, 'environmental_risk_review', 'completed', {'summary': result['conclusion'], 'proposed_level': review.final_level})
        except (ProviderError, ValueError, OSError, TimeoutError) as error:
            result = _empty_result(task, preliminary, status='failed', reason='Gemini环境综合复核未返回有效的有据结论')
            result['final_review'] = {'status': 'failed', 'failure_kind': _safe_failure(error)}
            pipeline.event(run_id, 'environmental_risk_review', 'failed', {'summary': result['conclusion'], 'failure_kind': _safe_failure(error)})
    if not result['compliance_checks']:
        result['compliance_checks'] = _compliance_rows(EnvironmentalRiskReview(final_level='insufficient', conclusion='证据不足',
            scene_quality_sufficient=False, source_review_complete=False, receptor_review_complete=False,
            pathway_review_complete=False, review_rationale='尚未完成复核'), policy, documents,
            {a['id'] for a in assets} | {d['id'] for d in documents}, result['warnings'])
    # Position and time limits qualify the scene assessment rather than being
    # silently converted into a negative finding or a precise current site risk.
    registered = all((a.get('metadata', {}).get('registration_verified') is True or a.get('metadata', {}).get('georeferenced') is True) for a in optical) if optical else False
    result['spatial_scope'] = 'registered_scene_context' if registered else 'image_scene_only'
    if not registered:
        result['limitations'].append('等级仅适用于本次可见影像场景；绝对位置、地面距离和敏感区法定边界未核实')
    if optical and not all(a.get('metadata', {}).get('acquisition_time') for a in optical):
        result['limitations'].append('部分影像实际拍摄时间未核实，不能据此断言点位当前或指定日期的风险状态')
    pipeline.event(run_id, 'environmental_risk_validation', result['status'], {'title': '我已核对风险依据和仍缺的标准事实', 'summary': result['conclusion'], 'final_level': result['final_level']})
    return result


async def analyze_environmental_risk(pipeline, run_id, task):
    """Independent execution and output contract for a risk question.

    The generic detection/re-voting/report prompts must not run here: they ask
    whether a stockpile exists and cannot answer source/pathway/receptor risk.
    """
    from .adaptive import collect_evidence
    from .consumer_conclusion import build_consumer_conclusion
    from .presentation import evidence_presentation, publish_scientific_artifacts
    from .store import now
    from .task_results import build_task_results

    collected = await collect_evidence(pipeline, run_id, task, evidence_only=True)
    assets, quality = collected['assets'], collected['quality']
    traces, warnings = collected['traces'], collected['warnings']
    documents = [pipeline.store.get(identity, 'document') for identity in task.document_ids]
    if any(len(d.get('text', '')) > 6000 for d in documents) or len(documents) > 6:
        warnings.append('风险模型本轮阅读前六份资料且每份限前6000字；超出部分未作为已审阅证据')
    # Policy retrieval and its failure are explicit. Turning off search must not
    # silently use remembered laws or convert missing standards into compliance.
    if task.search_policy:
        pipeline.event(run_id, 'policy', 'running', {'summary': '我正在检索本点位适用的国家和地方官方依据，后续逐项对照可见事实与缺证。'})
        try:
            policy = await pipeline.policy.search(task)
        except (ProviderError, ValueError, OSError, TimeoutError):
            policy = {'status': 'failed', 'sources': [], 'queries': [], 'reason': '本轮官方依据检索未完成'}
    else:
        policy = {'status': 'disabled_by_user', 'sources': [], 'queries': []}
    pipeline.event(run_id, 'policy', policy.get('status', 'failed'))
    risk = await assess_environmental_risk(pipeline, run_id, task, assets, documents, quality, policy, traces, collected['scientific_evidence'])
    warnings.extend(risk.get('warnings', []))
    initial = risk['preliminary_review']
    # Reuse the registered observation format for image overlays and the ledger,
    # while an empty discovery candidate list never means "no stockpile" here.
    observations = [dict(asset_id=row['asset_id'], statement=row['statement'][:2000], pixel_window=row.get('pixel_window'))
        for row in initial.get('observations', []) if row.get('asset_id') in {a['id'] for a in assets}]
    recognition = Recognition(observations=observations, unknowns=list(risk.get('limitations', []))[:30])
    assessments = [{'asset_id': a['id'], 'decision': 'unknown', 'quality_passed': None,
        'target_relevant': None, 'analysis_purpose': 'environmental_risk',
        'reason': '本轮审查环境场景和迁移路径，未执行堆存发现的阳性/阴性判定'} for a in assets]
    fusion = {'level': 'not_applicable', 'screening_applicable': False,
        'judgment': risk['conclusion'], 'basis': BOUNDARY, 'support': 'environmental_risk_review',
        'environmental_risk_level': risk['final_level'], 'task_evidence_sufficient': risk['status'] == 'completed',
        'probability': None, 'positive_asset_ids': [], 'negative_asset_ids': [],
        'analysis_summary': [risk['conclusion']], 'boundary': BOUNDARY}
    task_results = build_task_results(task, assets, recognition, assessments, fusion,
        temporal_collection=collected['selection'].get('temporal_optical'), environmental_risk=risk,
        scientific_evidence=collected.get('scientific_evidence'))
    fusion['task_results'] = task_results
    narrative = {'summary': risk['conclusion'], 'consumer_conclusion': risk['conclusion'],
        'recommendations': list(risk['next_steps']),
        'cited_policy_ids': list(dict.fromkeys(row['source_id'] for row in risk['compliance_checks'] if row.get('source_id')))}
    conclusion = build_consumer_conclusion(task, assets, recognition, assessments, fusion,
        narrative=narrative, task_results=task_results, environmental_risk=risk)
    if conclusion:
        task_results['consumer_conclusion'] = conclusion
    status = ('draft_pending_human_review' if risk['status'] == 'completed' else
        'needs_configuration' if risk['status'] == 'not_configured' or initial['status'] == 'not_configured' else 'needs_more_data')
    stages = [row for row in collected['workflow_stages'] if row['id'] not in {'recognition_review', 'synthesis'}]
    stages.extend([
        {'id': 'environmental_risk_initial', 'label': '环境描述与初步风险等级', 'status': initial['status'],
         'summary': initial.get('scene_description') or '环境初评尚未完成'},
        {'id': 'environmental_risk_review', 'label': '环境风险综合复核', 'status': risk['final_review']['status'],
         'summary': risk['final_review'].get('review_rationale') or risk['conclusion']},
        {'id': 'environmental_risk_validation', 'label': '风险依据与标准事实核对', 'status': risk['status'], 'summary': risk['conclusion']},
        {'id': 'synthesis', 'label': '环境风险结论整理', 'status': 'completed', 'result_status': status}])
    pipeline.event(run_id, 'synthesis', 'completed', {'stage': 'synthesis', 'result_status': status,
        'summary': risk['conclusion'], 'environmental_risk_level': risk['final_level']})
    ledger, presentation = evidence_presentation(assets, recognition, assessments, collected['scientific_evidence'], fusion)
    return {'schema_version': '0.3.0', 'package_type': 'RemoteSensingFindingPackage', 'run_id': run_id,
        'created_at': now(), 'status': status, 'workflow_mode': 'environmental_risk', 'task': task.model_dump(mode='json'),
        'environmental_risk': risk, 'selection': collected['selection'], 'catalog': collected['catalog'], 'quality': quality,
        'assets': [pipeline.store.public_asset(a) for a in assets], 'recognition': recognition.model_dump(),
        'recognition_status': 'not_requested_for_this_workflow', 'small_model_validation': {'status': 'not_integrated', 'enabled': False},
        'native_rs_results': [], 'task_outline': collected['task_outline'], 'strategy': collected['strategy'],
        'workflow_stages': stages, 'adaptive_review': collected['adaptive_review'], 'image_assessments': assessments,
        'fusion': fusion, 'task_results': task_results, 'consumer_conclusion': conclusion,
        'scientific_evidence': collected['scientific_evidence'], 'research_result': None,
        'scientific_artifacts': publish_scientific_artifacts(collected['scientific_evidence'], pipeline.store.run_dir(run_id)),
        'evidence_summary': ledger, 'evidence_presentation': presentation,
        'screening_priority': {'level': risk['final_level'], 'reason': BOUNDARY}, 'policy': policy,
        'documents': [{k: d[k] for k in ('id', 'name', 'sha256') if k in d} for d in documents],
        'specialist_review': {'status': 'not_required', 'reason': '环境风险已由独立综合复核阶段处理'},
        'narrative': narrative, 'narrative_status': 'completed' if risk['status'] == 'completed' else 'structured_incomplete',
        'model_calls': traces, 'warnings': list(dict.fromkeys(warnings)), 'limitations': risk['limitations']}
