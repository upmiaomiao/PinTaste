"""Safe public diagnostics and model/artifact projections."""
from __future__ import annotations
from copy import deepcopy
import math
import os
import re

def _report_text(value):
    if not isinstance(value, str) or len(value) > 500 or re.search(
            r'(?:^[A-Za-z]:[\\/]|^/|^\\\\|sk-[A-Za-z0-9]+|api[_-]?key|bearer\s|password[=:]|secret[=:])', value, re.I):
        return None
    return value

def _failure_site(exc, depth=4):
    """'KeyError at wuyu_reading.py:518 validate_reader <- wuyu_reading_workflow.py:301 run' (no message)."""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)[-depth:] if getattr(exc, '__traceback__', None) else []
    parts = [f"{os.path.basename(frame.filename)}:{frame.lineno} {frame.name}" for frame in reversed(frames)]
    return type(exc).__name__ + (' at ' + ' <- '.join(parts) if parts else '')

def _safe_failure_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', value) and not re.search(
        r'(?:sk_|password|secret|authorization|bearer)', value, re.I) else 'PROVIDER_OPERATION_FAILED'

def _failure_diagnostics(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    attempts = value.get('attempt_count')
    if type(attempts) is int and 1 <= attempts <= 4:
        result['attempt_count'] = attempts
    code = value.get('gateway_code')
    if isinstance(code, str) and code in {'MODEL_QUEUE_TIMEOUT', 'MODEL_QUEUE_FULL', 'MODEL_BODY_QUEUE_FULL',
                                         'MODEL_REQUEST_BODY_TIMEOUT', 'MODEL_UPSTREAM_TIMEOUT', 'MODEL_UPSTREAM_FAILED', 'MODEL_TIMEOUT'}:
        result['gateway_code'] = code
    stage = value.get('stage')
    if isinstance(stage, str) and stage in {'capacity_queue', 'model_queue', 'body_bytes', 'body_read', 'upstream',
                                          'gateway_unknown', 'client_timeout', 'connection', 'http_response', 'provider'}:
        result['stage'] = stage
    for key in ['response_cached', 'response_reused', 'provider_called']:
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ['prompt_sha256', 'response_sha256', 'diagnostic_id']:
        if isinstance(value.get(key), str) and re.fullmatch(r'[0-9a-f]{64}', value[key]):
            result[key] = value[key]
    for key in ['finish_reason', 'validation_code', 'request_id']:
        text = value.get(key)
        if _report_text(text) is not None and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', text):
            result[key] = text
    elapsed = value.get('elapsed_seconds')
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and 0 <= elapsed < 86400:
        result['elapsed_seconds'] = round(elapsed, 3)
    # Which figure on which page lacks evidence. Strictly shaped: a page number and
    # a few short strings made only of digits, signs, separators and a percent sign,
    # so no free text (and no credential-shaped content) can ride along.
    page = value.get('page')
    if isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 200:
        result['page'] = page
    numbers = value.get('unsupported_numbers')
    if isinstance(numbers, list):
        clean = [text for text in numbers[:12] if isinstance(text, str) and re.fullmatch(r'[0-9+\-.,eE%\s]{1,24}', text)]
        if clean:
            result['unsupported_numbers'] = clean
    if isinstance(value.get('usage'), dict):
        result['usage'] = {key: number for key, number in value['usage'].items()
            if key in {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens'}
            and isinstance(number, int) and not isinstance(number, bool) and number >= 0}
    if isinstance(value.get('context_report'), dict):
        result['context_report'] = _material_coverage(value['context_report'])
    return result

def _model_report(value):
    if not isinstance(value, dict):
        return {}
    result = {k: value[k] for k in ['real_model', 'fallback_used', 'provider_called'] if isinstance(value.get(k), bool)}
    for key in ['role', 'requested_model', 'model', 'provider_mode', 'mode', 'model_mode']:
        if _report_text(value.get(key)) is not None:
            result[key] = value[key]
    for key in ['response_model_version', 'finish_reason']:
        text = value.get(key)
        if _report_text(text) is not None and re.fullmatch(r'[A-Za-z0-9_.:+-]{1,160}', text):
            result[key] = text
    return result

def _public_runtime(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ['profile_id', 'image_model', 'image_protocol', 'fingerprint']
              if _report_text(value.get(key)) is not None}
    result.update({key: value[key] for key in ['schema_version', 'settings_version', 'image_workers', 'trace_workers']
                   if isinstance(value.get(key), int) and not isinstance(value[key], bool)})
    return result

def _material_coverage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ['used_chunks', 'total_chunks', 'used_chars', 'total_chars', 'context_tokens',
                'reserved_output_tokens', 'estimated_input_tokens', 'used_materials', 'total_materials',
                'used_pages', 'total_pages', 'source_figure_count', 'required_context_tokens']:
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
            result[key] = number
    for key in ['truncated', 'vision_enabled', 'english_budget', 'summarization_used', 'refinement_enabled']:
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ['used_material_ids', 'truncated_material_ids', 'unread_material_ids', 'dropped_chunk_ids',
                'source_pages_seen', 'omitted_evidence_ids', 'excerpted_evidence_ids', 'required_source_asset_ids']:
        if isinstance(value.get(key), list):
            result[key] = [v for v in value[key][:1000] if _report_text(v) is not None]
    if _report_text(value.get('sampling_strategy')) is not None:
        result['sampling_strategy'] = value['sampling_strategy']
    for key in ['input_mode', 'token_count_method', 'material_reader_model']:
        if _report_text(value.get(key)) is not None:
            result[key] = value[key]
    for key in ['materials', 'reparsed_materials']:
        if not isinstance(value.get(key), list):
            continue
        items = []
        for item in value[key][:1000]:
            if not isinstance(item, dict):
                continue
            entry = {}
            filename = item.get('filename')
            if _report_text(filename) is not None and '/' not in filename and '\\' not in filename:
                entry['filename'] = filename
            if isinstance(item.get('sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
                entry['sha256'] = item['sha256']
            for field in ['chunk_count', 'chars', 'estimated_tokens', 'table_count', 'old_chunks', 'new_chunks']:
                number = item.get(field)
                if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
                    entry[field] = number
            for field in ['extraction_complete', 'same_source_sha256']:
                if isinstance(item.get(field), bool):
                    entry[field] = item[field]
            if _report_text(item.get('ocr_status')) is not None:
                entry['ocr_status'] = item['ocr_status']
            items.append(entry)
        result[key] = items
    if isinstance(value.get('stage_timings'), dict):
        result['stage_timings'] = {key: number for key, number in value['stage_timings'].items()
            if key in {'material_read_seconds', 'context_prepare_seconds', 'planner_seconds', 'validation_seconds'}
            and isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0}
    if isinstance(value.get('server_figure_assignments'), list):
        result['server_figure_assignments'] = [_public_source(item) for item in value['server_figure_assignments'][:160]
                                               if isinstance(item, dict)]
    if isinstance(value.get('numeric_evidence_rechecks'), list):
        rechecks = []
        for item in value['numeric_evidence_rechecks'][:1000]:
            if not isinstance(item, dict) or item.get('extractor') != 'pymupdf':
                continue
            entry = {'extractor': 'pymupdf'}
            for key, upper in [('slide_number', 80), ('page', 300)]:
                if isinstance(item.get(key), int) and not isinstance(item[key], bool) and 1 <= item[key] <= upper:
                    entry[key] = item[key]
            for key in ['source_sha256', 'page_text_sha256']:
                if isinstance(item.get(key), str) and re.fullmatch(r'[0-9a-f]{64}', item[key]):
                    entry[key] = item[key]
            for key in ['evidence_id', 'material_id']:
                if _report_text(item.get(key)) is not None and re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', item[key]):
                    entry[key] = item[key]
            filename = item.get('filename')
            if _report_text(filename) is not None and '/' not in filename and '\\' not in filename:
                entry['filename'] = filename
            entry['recovered_numbers'] = [number for number in (item.get('recovered_numbers') or [])[:100]
                if isinstance(number, str) and len(number) <= 60 and re.fullmatch(
                    r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[%％]?', number)] if isinstance(item.get('recovered_numbers'), list) else []
            if all(key in entry for key in ['slide_number', 'page', 'source_sha256', 'page_text_sha256', 'evidence_id']) and entry['recovered_numbers']:
                rechecks.append(entry)
        result['numeric_evidence_rechecks'] = rechecks
    if isinstance(value.get('numeric_proposals'), list):
        labels = {'reference_unit_example': '核算示例（待确认）',
                  'illustrative_grade_labels': '拟议分级示例（待确认，非已发布标准）',
                  'proposed_application_combination': '建议组合（数量待确认）'}
        proposals = []
        for item in value['numeric_proposals'][:240]:
            if (not isinstance(item, dict) or item.get('kind') not in labels
                    or item.get('status') != 'requires_confirmation'
                    or item.get('requires_user_confirmation') is not True
                    or item.get('externally_verified') is not False
                    or type(item.get('slide_number')) is not int
                    or not 1 <= item['slide_number'] <= 80):
                continue
            proposals.append({'slide_number': item['slide_number'], 'kind': item['kind'],
                'status': 'requires_confirmation', 'requires_user_confirmation': True,
                'externally_verified': False, 'visible_label': labels[item['kind']],
                'fields': [field for field in (item.get('fields') or [])[:80]
                    if isinstance(field, str) and len(field) <= 120
                    and re.fullmatch(r'(?:title|copy|points|body)(?:[.\[\]A-Za-z0-9_]+)?', field)]
                    if isinstance(item.get('fields'), list) else [],
                'numbers': [number for number in (item.get('numbers') or [])[:20]
                    if isinstance(number, str) and len(number) <= 24
                    and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', number)]
                    if isinstance(item.get('numbers'), list) else []})
        result['numeric_proposals'] = proposals
    if isinstance(value.get('source_view_normalizations'), list):
        result['source_view_normalizations'] = [
            {key: item[key] for key in ['slide_number', 'asset_id', 'normalization', 'original_crop_sha256']}
            for item in value['source_view_normalizations'][:160] if isinstance(item, dict)
            and type(item.get('slide_number')) is int and 1 <= item['slide_number'] <= 80
            and item.get('normalization') == 'json_array_string'
            and isinstance(item.get('asset_id'), str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', item['asset_id'])
            and isinstance(item.get('original_crop_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', item['original_crop_sha256'])]
    return result

def _quality_report(value):
    if not isinstance(value, dict):
        return {}
    result = {k: value[k] for k in ['passed', 'json_repair_attempted', 'reused_raw_response', 'preview_unavailable']
              if isinstance(value.get(k), bool)}
    for timing_key in ('trace_performance', 'export_performance'):
        timing = value.get(timing_key)
        if isinstance(timing, dict):
            result[timing_key] = {key: number for key, number in timing.items()
                if key in {'version', 'total_seconds', 'provider_seconds', 'json_seconds', 'validation_seconds',
                    'provider_calls', 'local_attempts', 'cached_response_reused', 'explicit_retry', 'succeeded',
                    'render_seconds', 'local_export_seconds', 'page_count', 'native_probe_seconds',
                    'native_probe_cache_hits', 'native_probe_cache_misses', 'full_deck_qa_executed',
                    'input_tokens', 'output_tokens', 'usage_reported_calls'}
                and isinstance(number, (int, float)) and math.isfinite(number) and 0 <= number <= 10_000_000}
    if _report_text(value.get('visual_fidelity')) is not None:
        result['visual_fidelity'] = value['visual_fidelity']
    if isinstance(value.get('checks'), dict):
        result['checks'] = {k: v for k, v in value['checks'].items() if isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', k)
                            and (isinstance(v, bool) or v == 'not_applicable')}
    for key in ['geometry_corrections', 'normalization_corrections']:
        if isinstance(value.get(key), list):
            corrections = []
            for item in value[key][:2000]:
                if not isinstance(item, dict):
                    continue
                correction = {}
                for field in ['element_index', 'original_bounds', 'bounds', 'reason', 'field', 'original', 'normalized']:
                    part = item.get(field)
                    if isinstance(part, (int, float)) and not isinstance(part, bool) and math.isfinite(part):
                        correction[field] = part
                    elif isinstance(part, list) and len(part) <= 8 and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in part):
                        correction[field] = part
                    elif _report_text(part) is not None:
                        correction[field] = part
                if correction:
                    corrections.append(correction)
            result[key] = corrections
    return result

def _public_source(value):
    """Keep provenance identifiers/data while removing internal file handles."""
    if isinstance(value, list):
        return [_public_source(v) for v in value]
    if isinstance(value, dict):
        return {k: _public_source(v) for k, v in value.items()
                if not re.search(r'(?:^|_)(?:path|api_key|password|secret|authorization|token|raw_prompt|request_payload)$', k, re.I)}
    return deepcopy(value)

def _generation_report(output):
    result = {'model_report': _model_report(output.get('model_report')), 'qa': _quality_report(output.get('qa'))}
    if output.get('source_figure_mode') == 'reference_only':
        result['source_figure_mode'] = 'reference_only'
    if isinstance(output.get('source_reference_inputs'), list):
        references = []
        for item in output['source_reference_inputs'][:2]:
            if not isinstance(item, dict):
                continue
            entry = {key: item[key] for key in ['asset_id', 'figure_label'] if _report_text(item.get(key)) is not None}
            for key in ['sha256', 'input_sha256']:
                if isinstance(item.get(key), str) and re.fullmatch(r'[0-9a-f]{64}', item[key]):
                    entry[key] = item[key]
            index = item.get('input_image_index')
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 16:
                entry['input_image_index'] = index
            if item.get('usage') == 'source_content_reference':
                entry['usage'] = 'source_content_reference'
            if isinstance(item.get('source'), dict):
                source = item['source']
                entry['source'] = {}
                filename = source.get('filename')
                if _report_text(filename) is not None and '/' not in filename and '\\' not in filename:
                    entry['source']['filename'] = filename
                page = source.get('page')
                if isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 300:
                    entry['source']['page'] = page
                if isinstance(source.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', source['sha256']):
                    entry['source']['sha256'] = source['sha256']
                if _report_text(source.get('figure_label')) is not None:
                    entry['source']['figure_label'] = source['figure_label']
            crop = item.get('crop')
            if isinstance(crop, list) and len(crop) == 4 and all(isinstance(n, (int, float)) and not isinstance(n, bool)
                    and math.isfinite(n) and n >= 0 for n in crop) and min(crop[2:]) > 0:
                entry['crop'] = crop
            if all(key in entry for key in ['asset_id', 'sha256', 'input_sha256', 'input_image_index', 'usage']):
                references.append(entry)
        result['source_reference_inputs'] = references
    if isinstance(output.get('source_figures'), list):
        result['source_figures'] = [_public_source({k: v for k, v in figure.items() if k in {
            'asset_id', 'sha256', 'bounds', 'slot_bounds', 'source_crop_pixels', 'figure_label', 'source',
            'is_detail', 'rendered_pixel_sha256'}}) for figure in output['source_figures'][:2] if isinstance(figure, dict)]
    if isinstance(output.get('reference_inputs'), list):
        result['reference_inputs'] = []
        for item in output['reference_inputs'][:10]:
            if not isinstance(item, dict):
                continue
            entry = {k: value for k, value in item.items()
                if k in {'id', 'sha256', 'role', 'sample_kind', 'source_id', 'layout_family', 'selection_reason'} and _report_text(value) is not None}
            if isinstance(item.get('page_number'), int) and not isinstance(item['page_number'], bool):
                entry['page_number'] = item['page_number']
            if item.get('usage') == 'source_content_reference' and _report_text(item.get('asset_id')) is not None:
                entry.update(id=item['asset_id'], role='source_content_reference')
            index = item.get('input_image_index')
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 16:
                entry['input_image_index'] = index
            if entry:
                result['reference_inputs'].append(entry)
    selection = output.get('reference_selection')
    if isinstance(selection, dict):
        result['reference_selection'] = {k: value for k, value in selection.items()
            if k in {'provided_count', 'used_count', 'max_references', 'max_input_images', 'source_reference_count', 'total_input_images', 'candidate_count'}
            and isinstance(value, int) and not isinstance(value, bool)}
        if isinstance(selection.get('dropped_reference_ids'), list):
            result['reference_selection']['dropped_reference_ids'] = [v for v in selection['dropped_reference_ids'][:100] if _report_text(v) is not None]
        if _report_text(selection.get('priority')) is not None:
            result['reference_selection']['priority'] = selection['priority']
        if _report_text(selection.get('layout_family')) is not None:
            result['reference_selection']['layout_family'] = selection['layout_family']
        for key in ['deduplicated_reference_ids', 'page_specific_reference_ids']:
            if isinstance(selection.get(key), list):
                result['reference_selection'][key] = [value for value in selection[key][:100] if _report_text(value) is not None]
    if isinstance(output.get('edit_original_supplied'), bool):
        result['edit_original_supplied'] = output['edit_original_supplied']
    return result
