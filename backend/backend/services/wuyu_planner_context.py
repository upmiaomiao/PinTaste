"""Complete extracted context, with explicit budgets and no model summarization."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import time

from .wuyu_errors import PipelineError, check_cancel
from .wuyu_materials import allowed_file, parse_local_material
from .wuyu_source_assets import estimate_tokens

READER_SCHEMA_VERSION = 3
DEEPSEEK_MODELS = {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4.1-flash'}
LONG_CONTEXT_MODELS=DEEPSEEK_MODELS|{'gemini-4-argon','gemini-4-argon-ssvip'}


def positive_int(config, keys, default, maximum=4_194_304):
    value = next((config.get(key) for key in keys if config.get(key) not in (None, '')), default)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise PipelineError('provider_config', '上下文预算必须为有限正整数。') from None
    if isinstance(value, bool) or str(result) != str(value) or not 1 <= result <= maximum:
        raise PipelineError('provider_config', '上下文预算必须为受控范围内的正整数。')
    return result


def prepare_materials(materials, config, cancel_check=None):
    """Re-read old parsed files under the original SHA; never use a cut summary."""
    prepared, refreshes, reparsed = [], [], []
    started = time.monotonic()
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = [roots] if isinstance(roots, str) else roots
    for original in materials or []:
        check_cancel(cancel_check)
        if not isinstance(original, dict):
            raise PipelineError('invalid_material', '材料须为受控的已解析记录。')
        material = deepcopy(original)
        if material.get('role') not in (None, '', 'material'):
            prepared.append(material)
            continue
        limits = material.get('processing_limits') or {}
        incomplete = bool(limits.get('truncated') or any(w.get('code') == 'material_truncated'
            for w in material.get('warnings', []) if isinstance(w, dict))
            or any(chunk.get('excerpted') for chunk in material.get('chunks', []) if isinstance(chunk, dict)))
        source_path = material.get('_source_path') or material.get('path')
        if not source_path and material.get('relative_path') and config.get('UPLOAD_FOLDER'):
            source_path = str(Path(config['UPLOAD_FOLDER']) / material['relative_path'])
        legacy = limits.get('reader_schema_version') != READER_SCHEMA_VERSION
        legacy_file_record = legacy and bool(material.get('relative_path') or limits)
        if source_path and (legacy_file_record or incomplete):
            path = allowed_file(source_path, roots)
            expected = material.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise PipelineError('material_hash_mismatch', '旧材料缺少原文件SHA，不能自动重读或用摘要代替。')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    check_cancel(cancel_check); digest.update(block)
            if digest.hexdigest() != expected:
                raise PipelineError('material_hash_mismatch', '原材料已变化，须重新上传解析；不会读取另一个文件。')
            old_count = len(material.get('chunks') or [])
            parsed = parse_local_material(path, config, material.get('filename'), cancel_check)
            if parsed['sha256'] != expected:
                raise PipelineError('material_hash_mismatch', '材料在完整重读期间变化，已停止规划。')
            values = {key: deepcopy(parsed.get(key)) for key in
                ('chunks', 'warnings', 'processing_limits', 'source_assets', 'source_structured_data', 'ocr_status', 'ocr_report')}
            values['text'] = '\n'.join(chunk['text'] for chunk in values['chunks'])
            material.update(values)
            incomplete = bool(values['processing_limits'].get('truncated'))
            reparsed.append({'filename': Path(material.get('filename') or path.name).name,
                'sha256': expected, 'old_chunks': old_count, 'new_chunks': len(values['chunks']),
                'same_source_sha256': True})
            if material.get('id'):
                refreshes.append(dict(values, id=material['id'], sha256=expected))
        if incomplete:
            error = PipelineError('material_incomplete', '材料原文提取仍超过页数、行数、字符或时间边界，未调用模型，也未摘要或截断后生成大纲。')
            error.diagnostics = {'context_report': {'input_mode': 'full_extracted_context',
                'summarization_used': False, 'materials': [{'filename': Path(str(material.get('filename') or '材料')).name,
                    'sha256': material.get('sha256'), 'extraction_complete': False,
                    'used_chars': (material.get('processing_limits') or {}).get('used_chars', 0)}]}}
            raise error
        prepared.append(material)
    return prepared, refreshes, reparsed, round(time.monotonic() - started, 3)


def material_context_report(materials, chunks):
    rows = []
    for material in materials:
        if material.get('role') not in (None, '', 'material'):
            continue
        selected = material.get('chunks') or ([material] if 'text' in material and 'id' in material else [])
        selected = [chunk for chunk in selected if chunk.get('evidence_kind', 'project_material') not in ('style_reference', 'asset_reference')]
        first_source = selected[0].get('source', {}) if selected else {}
        first_source = first_source if isinstance(first_source, dict) else {}
        chars = sum(len(str(chunk.get('text', ''))) for chunk in selected)
        rows.append({'filename': Path(str(material.get('filename') or first_source.get('filename') or '用户提供材料')).name,
            'sha256': material.get('sha256') or first_source.get('sha256'),
            'chunk_count': len(selected), 'chars': chars,
            'estimated_tokens': sum(estimate_tokens(str(chunk.get('text', ''))) for chunk in selected),
            'extraction_complete': not bool((material.get('processing_limits') or {}).get('truncated')),
            'table_count': len(material.get('source_structured_data') or {}), 'ocr_status': material.get('ocr_status')})
    return {'input_mode': 'full_extracted_context', 'summarization_used': False, 'materials': rows,
            'used_chunks': len(chunks), 'total_chunks': len(chunks),
            'used_chars': sum(len(chunk.get('text', '')) for chunk in chunks), 'truncated': False}
