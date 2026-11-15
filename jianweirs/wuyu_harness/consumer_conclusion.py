"""A concise, auditable projection of effective findings; no model or IO calls."""
from __future__ import annotations

import re
from collections.abc import Mapping

from .evidence import candidate_presence_evidence, claim_issues, reject_unsupported_claims, sanitize_claims


def _record(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else dict(value) if isinstance(value, Mapping) else {}


def _clean(value, limit=90):
    text = re.sub(r'<[^>]*>|[*#`]', '', str(value or ''))
    text = re.sub(r'\b[0-9a-f]{32}\b', '相关影像', text)
    return re.sub(r'\s+', ' ', text).strip(' ，,。；;')[:limit]


def _period(task, fusion, task_results, assets):
    value = fusion.get('requested_period_evidence') or task_results.get('requested_period_evidence')
    if isinstance(value, Mapping) and value.get('assets'):
        return dict(value)
    try:
        from .schemas import TaskRequest
        from .fusion import requested_period_evidence
        fields = {key: val for key, val in task.items() if key in TaskRequest.model_fields}
        fields.setdefault('objective', '核查影像'); fields.setdefault('task_types', ['suspected_site_discovery'])
        computed = requested_period_evidence(TaskRequest.model_validate(fields), assets)
        return {**computed, **value, 'assets': computed['assets']} if isinstance(value, Mapping) else computed
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'required': bool(task.get('date_start') or task.get('date_end')), 'ready': False, 'assets': []}


def _root(asset, by_id):
    seen = set()
    while asset.get('derived_from_asset_id') in by_id and asset['id'] not in seen:
        seen.add(asset['id']); asset = by_id[asset['derived_from_asset_id']]
    return asset


