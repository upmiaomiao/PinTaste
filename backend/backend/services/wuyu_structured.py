"""Bind native tables/charts to re-readable project files, never model claims."""
import hashlib
import json
import math
from pathlib import Path
import re
from .wuyu_errors import PipelineError, check_cancel


def table_record(rows, path, filename, digest, *, sheet=None, truncated=False, locator=None):
    cleaned, chars = [], 0
    for row in rows[:100]:
        values = []
        for value in row[:30]:
            if value is not None and not isinstance(value, (str, int, float, bool)):
                value = value.isoformat() if hasattr(value, 'isoformat') else str(value)
            if isinstance(value, float) and not math.isfinite(value):
                value = str(value)
            if isinstance(value, str) and len(value) > 500:
                value, truncated = value[:500], True
            values.append(value)
        row_chars = sum(len(str(value or '')) for value in values)
        if chars + row_chars > 20000:
            truncated = True
            break
        chars += row_chars
        cleaned.append(values)
        truncated |= len(row) > 30
    if not cleaned:
        return None
    columns = max(len(row) for row in cleaned)
    if columns == 0:
        return None
    cleaned = [row + [None] * (columns - len(row)) for row in cleaned]
    source_locator = dict(locator or {})
    label = ('part-' + hashlib.sha256(json.dumps(source_locator, sort_keys=True).encode()).hexdigest()[:16]
             if source_locator else 'sheet-' + hashlib.sha256(sheet.encode()).hexdigest()[:12] if sheet is not None else 'delimited')
    data_id = f'{digest[:16]}:table:{label}'
    table_hash = hashlib.sha256(json.dumps(cleaned, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    return {'data_id': data_id, 'rows': cleaned, 'source_table_sha256': table_hash,
            'source': {'path': str(Path(path).resolve()), 'filename': filename, 'sha256': digest,
                       'sheet': sheet, 'row_start': 1, 'row_end': len(cleaned), **source_locator},
            'evidence_kind': 'project_material', 'provenance': 'bounded_local_file_table',
            'verification_scope': 'source_file_consistency_only', 'truncated': bool(truncated)}


def verified_sources(spec, config, cancel_check=None, cache=None):
    """Reparse original files so a client/model cannot self-mark data verified."""
    if spec.get('structured_data'):
        raise PipelineError('untrusted_structured_data', '模型或页面自填structured_data不能作为已验证项目数据。')
    records = spec.get('source_structured_data', {})
    if not isinstance(records, dict) or len(records) > 4:
        raise PipelineError('structured_limit', '每页最多引用四个有来源的结构化表。')
    if not records:
        return {}
    from .wuyu_materials import allowed_file, parse_local_material
    cache = {} if cache is None else cache
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = roots if isinstance(roots, list) else [roots]
    verified = {}
    for data_id, record in records.items():
        check_cancel(cancel_check)
        if not isinstance(record, dict) or record.get('data_id') != data_id or record.get('evidence_kind') != 'project_material':
            raise PipelineError('chart_source_required', '结构化数据缺少受控项目来源。')
        source = record.get('source', {})
        if not isinstance(source, dict) or not source.get('path') or not source.get('sha256'):
            raise PipelineError('chart_source_required', '结构化数据缺少原文件路径与hash。')
        path = allowed_file(source['path'], roots)
        if path.suffix.lower() not in ('.csv', '.tsv', '.xlsx', '.docx', '.pptx'):
            raise PipelineError('chart_source_required', '原生表格数据须来自可核对的本地Office、CSV或TSV来源。')
        cache_key = (str(path), source['sha256'])
        if cache_key not in cache:
            if len(cache) >= 16:
                raise PipelineError('structured_limit', '一次处理的结构化源文件超过16个。')
            parsed = parse_local_material(path, config, filename=source.get('filename'), cancel_check=cancel_check)
            if parsed['sha256'] != source['sha256']:
                raise PipelineError('stale_structured_data', '结构化原文件已变化，需重新解析并确认。')
            cache[cache_key] = parsed.get('source_structured_data', {})
        actual = cache[cache_key].get(data_id)
        if not actual or record.get('source_table_sha256') != actual.get('source_table_sha256'):
            raise PipelineError('chart_source_required', '结构化数据ID或内容hash与原文件不匹配。')
        verified[data_id] = actual
    return verified


def native_data(element, sources):
    record = sources.get(element.get('data_id'))
    if not record:
        raise PipelineError('chart_source_required', '原生图表或表格未引用当前页已核对的项目原文件。')
    rows = record['rows']
    mapping = element.get('data_mapping', {})
    if not isinstance(mapping, dict):
        raise PipelineError('invalid_trace', '数据列映射必须为对象。')
    if element['type'] == 'table':
        values = {'rows': rows}
    else:
        category = mapping.get('category_column')
        columns = mapping.get('value_columns')
        if isinstance(category, bool) or not isinstance(category, int) or not 0 <= category < len(rows[0]) or not isinstance(columns, list) or not 1 <= len(columns) <= 20 or any(isinstance(c, bool) or not isinstance(c, int) or not 0 <= c < len(rows[0]) or c == category for c in columns) or len(set(columns)) != len(columns):
            raise PipelineError('invalid_trace', '原生图表须明确给出合法的category_column与value_columns。')
        header = mapping.get('header', True)
        if not isinstance(header, bool):
            raise PipelineError('invalid_trace', '数据header标记必须为布尔值。')
        data_rows = rows[1:] if header else rows
        if not data_rows:
            raise PipelineError('chart_source_required', '结构化源表没有可绘制的数据行。')
        def numeric(value):
            if isinstance(value, str) and re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?', value.strip()):
                value = float(value.replace(',', ''))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e15:
                raise PipelineError('chart_source_required', '图表数值须为原表中的有限纯数值，单位文本不能被猜测转换。')
            return value
        values = {'chart_type': element.get('chart_type'),
                  'categories': [str(row[category] if row[category] is not None else '') for row in data_rows],
                  'series': [{'name': str(rows[0][col] if header else f'column_{col + 1}'),
                              'values': [numeric(row[col]) for row in data_rows]} for col in columns]}
    return {**values, 'source_data_verified': True,
            'source_data_provenance': {'data_id': record['data_id'], 'source_sha256': record['source']['sha256'],
                                       'source_table_sha256': record['source_table_sha256'], 'source': record['source'],
                                       'source_was_reparsed': True, 'verification_scope': 'source_file_consistency_only',
                                       'partial_source_table': record['truncated']}}
