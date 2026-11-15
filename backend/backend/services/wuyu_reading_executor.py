"""Execute one material reader with its durable response and segment caches.

Owns cache integrity, atomic writes and per-job dispatch guards. Project state,
concurrent scheduling, charging and synthesis remain in wuyu_reading_workflow.
Evidence validation stays in wuyu_reading; raw paid responses are preserved.
"""
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .wuyu_errors import PipelineError
from .wuyu_reading import (encoded, sha, reader_prompt, validate_reader,
                           validate_grounded_reader, reader_acknowledgement)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise PipelineError('reading_cache_invalid', '阅读缓存路径不符合项目范围。')
    descriptor, temporary_name = tempfile.mkstemp(prefix='.r-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_response(task, root):
    paths = [root / 'response.json'] + sorted(root.glob('attempt-*/response.json'))
    if len(paths) > 129:raise PipelineError('reading_retry_limit', '阅读重试缓存超过受控数量。')
    for path in reversed(paths):
        if not path.is_file():continue
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise PipelineError('reading_cache_invalid', '阅读缓存文件超限或不安全。')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('task_id') != task['id'] or value.get('raw_sha256') != sha(value.get('raw', '')):continue
            if value.get('finish_reason')=='length':continue
            result = validate_grounded_reader(value['raw'], task)
            return {'reading_result': True, 'result': result,
                    'model_report': {'provider_called': False, 'response_reused': True}}
        except (ValueError, KeyError, PipelineError):
            continue
    # A segment cache alone cannot prove which image blocks were supplied.
    if task.get('visual_inputs'):return None
    proposals=[];issues=[];normalizations=[]
    for segment in task['segments']:
        key=task.get('cache_keys',{}).get(segment['id'])
        if not key:return None
        path=root.parent/'segments'/(key[:32]+'.json')
        if not path.is_file():return None
        if path.is_symlink()or path.stat().st_size>2*1024*1024:raise PipelineError('reading_cache_invalid','逐段阅读缓存超限或不安全。')
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('partial_validation')is True:return None
            if value.get('cache_key')!=key or value.get('segment_id')!=segment['id'] or value.get('source_text_sha256')!=segment['text_sha256']:return None
            proposals.extend(value['findings']);issues.extend(value.get('issues',[]))
            for item in value.get('normalizations', []):
                asset_correction = (isinstance(item, dict) and set(item) == {'code','segment_id','field','removed_count'}
                        and item['code'] == 'same_document_reference_removed_from_empty_asset_catalog'
                        and item['segment_id'] == segment['id'] and item['field'] == 'source_asset_ids'
                        and task.get('asset_catalog', {}).get(segment['material_id']) == []
                        and type(item['removed_count']) is int and 1 <= item['removed_count'] <= 4)
                quote_correction = (isinstance(item, dict) and set(item) == {'code','segment_id','field','original_segment_id','match_count'}
                        and item['code'] == 'exact_quote_relocated_within_same_material'
                        and item['segment_id'] == segment['id'] and item['field'] == 'segment_id'
                        and task.get('reader_contract') == 3 and type(item['match_count']) is int and item['match_count'] == 1
                        and item['original_segment_id'] != segment['id']
                        and any(s['id'] == item['original_segment_id'] and s['material_id'] == segment['material_id'] for s in task['segments']))
                relevance_correction = (isinstance(item, dict)
                        and set(item) == {'code','segment_id','field','original_value','normalized_value'}
                        and item['code'] == 'fractional_relevance_normalized_to_integer_scale'
                        and item['segment_id'] == segment['id'] and item['field'] == 'relevance'
                        and task.get('reader_contract') == 3 and type(item['original_value']) is float
                        and math.isfinite(item['original_value']) and 0 <= item['original_value'] <= 1
                        and type(item['normalized_value']) is int
                        and item['normalized_value'] == min(5, 1 + math.floor(item['original_value'] * 4 + .5))
                        and any(f.get('relevance') == item['normalized_value'] for f in value['findings']))
                summary_correction = (isinstance(item, dict)
                        and set(item) == {'code','segment_id','field','discarded_summary_sha256','unsupported_numeric_tokens','quote_sha256','replacement'}
                        and item['code'] == 'unsupported_numeric_summary_replaced_with_exact_quote'
                        and item['segment_id'] == segment['id'] and item['field'] == 'summary'
                        and task.get('reader_contract') == 3 and item['replacement'] == 'exact_original_quote'
                        and isinstance(item['discarded_summary_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', item['discarded_summary_sha256'])
                        and type(item['unsupported_numeric_tokens']) is int and 1 <= item['unsupported_numeric_tokens'] <= 4000
                        and any(isinstance(f.get('quote'), str) and f.get('summary') == f['quote']
                                and sha(f['quote']) == item['quote_sha256'] for f in value['findings']))
                discarded_relevance = (isinstance(item, dict)
                        and set(item) == {'code','segment_id','field','original_value','normalized_value'}
                        and item['code'] == 'out_of_range_relevance_discarded'
                        and item['segment_id'] == segment['id'] and item['field'] == 'relevance'
                        and task.get('reader_contract') == 3
                        and type(item['original_value']) is int and 6 <= item['original_value'] <= 10
                        and type(item['normalized_value']) is int and item['normalized_value'] == 3
                        and any(f.get('segment_id') == segment['id'] and type(f.get('relevance')) is int
                                and f['relevance'] == 3 for f in value['findings']))
                if asset_correction or quote_correction or relevance_correction or summary_correction or discarded_relevance:
                    normalizations.append(item)
                else:return None
        except (ValueError,KeyError,TypeError):return None
    if task['segments']:
        result=validate_reader({**reader_acknowledgement(task),'findings':proposals,'issues':issues},task)
        if normalizations:result['normalizations']=normalizations
        return {'reading_result':True,'result':result,'model_report':{'provider_called':False,'response_reused':True}}
    return None


def _cached(task, root, config=None, cancel_check=None):
    cached = _cached_response(task, root)
    if cached is not None and task.get('visual_inputs') and config is not None:
        from .wuyu_reading_visuals import verify_reader_image_integrity
        verify_reader_image_integrity(task, config, cancel_check)
    return cached


def run_reader(task, pipeline, root, job_id, allow_retry, cancel):
    cached = _cached(task, root, getattr(pipeline, 'config', {}), cancel)
    if cached is not None:return cached
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise PipelineError('reading_cache_invalid', '阅读缓存目录不符合项目范围。')
    previous = list(root.glob('attempt-*/dispatch.json'))
    if previous and not allow_retry:
        raise PipelineError('reading_retry_required', '此前阅读响应不明确，请显式重试未完成阅读；不会自动重复请求。')
    attempt = root / ('attempt-' + job_id)
    attempt.mkdir(exist_ok=True)
    if attempt.is_symlink():raise PipelineError('reading_cache_invalid', '阅读请求目录不安全。')
    marker = attempt / 'dispatch.json'
    try:descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:raise PipelineError('reading_retry_required', '此任务已发起过该阅读请求，不会自动再次调用。') from None
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(encoded({'task_id': task['id'], 'job_id': job_id}));stream.flush();os.fsync(stream.fileno())
    if cancel():raise PipelineError('cancelled', '阅读已取消。')
    from .wuyu_reading_visuals import reader_images
    images = reader_images(task,pipeline.config,cancel) if task.get('visual_inputs')else[]
    if cancel():raise PipelineError('cancelled', '阅读已取消。')
    # Once dispatched, save the paid response before the owning worker decides
    # whether it may publish. Never discard a response because quota changed.
    raw = pipeline.gateway.text('planner', reader_prompt(task), cancel_check=lambda:False,
                                max_output_tokens=task.get('output_tokens'),
                                timeout=min(900,180+math.ceil(task.get('input_tokens',0)/2000)),
                                **({'images':images}if images else{}))
    if not isinstance(raw, str) or len(raw.encode()) > 8 * 1024 * 1024:
        raise PipelineError('invalid_reading', '阅读模型响应超出安全边界。')
    # Persist the response before validation; ambiguous/failed IO is never silently retried.
    metadata = getattr(pipeline.gateway, 'last_response_metadata', {})
    finish = metadata.get('finish_reason') if isinstance(metadata,dict)else None
    _write(attempt / 'response.json', {'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw),'finish_reason':finish,
        'reader_contract': task.get('reader_contract', 1), 'segment_count': len(task['segments']),
        'output_tokens': task.get('output_tokens'), 'output_limits': task.get('output_limits', {})})
    if finish=='length':raise PipelineError('output_truncated','阅读模型输出达到长度上限，不能宣称阅读结果完整。')
    result = validate_grounded_reader(raw, task)
    for segment in task['segments']:
        key=task['cache_keys'][segment['id']]
        proposals=[{k:v for k,v in finding.items()if k in {'segment_id','quote','summary','topic','relevance','source_asset_ids','data_ids'}}for finding in result['findings']if finding['segment_id']==segment['id']and finding.get('fact_status')!='model_visual_transcription']
        _write(root.parent/'segments'/(key[:32]+'.json'),{'cache_key':key,'segment_id':segment['id'],
            'partial_validation':bool(result.get('rejected_findings')),
            'source_text_sha256':segment['text_sha256'],'findings':proposals,'issues':result['issues'],
            'normalizations':[item for item in result.get('normalizations',[]) if item['segment_id']==segment['id']]})
    return {'reading_result': True, 'result': result, 'model_report': {
        'provider_called': True, 'response_reused': False,
        'usage': metadata.get('usage', {}) if isinstance(metadata, dict) else {}}}