def _candidates(assets, recognition, assessments, presence):
    by_id = {asset['id']: asset for asset in assets}
    by_assessment = {row.get('asset_id'): row for row in assessments}
    pending = set(presence.get('pending_asset_ids') or [])
    observations = recognition.get('observations') or []
    counters, records = {}, []
    for candidate in recognition.get('candidates') or []:
        evidence = [observations[i] for i in candidate.get('evidence_indices', [])
                    if type(i) is int and 0 <= i < len(observations)]
        identities = list(dict.fromkeys(row.get('asset_id') for row in evidence))
        if not identities and len(assets) == 1:
            identities = [assets[0]['id']]
        for identity in identities:
            asset = by_id.get(identity, {})
            if identity not in pending or asset.get('metadata', {}).get('modality', 'optical') != 'optical':
                continue
            local_index = counters.get(identity, 0); counters[identity] = local_index + 1
            windows = [list(row['pixel_window']) for row in evidence if row.get('asset_id') == identity and row.get('pixel_window')]
            assessed = by_assessment.get(identity, {})
            review = assessed.get('optical_false_positive_review') or {}
            reviewed = review.get('candidates') or [] if review.get('status') == 'completed' else []
            matched = next((row for row in reviewed if any(window in (row.get('original_pixel_windows') or [])
                or window == row.get('corrected_pixel_window') for window in windows)), None)
            if matched is None and len(reviewed) == 1:
                matched = reviewed[0]
            if matched and matched.get('effective_status') == 'excluded_obvious_false_positive':
                continue
            texts = (matched or {}).get('visible_evidence') or []
            texts = texts if isinstance(texts, list) else [texts]
            if not texts and review.get('status') != 'completed':
                texts = [row.get('statement', '') for row in evidence if row.get('asset_id') == identity
                         and not re.search(r'原生模型标注|normalized_1000|模型类别为', row.get('statement', ''))]
            if not texts and review.get('status') == 'completed':
                texts = [review.get('summary', '')]
            visible = [_clean(text) for text in texts if _clean(text)]
            detail = asset.get('detail_review_target') or {}
            parent = _root(asset, by_id)
            inherited = (asset.get('derived_from_asset_id') and detail.get('parent_asset_id') == asset.get('derived_from_asset_id')
                         and type(detail.get('candidate_index')) is int)
            index = detail['candidate_index'] if inherited else (matched or {}).get('candidate_index', local_index)
            object_key = (parent['id'], index) if inherited or not asset.get('derived_from_asset_id') else None
            records.append({'asset_id': identity, 'root_asset_id': parent['id'], 'object_key': object_key,
                'candidate_index': index, 'label': _clean(candidate.get('label'), 60) or '疑似堆放物料',
                'effective_status': (matched or {}).get('effective_status', 'unreviewed_candidate'),
                'visible_evidence': visible, 'pixel_windows': windows,
                'competing_interpretation': _clean((matched or {}).get('competing_interpretation'), 100),
                'derived': bool(asset.get('derived_from_asset_id')), 'lineage_recorded': bool(inherited),
                'water_adjacency': any(re.search(r'临水|水体(?:边|岸)|河岸|河边|沟渠(?:边|旁)|紧邻.{0,8}水体', text) for text in visible),
                'quality_passed': assessed.get('quality_passed') is True})
    # An audit can retain a candidate even when a legacy package lacks the
    # effective recognition record. Do not invent a material label in that case.
    represented = {row['asset_id'] for row in records}
    for identity in pending - represented:
        asset = by_id.get(identity)
        if not asset or asset.get('metadata', {}).get('modality', 'optical') != 'optical':
            continue
        review = by_assessment.get(identity, {}).get('optical_false_positive_review') or {}
        for row in review.get('candidates') or []:
            if row.get('effective_status') == 'excluded_obvious_false_positive':
                continue
            values = row.get('visible_evidence') or []; values = values if isinstance(values, list) else [values]
            visible = [_clean(text) for text in values if _clean(text)]
            records.append({'asset_id': identity, 'root_asset_id': _root(asset, by_id)['id'], 'object_key': None,
                'candidate_index': row.get('candidate_index'), 'label': '疑似堆放物料',
                'effective_status': row.get('effective_status', 'uncertain'), 'visible_evidence': visible,
                'pixel_windows': row.get('original_pixel_windows') or [], 'competing_interpretation': _clean(row.get('competing_interpretation')),
                'derived': bool(asset.get('derived_from_asset_id')), 'lineage_recorded': False,
                'water_adjacency': any('临水' in text or '河岸' in text for text in visible),
                'quality_passed': by_assessment.get(identity, {}).get('quality_passed') is True})
    return records


def _time_basis(records, period, by_id):
    identities = list(dict.fromkeys(row['root_asset_id'] for row in records))
    rows = {row.get('asset_id'): row for row in period.get('assets', [])}
    adopted, reported, unknown = {}, {}, []
    for identity in identities:
        if by_id.get(identity, {}).get('metadata', {}).get('modality', 'optical') != 'optical':
            continue
        row = rows.get(identity, {})
        if row.get('actual_dates_verified') is True and row.get('reported_dates'):
            adopted[identity] = list(row['reported_dates'])
        else:
            unknown.append(identity)
            if row.get('reported_dates'):
                reported[identity] = list(row['reported_dates'])
    target = period.get('target_time') or {}
    requested = target.get('requested_month')
    if requested and re.fullmatch(r'\d{4}-\d{2}', requested):
        requested = f'{requested[:4]}年{int(requested[-2:])}月'
    elif target.get('requested_start'):
        requested = target['requested_start'] + ('至' + target['requested_end'] if target.get('requested_end') != target['requested_start'] and target.get('requested_end') else '')
    return {'status': 'unverified' if unknown or not identities else 'verified', 'optical_asset_ids': identities,
        'verified_dates_by_asset': adopted, 'unverified_reported_dates_by_asset': reported,
        'date_unknown_asset_ids': unknown, 'requested_period': requested,
        'requested_period_satisfied': bool(records) and all(rows.get(identity, {}).get('eligible') is True for identity in identities)
            if period.get('required') else None,
        'edition_is_capture_date': False, 'scientific_dates_establish_optical_date': False}


