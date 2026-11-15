"""Bounded optical history requests with requested and actual dates separated."""
from __future__ import annotations
from datetime import date
import re

from .data import DataClientError
from .schemas import TaskKind
from .strategy import TEMPORAL_TASKS, optical_source_allowed

from .source_catalog import HISTORICAL_OPTICAL as HISTORICAL  # noqa: E402


def endpoint_year_order(years):
    """Try the historical and recent endpoints before filling interior years."""
    ordered = sorted(set(years))
    if len(ordered) <= 2:
        return ordered
    endpoints = [ordered[0], ordered[-1], ordered[(len(ordered)-1)//2]]
    return list(dict.fromkeys([*endpoints, *ordered]))


def select_history_dates(dates, start=None, end=None, limit=3):
    """Keep temporal endpoints and a middle sample, never claim all dates read."""
    valid = set()
    for value in dates:
        try:
            parsed = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
        if (not start or parsed >= start) and (not end or parsed <= end):
            valid.add(parsed.isoformat())
    ordered = sorted(valid)
    if len(ordered) <= limit: return ordered
    indices = {0, len(ordered)-1}
    if limit >= 3: indices.add((len(ordered)-1)//2)
    return [ordered[index] for index in sorted(indices)[:limit]]


async def collect_temporal_optical(pipeline, run_id, task, strategy, selection, catalog, views, assets):
    from .intent import named_research_sources, _source_limits
    from .optical_time import (ANNUAL_SOURCE_YEARS, annual_retrieval_years, history_catalog_only, infer_optical_periods,
                               optical_source_preferences, optical_time_intent,
                               rank_history_dates)
    time_intent = optical_time_intent(task)
    nearest_first = time_intent['nearest_context_allowed']
    historic_names = (set(task.selected_sources) | set(named_research_sources(task.objective))) & HISTORICAL
    if 'siwei' in set(task.selected_sources) | set(named_research_sources(task.objective)) and time_intent['requested_years'] == [2025]:
        historic_names.add('siwei2025')
    required = (bool(set(task.task_types) & TEMPORAL_TASKS) or bool(task.optical_periods)
                or time_intent['constrained'] or bool(historic_names) and bool(re.search(r'历史|时序|前后|逐期|变化|哪些日期', task.objective)))
    record = {'required': required, 'status': 'not_required', 'requests': [], 'catalog_dates': [],
        'catalog_query_status': 'not_queried', 'catalog_complete': False, 'sampled': True,
        'requested_interval': [time_intent['requested_start'], time_intent['requested_end']],
        'retrieval_interval': [time_intent['retrieval_start'], time_intent['retrieval_end']],
        'target_time': time_intent, 'nearest_catalog_dates': [], 'catalog_dates_in_requested_interval': [],
        'edition_is_capture_date': False, 'max_source_period_attempts': 3, 'images_analyzed': False,
        'annual_product_first': False, 'dated_archive_first': False,
        'nearest_date_first': nearest_first, 'requested_month_date_gap': None,
        'date_qualified_asset_ids': []}
    if not required or not task.acquire_optical or task.online_supplement == 'uploaded_only' or not views:
        if required:
            record.update(status='needs_more_data', reason='本轮未授权在线取图或缺少目标范围，保留上传证据')
        return [], [], record
    remaining = max(0, pipeline.settings.max_assets - len(assets))
    record['max_image_attempts'] = remaining
    if not remaining:
        record.update(status='needs_more_data', reason='已有影像已用满本轮容量，未追加年度或历史取图')
        return [], [], record
    configured = {s.get('source', s.get('id')) for s in catalog.get('groups', {}).get('optical', {}).get('sources', [])
                 if isinstance(s, dict) and s.get('configured') is not False}
    contract = strategy['optical_source_contract']
    available = {source for source in configured if source in HISTORICAL and optical_source_allowed(source, contract)}
    explicit = [item.model_dump(mode='json', exclude_none=True) for item in task.optical_periods]
    declared = infer_optical_periods(task)
    if not explicit:
        # Text-derived ranges should not lose their latest year merely because
        # the bounded image budget stopped an ascending prefix halfway through.
        century_order = endpoint_year_order([row['year'] for row in declared
                                             if row['source'] == 'century' and row.get('year')])
        if len(century_order) > 2:
            positions = {year: index for index, year in enumerate(century_order)}
            declared = sorted(declared, key=lambda row: positions.get(row.get('year'), len(positions))
                              if row['source'] == 'century' else len(positions))
    requests = [item for item in declared if item['source'] in available]
    warnings = []
    if declared and len(requests) != len(declared):
        warnings.append('部分指定年度或历史图源尚未配置/不在本轮允许来源中，未替换为当前底图')
        record['requests'].extend({**item, 'asset_ids': [], 'status': 'unavailable', 'actual_dates': [],
            'date_qualified_asset_ids': [], 'requested_edition_is_actual_date': False,
            'reason_code': 'source_not_admitted_or_configured',
            'reason': '指定年度或历史图源尚未配置或不在本轮允许来源中，未发起取图'}
            for item in declared if item['source'] not in available)
    history_requests = []
    annual_pair = len(historic_names & {'century', 'siwei2025', 'jilin2024'}) >= 2
    catalog_only = history_catalog_only(task)
    history_forbidden = 'history246' in _source_limits(task.objective)[0] or 'history246' in contract.get('excluded_sources', [])
    history_catalog_allowed = ('history246' in configured and not history_forbidden
        and ((not explicit and 'history246' in available and (not historic_names or 'history246' in historic_names))
             or time_intent['constrained'] and annual_pair or catalog_only
             or nearest_first and 'history246' in available))
    if history_catalog_allowed:
        bounds = views[0]['bbox']
        from .vicinity import resolve_source_zoom
        history_entry = next((row for row in catalog.get('groups', {}).get('optical', {}).get('sources', [])
                              if isinstance(row, dict) and row.get('source', row.get('id')) == 'history246'), {})
        history_zoom = resolve_source_zoom(history_entry, task.optical_zoom)
        params = {'source': 'history246', 'lon': (bounds[0]+bounds[2])/2,
                  'lat': (bounds[1]+bounds[3])/2, 'zoom': history_zoom['zoom']}
        record['catalog_zoom_decision'] = history_zoom
        pipeline.event(run_id, 'temporal_catalog', 'running', {'source': 'history246', 'target_time': time_intent})
        try:
            if not history_zoom['supported']:
                raise DataClientError('unsupported_requested_zoom', history_zoom['limitation'])
            result = await pipeline.data.execute('optical.times', params)
            all_dates = select_history_dates(result.get('dates', []), limit=1000)
            ranked = rank_history_dates(all_dates, time_intent, limit=8)
            matched = [row['date'] for row in ranked if row['in_requested_interval']]
            record.update(catalog_dates=all_dates, catalog_query_status='completed',
                          catalog_complete=result.get('complete') is True,
                          nearest_catalog_dates=ranked,
                          catalog_dates_in_requested_interval=[stamp for stamp in all_dates
                              if (not time_intent['requested_start'] or stamp >= time_intent['requested_start'])
                              and (not time_intent['requested_end'] or stamp <= time_intent['requested_end'])])
            if time_intent['requested_month']:
                record['requested_month_date_gap'] = not bool(matched)
            if time_intent['constrained']:
                # An absent requested year does not justify taking the archive's
                # oldest image. The nearest outside-period image is labelled context.
                if nearest_first:
                    # Retry the next nearest real archive date when one image
                    # is unavailable, including a date in the previous year.
                    count = 3 if TaskKind.RECURRENCE in task.task_types else 2
                    candidates = ranked
                    if time_intent['nearest_candidate_search_bounded']:
                        candidates = [row for row in ranked
                            if (not time_intent['retrieval_start'] or row['date'] >= time_intent['retrieval_start'])
                            and (not time_intent['retrieval_end'] or row['date'] <= time_intent['retrieval_end'])]
                    chosen = [row['date'] for row in candidates[:count]]
                elif time_intent['calendar_precision'] in {'month','day'}:
                    chosen = matched[:3]  # A strict temporal restriction forbids outside context.
                else:
                    required_dates = strategy.get('required_distinct_dates', 0)
                    chosen = (select_history_dates(matched, limit=min(3, required_dates)) if required_dates
                              else matched[:1]) or [row['date'] for row in ranked[:1]]
                if not matched:
                    record['requested_period_catalog_gap'] = True
                    nearest = ranked[0] if ranked else None
                    record['catalog_gap_reason'] = ('本次目录未列出请求时段内日期；最近列出的日期为'
                        + nearest['date'] + '，距请求时段' + str(nearest['distance_to_requested_interval_days'])
                        + '天，只可作为异时背景' if nearest else '本次目录未返回可用日期，未推断该时段不存在影像')
                    warnings.append(record['catalog_gap_reason'])
            else:
                count = strategy.get('required_distinct_dates') or 3
                chosen = select_history_dates(all_dates, limit=min(3, count, remaining))
            history_requests = [{'source': 'history246', 'date': stamp} for stamp in chosen]
            if 'history246' not in available or catalog_only:
                record['history_catalog_only'] = True
                record['history_image_not_requested_reason'] = ('本轮明确只查询历史目录，未授权下载目录候选影像'
                    if catalog_only else '本轮限定的取图来源不含history246；已核对目录，未绕过图源选择下载其他来源')
                history_requests = []
            record['history_candidate_downloads_authorized'] = bool('history246' in available and not catalog_only)
            pipeline.event(run_id, 'temporal_catalog', 'completed', {'source': 'history246', 'date_count': len(all_dates),
                'sample_dates': chosen, 'nearest_dates': ranked, 'target_time': time_intent,
                'all_dates_analyzed': False, 'requested_period_catalog_gap': record.get('requested_period_catalog_gap', False)})
        except (DataClientError, ValueError, KeyError) as error:
            record.update(catalog_query_status='failed', reason=getattr(error, 'code', type(error).__name__))
            pipeline.event(run_id, 'temporal_catalog', 'failed', {'source': 'history246', 'code': record['reason']})
    fallback = []
    if not declared:
        if time_intent['constrained']:
            for source in optical_source_preferences(task):
                if source not in available or source == 'history246':
                    continue
                if source == 'century':
                    fallback.extend({'source': source, 'year': year} for year in endpoint_year_order(annual_retrieval_years(task)))
                elif source in ANNUAL_SOURCE_YEARS:
                    fallback.append({'source': source, 'year': ANNUAL_SOURCE_YEARS[source]})
        else:
            if 'century' in available:
                fallback.extend({'source': 'century', 'year': year} for year in (2021, 2025))
            fallback.extend({'source': source, 'year': year} for source, year in ANNUAL_SOURCE_YEARS.items() if source in available)
        if historic_names:
            fallback.sort(key=lambda item: item['source'] not in historic_names)
    # Explicit/named editions stay exact. An automatic period first uses archive
    # dates inside that period, then matching annual products; an outside date
    # remains labelled context and comes last. Month/day nearest-date permission
    # still preserves the user's requested anchor separately.
    if declared:
        requested = [*history_requests, *declared] if nearest_first else declared
        queue = [*history_requests, *requests] if nearest_first else requests
    elif time_intent['constrained']:
        in_period = [item for item in history_requests
                     if (not time_intent['requested_start'] or item['date'] >= time_intent['requested_start'])
                     and (not time_intent['requested_end'] or item['date'] <= time_intent['requested_end'])]
        context = [item for item in history_requests if item not in in_period]
        requested = [*history_requests, *fallback] if nearest_first else [*in_period, *fallback, *context]
        record['dated_archive_first'] = bool(in_period or nearest_first and history_requests)
        record['annual_product_first'] = not record['dated_archive_first'] and bool(fallback)
        queue = requested
    else:
        requested = history_requests
        queue = [*history_requests, *fallback]
    queue = list({(item['source'], item.get('year'), item.get('date')): item for item in queue}.values())
    if nearest_first:
        record['max_source_period_attempts'] = min(5,max(3,len(queue)))
    acquired, attempts, cursor = [], 0, 0
    max_attempts = record['max_source_period_attempts']
    while cursor < len(queue):
        item = queue[cursor]
        cursor += 1
        if attempts >= max_attempts or len(acquired) >= remaining:
            break
        if (not declared and not time_intent['constrained'] and attempts == 2 and not acquired
                and fallback and item.get('source') == 'history246'):
            item = fallback[0]
        if not time_intent['constrained'] and not declared and item in fallback and len(acquired) >= min(2, remaining):
            break
        attempts += 1
        capacity = remaining-len(acquired)
        view = {**views[0], **{key: value for key, value in item.items() if key != 'source'},
                'role': 'baseline' if not acquired else 'current',
                'acquisition_purpose': 'requested_period_optical' if time_intent['constrained'] else 'optical_temporal_sample'}
        if item['source'] != 'history246':
            # Annual services do not expose a month/date selector. An inherited
            # target day must never leak into an annual provider request.
            view.pop('date',None)
        if item['source'] == 'history246':
            view.pop('year',None)
        stamp = item.get('date') or item.get('year') or 'unknown'
        phase = 'temporal_'+str(stamp)
        previous_plans = len(selection.get('coverage_plans') or [])
        images, issues = await pipeline.acquire_views(run_id, task, selection, catalog,
            view_plan=[view], source_choices=[item['source']], phase=phase,
            plan_coverage=True, max_new_assets=capacity)
        warnings.extend(issues)
        acquired.extend(images)
        actual = list(dict.fromkeys(str(value) for image in images for value in
            (image.get('data_api_metadata', {}).get('actual_dates') or [])))
        qualified = []
        for image in images:
            meta = image.get('data_api_metadata', {})
            dates = meta.get('actual_dates') or ([meta['actual_date']] if meta.get('actual_date') else [])
            if (meta.get('temporal_evidence', {}).get('actual_capture_date_verified') is True and dates
                    and all((not time_intent['requested_start'] or str(value) >= time_intent['requested_start'])
                            and (not time_intent['requested_end'] or str(value) <= time_intent['requested_end']) for value in dates)):
                qualified.append(image['id'])
        record['date_qualified_asset_ids'].extend(qualified)
        plans = (selection.get('coverage_plans') or [])[previous_plans:]
        coverage = next((plan for plan in reversed(plans) if plan.get('source') == item['source'] and plan.get('phase') == phase), None)
        complete = coverage.get('download_complete') is True if coverage is not None else None
        partial = bool(images) and complete is False
        status = 'partial' if partial else 'downloaded' if images else 'failed'
        record['requests'].append({**item, 'asset_ids': [image['id'] for image in images],
            'status': status, 'actual_dates': actual,
            'date_qualified_asset_ids': qualified, 'requested_edition_is_actual_date': False,
            'asset_capacity': capacity,
            'required_window_count': coverage.get('required_windows') if coverage is not None else None,
            'downloaded_window_count': coverage.get('downloaded_window_count', len(images)) if coverage is not None else len(images),
            'coverage_complete': complete, 'actual_coverage_verified': False,
            'requested_month': time_intent['requested_month'],
            'selector_semantics': 'catalog_date_requested_actual_scope_still_requires_verification' if item['source']=='history246' else 'annual_edition_only_month_not_selectable',
            'reason_code': 'requested_period_coverage_incomplete' if partial else None if images else 'requested_period_image_unavailable',
            'reason': ('已取得部分计划窗口，仍有窗口未下载完成，不能视为该时相完整覆盖' if partial else None) if images else
                ('；'.join(str(issue) for issue in issues)[:800] or '未取得该图源请求版次或日期的影像，未替换为当前底图')})
    record['status'] = 'downloaded' if acquired else 'needs_more_data'
    record['downloaded_count'] = len(acquired)
    timeline = []
    for asset in acquired:
        meta = asset.get('data_api_metadata', {})
        actual = meta.get('actual_dates') or ([meta['actual_date']] if meta.get('actual_date') else [])
        verified = meta.get('temporal_evidence', {}).get('actual_capture_date_verified') is True
        timeline.append({'asset_id': asset['id'], 'source': asset.get('metadata', {}).get('source'),
                         'actual_dates': actual, 'actual_capture_date_verified': verified,
                         'edition_is_capture_date': False})
    record['chronological_assets'] = sorted(timeline, key=lambda row:
        (not row['actual_capture_date_verified'] or not row['actual_dates'],
         min(row['actual_dates']) if row['actual_dates'] else '', row['asset_id']))
    attempted_keys = {(row['source'], row.get('date'), row.get('year')) for row in record['requests']}
    record['not_attempted_requested_periods'] = [item for item in requested
        if (item['source'], item.get('date'), item.get('year')) not in attempted_keys]
    record['all_requested_periods_acquired'] = bool(requested) and not record['not_attempted_requested_periods'] and all(
        row['status'] == 'downloaded' for row in record['requests'] if (row['source'], row.get('date'), row.get('year'))
        in {(item['source'], item.get('date'), item.get('year')) for item in requested})
    record['requested_time_evidence_satisfied'] = bool(record['date_qualified_asset_ids'])
    record['requested_month_capture_evidence_ready'] = bool(record['date_qualified_asset_ids']) if time_intent['requested_month'] else None
    if time_intent['requested_month'] and not record['date_qualified_asset_ids']:
        record['date_gap_next_action'] = '尚无该月实际日期已核实的高分影像；可按任务需要检索该月及邻近的多光谱真实场景，仍不替代小目标形态证据'
    if acquired and (record['not_attempted_requested_periods'] or any(row['status'] != 'downloaded' for row in record['requests'])):
        record['status'] = 'partial'
    if acquired and time_intent['constrained'] and not record['date_qualified_asset_ids']:
        warnings.append('已取得年度产品或异时参考，但未核实请求时段内的实际拍摄日期，不能升级为该年份或发现时点的影像证据')
    if not acquired:
        record.setdefault('reason', '未取得请求时段的可用高分影像；目录日期或年度版次不表示已经分析该期像元')
    return acquired, warnings, record