def _time_sentence(basis):
    if basis['status'] == 'unverified':
        return ('高分影像实际拍摄日期未核实，尚不能确认这些线索在' + basis['requested_period'] + '是否存在。'
                if basis.get('requested_period') else '高分影像实际拍摄日期未核实，当前状态需现场确认。')
    dates = list(dict.fromkeys(value for values in basis['verified_dates_by_asset'].values() for value in values))
    text = '相关高分影像实际日期为' + '、'.join(dates[:3]) + '。'
    if basis.get('requested_period_satisfied') is False:
        text += '这些影像尚不能完整回答' + (basis.get('requested_period') or '请求时段') + '的状态。'
    return text


def _model_text(value, *, pending, labels, time_basis, inactive_labels=()):
    """Check compatibility, not a second visual/material judgment."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = _clean(value, 601)
    if len(text) > 300:
        return None, 'length_limit'
    if text and text[-1] not in '。！？!?' and len(text) < 300:
        text += '。'
    try:
        reject_unsupported_claims(value)
    except ValueError:
        value, _ = sanitize_claims(value)
        text = _clean(value, 601)
        if not text or claim_issues(text):
            return None, 'unsupported_claim'
    for label in inactive_labels:
        term = label.removeprefix('疑似').removeprefix('一般')
        if len(term) < 3:
            continue
        for clause in re.split(r'[，,。；;]', text):
            if term in clause and not re.search(r'排除|并非|不是|未确认|无法确认|不能确认', clause):
                return None, 'excluded_or_unretained_material_type'
    if pending:
        if not re.search(r'疑似|候选|线索|可能', text):
            return None, 'missing_candidate_hypothesis'
        typed = [label.removeprefix('疑似') for label in labels if label != '疑似堆放物料']
        if typed and not any(label in text for label in typed):
            return None, 'generic_or_unrelated_candidate_summary'
    # Dates in an uncertainty statement are allowed. A scientific date, product
    # edition or requested month cannot be promoted to an optical capture fact.
    known = {date for dates in time_basis['verified_dates_by_asset'].values() for date in dates}
    for clause in re.split(r'[，,。；;]', text):
        if re.search(r'未知|未核|未实|待核|无法|不能|尚不|不确定|缺少|没有.{0,6}日期', clause):
            continue
        if re.search(r'拍摄|成像|影像日期|已(?:经)?存在|已形成', clause):
            dates = re.findall(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', clause)
            if any(date not in known for date in dates) or (time_basis['status'] == 'unverified' and re.search(r'\d{4}年', clause)):
                return None, 'unverified_optical_time_claim'
    if time_basis['status'] == 'unverified' and not re.search(r'(?:日期|时间|时段).{0,14}(?:未|待|未知|无法|不能)|(?:无法|不能).{0,12}(?:日期|时间|时段)|(?:无法|不能|尚不能)确认.{0,25}\d{4}年', text):
        # Append the time limit rather than discarding an otherwise sound answer.
        text = text.rstrip('。') + '。影像实际拍摄日期尚未核实。'
        if len(text) > 330:
            return None, 'missing_optical_time_limit'
    return text, None


def build_consumer_conclusion(task, assets, recognition, assessments, fusion, *, narrative=None, task_results=None, environmental_risk=None):
    """Project effective evidence into one short paragraph, without mutating it."""
    task, recognition, fusion = _record(task), _record(recognition), _record(fusion)
    narrative, task_results = _record(narrative), _record(task_results)
    from .task_outputs import presentation_contract
    specialized = presentation_contract({'task': task, 'assets': assets, 'task_results': task_results,
        'narrative': narrative, 'environmental_risk': environmental_risk or task_results.get('environmental_risk')})
    if specialized['enabled'] and specialized['workflow_mode'] in {'environmental_risk', 'temporal_change', 'contextual_qa'}:
        return {'schema_version': 'consumer-conclusion-v13', 'text': specialized['lead'],
            'status': 'task_specific_result', 'origin': 'task_specific_evidence_projection',
            'workflow_mode': specialized['workflow_mode'], 'presentation': specialized}
    if task.get('search_only') or fusion.get('screening_applicable') is False or fusion.get('level') == 'not_applicable':
        return {}
    assets = [_record(asset) for asset in assets]; assessments = [_record(row) for row in assessments]
    by_id = {asset['id']: asset for asset in assets if asset.get('id')}
    period = _period(task, fusion, task_results, assets)
    presence = candidate_presence_evidence(assets, assessments, recognition, period, task=task)
    records = _candidates(assets, recognition, assessments, presence)
    records.sort(key=lambda row: (row['water_adjacency'], row['quality_passed'], not row['derived'], row['effective_status'] == 'supported_candidate'), reverse=True)
    primary = records[0] if records else None
    related = ([row for row in records if row['object_key'] is not None and row['object_key'] == primary['object_key']]
               if primary and primary['object_key'] is not None else [primary] if primary else [])
    figures = {asset['id']: f'图{index + 1}' for index, asset in enumerate(assets)}
    identities = list(dict.fromkeys(row['asset_id'] for row in related))
    labels = list(dict.fromkeys(row['label'] for row in related))
    time_records = related or records or [{'root_asset_id': identity} for identity in presence['negative_review_asset_ids']]
    if not identities:
        identities = list(presence['negative_review_asset_ids'])
    time_basis = _time_basis(time_records, period, by_id)
    location = {'scope': 'image_relative', 'description': (primary['visible_evidence'] or ['图内候选位置'])[0] if primary else None,
        'asset_ids': identities, 'figure_labels': [figures[identity] for identity in identities],
        'pixel_windows': primary['pixel_windows'] if primary else [], 'task_center_is_candidate_coordinate': False}
    actions = []
    if presence['has_pending_candidates']:
        status = 'suspected_finding'
        if primary:
            cue = (primary['visible_evidence'] or ['图内保留尚未排除的物料候选'])[0]
            label = primary['label'] if primary['label'].startswith('疑似') else '疑似' + primary['label']
            references = '、'.join(figures[identity] for identity in identities)
            text = f'发现疑似固废堆存线索：{references}中{cue}，初步按{label}核查，仍需与正常施工或备料等解释区分。'
        else:
            text = '已有尚未排除的物料候选，但现有记录不足以说明具体类型与位置，需先核对原图候选。'
        text += _time_sentence(time_basis)
        actions = ['现场核对物料来源、用途与性质，区分废料和正常施工备料']
        if primary and primary['water_adjacency']:
            actions.append('优先核查岸边排水、水体连通与防护措施；可见邻接不证明污染')
            text += '重点现场核对物料来源和用途，并优先检查岸边排水、与水体的连通及防护措施。可见邻接仅提示优先核查，尚未确认污染。'
        else:
            text += '重点现场核对物料来源、用途和实际性质，确认是废料还是正常施工备料。'
    elif presence['negative_task_scope_complete'] and (not period.get('required') or period.get('ready')):
        status = 'not_detected'
        text = '本轮已审查影像的可判读范围未发现明确固废堆存线索，结论仅限这些影像的范围与时点。'
        text += _time_sentence(time_basis)
    else:
        status = 'insufficient_evidence'
        text = ('已复核候选已被有效排除，其余范围或质量仍不足以判断是否存在疑似固废。'
                if presence['all_observed_candidates_effectively_excluded'] else
                '尚无足以判断具体固废位置或类型的有效影像证据，需要补充可判读的目标影像后再核查。')
        if period.get('required') and not period.get('ready'):
            text += '请求时段的实际高分拍摄日期仍未核实。'
    kinds = task.get('task_types') or ['suspected_site_discovery']
    primary_kind = str(kinds[0])
    task_specific = primary_kind not in {'suspected_site_discovery', 'waste_type_screening'}
    if task_specific:
        rows = task_results.get('results') or []
        result = next((row for row in rows if row.get('task_type') == primary_kind), {})
        fallback = {'stockpile_change': '现有证据尚不能确认堆存变化方向', 'cleanup_progress': '现有证据尚不能确认清理进度',
            'residual_stockpile': '整改后残留位置与范围仍待核查', 'repeated_dumping': '现有证据尚不能确认清理后再次倾倒',
            'site_tracking': '点位逐期状态仍有证据缺口', 'stockpile_extent': '现有证据不足以可靠测量堆存范围',
            'environmental_context': '环境受体与迁移路径仍需核查', 'surrounding_relationship': '场地周边关系与边界仍需核对'}
        text = _clean(result.get('conclusion'), 350) or fallback.get(primary_kind, '该任务的有效结论尚需补充证据')
        text = text.rstrip('。') + '。'
        if not re.search(r'日期|拍摄|时段', text):
            text += _time_sentence(time_basis)
        actions = [_clean(value, 100) for value in result.get('next_steps', [])[:2] if isinstance(value, str) and value.strip()]
        if actions:
            text += '重点' + actions[0].rstrip('。') + '。'
    proposed, reason = (None, None)
    if presence['has_pending_candidates'] or task_specific:
        inactive = {str(row.get('label', '')) for row in recognition.get('candidates', [])} - {row['label'] for row in records}
        proposed, reason = _model_text(narrative.get('consumer_conclusion'), pending=presence['has_pending_candidates'] and not task_specific,
            labels=labels, time_basis=time_basis, inactive_labels=inactive)
    elif narrative.get('consumer_conclusion'):
        reason = 'no_pending_candidate_state_uses_effective_projection'
    return {'schema_version': 'consumer-conclusion-v1', 'text': proposed or text, 'status': status,
        'origin': 'report_model' if proposed else 'effective_evidence_projection', 'model_text_not_adopted_reason': reason,
        'evidence_asset_ids': identities, 'candidate_labels': labels, 'location_basis': location,
        'optical_time_basis': time_basis, 'field_verification': actions,
        'primary_task_type': primary_kind, 'text_scope': 'task_specific_result' if task_specific else 'effective_screening_result',
        'candidate_evidence': [{key: row[key] for key in ('asset_id', 'root_asset_id', 'candidate_index', 'label', 'effective_status',
            'visible_evidence', 'pixel_windows', 'competing_interpretation', 'derived', 'lineage_recorded')} for row in related],
        'independent_risk_point_count': None, 'crops_add_independent_evidence': False}


def package_consumer_conclusion(package):
    """Use a controlled saved projection, otherwise project a legacy package."""
    package = _record(package); task = _record(package.get('task'))
    fusion = _record(package.get('fusion'))
    from .task_outputs import presentation_contract
    presentation = presentation_contract(package)
    if presentation['enabled'] and presentation['workflow_mode'] in {'environmental_risk', 'temporal_change', 'contextual_qa'}:
        return {'schema_version': 'consumer-conclusion-v13', 'text': presentation['lead'],
            'status': 'task_specific_result', 'origin': 'task_specific_evidence_projection',
            'workflow_mode': presentation['workflow_mode'], 'presentation': presentation}
    if package.get('research_result') or task.get('search_only') or fusion.get('screening_applicable') is False or fusion.get('level') == 'not_applicable':
        return {}
    saved = package.get('consumer_conclusion') or _record(package.get('task_results')).get('consumer_conclusion')
    if isinstance(saved, Mapping) and isinstance(saved.get('text'), str) and saved['text'].strip():
        if presentation['enabled']:
            presentation = presentation_contract(package, base_lead=saved['text'])
            return {**saved, 'text': presentation['lead'], 'workflow_mode': presentation['workflow_mode'], 'presentation': presentation}
        return dict(saved)
    result = build_consumer_conclusion(task, package.get('assets') or [], package.get('recognition') or {},
        package.get('image_assessments') or [], fusion, narrative=package.get('narrative'), task_results=package.get('task_results'))
    if presentation['enabled']:
        presentation = presentation_contract(package, base_lead=result.get('text', ''))
        result.update(text=presentation['lead'], workflow_mode=presentation['workflow_mode'], presentation=presentation)
    return result
