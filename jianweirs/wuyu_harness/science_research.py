"""Goal-directed archive research, independent of basemap registration.

Catalog searches, local quality probes, native windows and model observations
are separate facts. A failed band read never means that an archive has no scene.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Literal

from pydantic import Field

from .data import DataClientError
from .models import ProviderError
from .schemas import Record
from .science import acquire_science_window, analyze_science, probe_s2_scene, rank_science_scenes
from .strategy import task_strategy
from .views import _bbox


class ResearchAction(Record):
    action: Literal['inspect_scene', 'search_period']
    source: str
    scene_id: str | None = None
    start: date | None = None
    end: date | None = None
    reason: str = Field(min_length=1, max_length=1200)


class ResearchDecision(Record):
    assessment: str = Field(max_length=3000)
    actions: list[ResearchAction] = Field(default_factory=list, max_length=8)
    stop: bool = False
    stop_reason: str = Field(default='', max_length=1500)


RESEARCH_INSTRUCTION = '''根据用户本轮目标和真实工具结果决定下一批科研取证动作。返回指定JSON，不输出内部思考。
用户问有无存档时优先回答目录事实；用户要看影像/分析或历史趋势时选择真实scene_id读取，兼顾目标月份、前后期和中间时段。
点位SCL失败、波段读取失败、没有覆盖、目录为零是不同情况。整景低云量不保证点位无云。优先尝试尚未读过的低云候选，不重复已成功的场景。
只能使用给定allowed_sources和candidate_scenes；search_period必须在allowed_time_range内，不改变用户坐标。不要因底图未配准而放弃独立WGS84科学影像检索。
看到局部云遮挡、短暂读取失败或时相缺口时，可换景/换月；仅在用户允许其他来源时换源。科研窗口不用凑卫星数量。
已有科学分析足够回答本轮问题时stop=true并解释；仅有目录不能说已做像元分析。混合像元、农事/含水/轨道差异须留给科学分析，不用单一指数或SAR明暗判定固废。
remaining_budget是本轮资源熔断边界，不是必须用满的配额。每个动作写针对现有证据缺口的具体原因。'''


def month_periods(start: date, end: date, maximum: int = 24) -> tuple[list[dict], bool]:
    """Cover every month, or explicitly sample evenly across a longer archive."""
    values = []
    cursor = start.replace(day=1)
    while cursor <= end:
        last = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        values.append({'label': cursor.strftime('%Y-%m'), 'start': max(start, cursor), 'end': min(end, last)})
        cursor = last + timedelta(days=1)
    if len(values) <= maximum:
        return values, True
    maximum = max(1, maximum)
    indices = sorted({round(i * (len(values) - 1) / max(1, maximum - 1)) for i in range(maximum)})
    return [values[i] for i in indices], False


def public_scene(scene: dict, source: str) -> dict:
    """Keep source facts but never put signed asset URLs into reports/prompts."""
    keys = ('id', 'acquired_at', 'bbox', 'platform', 'cloud_cover', 'orbit_direction',
            'relative_orbit', 'instrument_mode', 'polarizations', 'band', 'product')
    return {**{key: scene[key] for key in keys if key in scene}, 'source': source}


def research_bbox(task):
    if task.coordinate_system != 'wgs84':
        return None
    if task.bbox:
        return list(task.bbox)
    if task.center:
        return _bbox(*task.center, 600, 600)
    return None


def _catalog_sources(catalog):
    return {entry.get('source', entry.get('id')): (group, entry)
        for group in ('multispectral', 'hyperspectral', 'sar')
        for entry in catalog.get('groups', {}).get(group, {}).get('sources', [])
        if isinstance(entry, dict) and entry.get('source', entry.get('id'))}


def _scene_date(scene):
    try:
        return datetime.fromisoformat(str(scene.get('acquired_at')).replace('Z', '+00:00')).date()
    except (ValueError, TypeError):
        return None


class ArchiveResearch:
    def __init__(self, pipeline, run_id, task, catalog, existing_assets, request):
        from .intent import research_time_scope
        self.pipeline, self.run_id, self.task, self.request = pipeline, run_id, task, request
        self.context = existing_assets
        self.catalog = _catalog_sources(catalog)
        self.bbox = research_bbox(task)
        self.scope = research_time_scope(task, request)
        self.start, self.end = self.scope['start'], self.scope['end']
        self.budget = task_strategy(task, max_assets=pipeline.settings.max_assets)['research_budget']
        self.slots = min(self.budget['max_windows'], max(0, pipeline.settings.max_assets - len(existing_assets)))
        requested = list(request.sources)
        if not requested:
            from .demand import primary_science_sources
            requested = primary_science_sources(task)
        self.sources = list(dict.fromkeys(requested))
        self.items, self.scenes, self.queried, self.inspected = {}, {}, {}, set()
        self.assets, self.traces, self.warnings, self.history = [], [], [], []
        self.counts = {'catalog_queries': 0, 'quality_probes': 0, 'windows': 0, 'replan_rounds': 0}
        self.query_lock = asyncio.Semaphore(3)
        for source in self.sources:
            group, metadata = self.catalog.get(source, ('unknown', {}))
            self.items[source] = {'source': source, 'group': group, 'operation': group + '.search',
                'bbox': self.bbox, 'time_range': f'{self.start}/{self.end}', 'time_basis': self.scope['time_basis'],
                'status': 'not_queried', 'science_pixels_analyzed': False, 'scene_count': 0, 'scenes': [],
                'catalog_queries': [], 'monthly_availability': [], 'quality_probes': [], 'subsets': [],
                'selection_reason': '本轮用户目标指定的存档检索与科学分析', 'catalog_complete': False,
                'full_history_complete': False, 'spatial_basis': {
                    'scope': 'declared_WGS84_task_AOI_not_optical_candidate_geolocation',
                    'optical_target_correspondence_verified': False, 'can_repair_optical_registration': False},
                'download_access': metadata.get('download_access', metadata.get('data_access')),
                'spatial_selection': 'provider_footprint_intersects_requested_WGS84_AOI_not_local_cloud_quality',
                'source_parameters': {key: metadata[key] for key in ('name', 'native_resolution_m', 'pixel_spacing_m',
                    'band_count', 'wavelength_range_nm', 'product', 'science_format') if key in metadata}}

    def event(self, step, status, **data):
        self.pipeline.event(self.run_id, step, status, data)

    async def search(self, source, start, end, label=None):
        key = (source, start.isoformat(), end.isoformat())
        if key in self.queried:
            return self.queried[key]
        # Complete larger queries already establish this smaller interval.
        if any(s == source and date.fromisoformat(a) <= start <= end <= date.fromisoformat(b)
               and row.get('complete') for (s, a, b), row in self.queried.items()):
            return None
        if self.counts['catalog_queries'] >= self.budget['max_catalog_queries']:
            return None
        if source not in self.catalog:
            self.items[source].update(status='unsupported_source', reason='source_not_in_live_scientific_catalog')
            return None
        group, _ = self.catalog[source]
        self.counts['catalog_queries'] += 1
        limit = min(50, self.budget.get('catalog_limit', 50))
        row = {'period': label or start.strftime('%Y-%m'), 'time_range': f'{start}/{end}',
               'start': str(start), 'end': str(end), 'limit': limit, 'status': 'running', 'complete': False}
        self.queried[key] = row
        self.items[source]['catalog_queries'].append(row)
        params = {'source': source, 'bbox': self.bbox, 'limit': limit}
        params.update({'start': str(start) + 'T00:00:00Z', 'end': str(end) + 'T23:59:59Z'}
                      if group == 'sar' else {'datetime': row['time_range']})
        self.event('science_query', 'running', source=source, group=group, period=row['period'])
        async with self.query_lock:
            try:
                result = await self.pipeline.data.execute(group + '.search', params)
                values = result.get('items' if group == 'sar' else 'scenes')
                if not isinstance(values, list):
                    raise ValueError('invalid_scene_catalog')
                values = [s for s in values if isinstance(s, dict) and isinstance(s.get('id'), str)
                          and _scene_date(s) is not None and start <= _scene_date(s) <= end]
                more = bool(result.get('more_available') or result.get('results_limited') or len(result.get('items' if group == 'sar' else 'scenes', [])) >= limit)
                for scene in values:
                    self.scenes[(source, scene['id'])] = scene
                row.update(status='completed', returned=len(values), more_available=more, complete=not more,
                           retrieved_at=result.get('retrieved_at'))
                self.event('science_query', 'completed', source=source, group=group, period=row['period'], scene_count=len(values))
            except (DataClientError, ValueError) as error:
                code = error.code if isinstance(error, DataClientError) else 'invalid_scene_catalog'
                row.update(status='unavailable', reason=code, returned=0)
                self.event('science_query', 'unavailable', source=source, group=group, period=row['period'])
        # Split a truncated page using non-overlapping date intervals rather
        # than pretending that one page is a complete multi-year archive.
        if row.get('more_available') and start < end:
            middle = start + (end - start) // 2
            children = await asyncio.gather(self.search(source, start, middle, row['period']),
                self.search(source, middle + timedelta(days=1), end, row['period']))
            row['complete'] = all(child is not None and child.get('complete') for child in children)
        return row

    def refresh_catalog_facts(self, periods, all_months):
        for source, item in self.items.items():
            scenes = [scene for (s, _), scene in self.scenes.items() if s == source]
            scenes.sort(key=lambda scene: (_scene_date(scene), scene['id']))
            item.update(scenes=[public_scene(s, source) for s in scenes], scene_count=len(scenes))
            rows = []
            for period in periods:
                query = self.queried.get((source, str(period['start']), str(period['end'])))
                actual = [scene for scene in scenes if period['start'] <= _scene_date(scene) <= period['end']]
                complete = bool(query and query.get('complete'))
                rows.append({'period': period['label'], 'time_range': f"{period['start']}/{period['end']}",
                    'scene_count': len(actual), 'acquisition_dates': sorted({str(_scene_date(s)) for s in actual}),
                    'catalog_complete': complete,
                    'status': 'scenes_found' if actual else 'no_catalog_scenes' if complete else 'query_incomplete',
                    'quality_checked_scenes': 0, 'usable_windows': 0})
            item['monthly_availability'] = rows
            item['catalog_complete'] = all_months and bool(rows) and all(r['catalog_complete'] for r in rows)
            # An enumerated catalog is not a completed pixel-level history.
            # Cloudy/unread dates and unsampled months remain explicit gaps.
            item['full_history_complete'] = False
            item['completeness_scope'] = 'requested_time_range_catalog_only_not_all_scenes_analyzed'
            if scenes:
                if item.get('status') not in {'analyzed', 'downloaded_unanalyzed'}:
                    item['status'] = 'catalog_only'
            elif item.get('status') != 'unsupported_source':
                item['status'] = 'no_results' if item['catalog_complete'] else 'query_unavailable'

    def candidates(self):
        values = []
        for source in self.sources:
            scenes = [scene for (s, identity), scene in self.scenes.items() if s == source and (s, identity) not in self.inspected]
            # Spread candidates across months before comparing quality tiers.
            groups = {}
            for scene in scenes:
                groups.setdefault(_scene_date(scene).strftime('%Y-%m'), []).append(scene)
            for month, items in sorted(groups.items()):
                for scene in rank_science_scenes(items, anchor=_scene_date(items[0]))[:3]:
                    values.append(public_scene(scene, source))
        return values

    def fallback_actions(self, candidates):
        if not candidates:
            return []
        # Include endpoints and middle months without asserting a trend from
        # endpoint samples. Additional rounds can fill actual remaining gaps.
        target = min(self.slots - len(self.assets), 6 if self.request.mode == 'timeseries' else 3)
        choices = []
        by_period = {}
        for row in candidates:
            by_period.setdefault(str(row.get('acquired_at', ''))[:7], []).append(row)
        periods = sorted(by_period)
        ordered = sorted(range(len(periods)), key=lambda i: min(i, len(periods) - i - 1))
        if len(periods) > 2:
            ordered = list(dict.fromkeys([0, len(periods) - 1, len(periods) // 2, *ordered]))
        for i in ordered:
            choices.append(by_period[periods[i]][0])
        choices += [row for row in candidates if row not in choices]
        return [ResearchAction(action='inspect_scene', source=row['source'], scene_id=row['id'],
            reason='按用户时段选择尚未检查的代表时相，实际核验点位质量后再读取科学窗口') for row in choices[:max(0, target)]]

    async def decide(self, round_index):
        candidates = self.candidates()
        payload = {'user_task': self.task.objective, 'research_request': self.request.model_dump(mode='json'),
            'allowed_sources': self.sources, 'allowed_time_range': f'{self.start}/{self.end}',
            'candidate_scenes': candidates, 'catalog': [{k: item[k] for k in ('source', 'scene_count', 'catalog_complete', 'monthly_availability')} for item in self.items.values()],
            'quality_results': [{k: probe.get(k) for k in ('source', 'scene_id', 'scene_acquired_at', 'status', 'quality_screening')} for item in self.items.values() for probe in item['quality_probes']],
            'analyses': [item.get('analysis') for item in self.items.values() if item.get('analysis')],
            'previous_actions': self.history, 'remaining_budget': {
                'catalog_queries': self.budget['max_catalog_queries'] - self.counts['catalog_queries'],
                'quality_probes': self.budget['max_quality_probes'] - self.counts['quality_probes'],
                'windows': self.slots - len(self.assets), 'rounds': self.budget['max_replan_rounds'] - round_index}}
        config = self.pipeline.settings.models.get('main')
        if config and config.ready:
            try:
                result, trace = await self.pipeline.models.complete(config, RESEARCH_INSTRUCTION, payload, ResearchDecision)
                self.traces.append(trace)
                return result
            except (ProviderError, ValueError):
                self.history.append({'action': 'research_replan', 'status': 'unavailable', 'reason': 'main_planner_unavailable_using_catalog_facts'})
        return ResearchDecision(assessment='根据实际目录与剩余预算选择代表时相', actions=self.fallback_actions(candidates))

    async def inspect(self, action):
        key = (action.source, action.scene_id)
        scene = self.scenes.get(key)
        if scene is None or key in self.inspected or len(self.assets) >= self.slots:
            return False
        self.inspected.add(key)
        source, item = action.source, self.items[action.source]
        group, metadata = self.catalog[source]
        row = {'action': 'inspect_scene', 'source': source, 'scene_id': action.scene_id,
               'reason': action.reason, 'status': 'running'}
        self.history.append(row)
        supported = source in {'sentinel2', 'sentinel1-rtc', 'landsat'}
        if not supported:
            row.update(status='not_attempted', reason_code='provider_authorization_required' if metadata.get('download_authentication_required') or 'login' in str(metadata.get('data_access')) else 'native_window_adapter_not_available')
            item['download_status'] = row['reason_code']
            return False
        if source == 'sentinel2':
            if self.counts['quality_probes'] >= self.budget['max_quality_probes']:
                row.update(status='skipped_budget', reason_code='quality_probe_budget_exhausted')
                return False
            self.counts['quality_probes'] += 1
            self.event('science_quality_probe', 'running', source=source, scene_id=action.scene_id)
            try:
                probe = await probe_s2_scene(self.pipeline, self.run_id, self.task, scene, self.bbox)
            except (DataClientError, ValueError, OSError) as error:
                probe = {'scene_id': action.scene_id, 'scene_acquired_at': scene.get('acquired_at'),
                    'status': 'read_failed', 'quality_screening': {'status': 'unknown', 'usable': False,
                        'reason': error.code if isinstance(error, DataClientError) else type(error).__name__}}
            item['quality_probes'].append(probe)
            self.event('science_quality_probe', 'completed' if probe.get('status') == 'usable' else 'unavailable',
                source=source, scene_id=action.scene_id, quality_status=probe.get('quality_screening', {}).get('status'))
            if probe.get('status') != 'usable':
                row.update(status='quality_rejected' if probe.get('quality_screening', {}).get('status') == 'obscured_or_unusable' else 'read_failed',
                    reason_code=probe.get('quality_screening', {}).get('reason', 'quality_unknown'))
                return False
        self.event('science_subset', 'running', source=source, scene_id=action.scene_id)
        try:
            if source == 'landsat':
                from .science_landsat import acquire_landsat_window
                asset, evidence = await acquire_landsat_window(self.pipeline, self.run_id, self.task, scene, self.bbox)
            else:
                asset, evidence = await acquire_science_window(self.pipeline, self.run_id, self.task, group, source, scene, self.bbox)
            evidence.update(selection_reason=action.reason, scene_acquired_at=scene.get('acquired_at'))
            item['subsets'].append(evidence)
            if asset:
                self.assets.append(asset)
                self.counts['windows'] += 1
                row.update(status='completed', asset_id=asset['id'])
            else:
                row.update(status='read_failed', reason_code=evidence.get('reason', 'no_preview'))
        except (DataClientError, ValueError, OSError, KeyError) as error:
            row.update(status='read_failed', reason_code=error.code if isinstance(error, DataClientError) else type(error).__name__)
        self.event('science_subset', 'completed' if row['status'] == 'completed' else 'unavailable', source=source, scene_id=action.scene_id)
        return row['status'] == 'completed'

    async def analyze_new(self):
        for source, item in self.items.items():
            assets = [asset for asset in self.assets if asset.get('metadata', {}).get('source') == source]
            ids = [asset['id'] for asset in assets]
            if not assets or ids == item.get('_analyzed_ids'):
                continue
            item['_analyzed_ids'] = ids
            windows = [window for window in item['subsets'] if window.get('preview_asset_id') in ids]
            self.event('science_analysis', 'running', source=source)
            item['analysis'] = await analyze_science(self.pipeline, self.task, assets,
                {'source': source, 'windows': windows, 'spatial_basis': item['spatial_basis']}, self.traces, context_assets=self.context)
            item['science_pixels_analyzed'] = item['analysis'].get('science_pixels_analyzed', False)
            item['status'] = 'analyzed' if item['science_pixels_analyzed'] else 'downloaded_unanalyzed'
            self.event('science_analysis', 'completed' if item['science_pixels_analyzed'] else 'unavailable', source=source)

    async def run(self):
        if not self.bbox:
            return self.finish('unavailable', '缺少明确WGS84任务区域，不能从未知坐标基准猜测科学取图位置', False)
        periods, all_months = month_periods(self.start, self.end, self.budget['max_periods'])
        # Keep every source represented at early/middle/recent dates when a
        # multi-source archive exceeds this run's query budget. Never spend the
        # entire budget on the oldest months and silently omit recent ones.
        initial_limit = max(1, self.budget['max_catalog_queries'] - min(8, self.budget['max_catalog_queries'] // 4))
        period_capacity = max(1, initial_limit // max(1, len(self.sources)))
        if len(periods) > period_capacity:
            indices = sorted({round(i * (len(periods) - 1) / max(1, period_capacity - 1)) for i in range(period_capacity)})
            initial_periods = [periods[i] for i in indices]
        else:
            initial_periods = periods
        jobs = [(source, period) for period in initial_periods for source in self.sources]
        await asyncio.gather(*(self.search(source, p['start'], p['end'], p['label']) for source, p in jobs))
        self.refresh_catalog_facts(periods, all_months)
        answered = any(query.get('status') == 'completed' for item in self.items.values() for query in item['catalog_queries'])
        if self.request.mode == 'availability':
            complete = all(item['catalog_complete'] for item in self.items.values())
            return self.finish('completed' if complete else 'partial' if answered else 'unavailable',
                '已按月份核对指定存档；目录有景与下载/可分析状态分别记录' if answered else '本轮目录请求未成功，不能回答无数据', answered)
        stop_reason = '本轮科研迭代资源已用完，保留实际已检索和已分析的证据及未解决缺口'
        for round_index in range(self.budget['max_replan_rounds']):
            if not self.candidates():
                stop_reason = '指定来源的候选场景已检查，或目录未取得候选；未把失败写成没有影像'
                break
            if self.slots <= len(self.assets):
                stop_reason = '本轮影像窗口槽位已满，目录结果仍保留，不重复下载已有场景'
                break
            self.counts['replan_rounds'] += 1
            self.event('research_replan', 'running')
            decision = await self.decide(round_index)
            self.event('research_replan', 'completed')
            self.history.append({'action': 'research_replan', 'status': 'completed', 'assessment': decision.assessment,
                                 'stop': decision.stop, 'stop_reason': decision.stop_reason})
            if decision.stop and not self.assets and self.slots and any(row['source'] in {'sentinel2', 'landsat', 'sentinel1-rtc'} for row in self.candidates()):
                # A requested image analysis has not been fulfilled merely by
                # finding a catalog page. Try a real candidate before stopping.
                decision = ResearchDecision(assessment=decision.assessment, actions=self.fallback_actions(self.candidates()))
            if decision.stop:
                stop_reason = decision.stop_reason or '现有科研证据已回答本轮目标，停止无必要追加'
                break
            changed = False
            queried_before = self.counts['catalog_queries']
            for action in decision.actions:
                if action.source not in self.sources:
                    continue
                if action.action == 'search_period':
                    if action.start and action.end and self.start <= action.start <= action.end <= self.end:
                        previous = self.counts['catalog_queries']
                        await self.search(action.source, action.start, action.end)
                        changed |= self.counts['catalog_queries'] > previous
                else:
                    before = len(self.inspected)
                    await self.inspect(action)
                    changed |= len(self.inspected) > before
            if self.counts['catalog_queries'] > queried_before:
                self.refresh_catalog_facts(periods, all_months)
            await self.analyze_new()
            if not changed:
                stop_reason = '规划未提出范围内尚未执行的有效动作，停止重复请求并保留缺口'
                break
        completed = any(item.get('analysis', {}).get('status') == 'completed' for item in self.items.values())
        return self.finish('completed' if completed else 'partial' if answered else 'unavailable', stop_reason, answered)

    def finish(self, status, stop_reason, answered):
        for source, item in self.items.items():
            item.pop('_analyzed_ids', None)
            if item['subsets']:
                item['subset'] = item['subsets'][0]
            item['analyzed_acquisition_dates'] = sorted({str(window.get('scene_acquired_at', ''))[:10]
                for window in item['subsets'] if window.get('preview_asset_id')})
            for period in item['monthly_availability']:
                month = period['period']
                period['quality_checked_scenes'] = sum(str(probe.get('scene_acquired_at', '')).startswith(month) for probe in item['quality_probes'])
                period['usable_windows'] = sum(str(window.get('scene_acquired_at', '')).startswith(month) and bool(window.get('preview_asset_id')) for window in item['subsets'])
            if item['scene_count'] and not item['subsets'] and self.request.mode != 'availability':
                item['status'] = 'catalog_found_no_usable_window'
            item['download_attempts'] = [row for row in self.history if row.get('action') == 'inspect_scene' and row.get('source') == source]
        return {'assets': self.assets, 'scientific_evidence': list(self.items.values()), 'traces': self.traces,
            'warnings': self.warnings, 'review': {'status': status, 'mode': self.request.mode,
                'availability_answered': answered, 'request': self.request.model_dump(mode='json'),
                'time_scope': {key: str(value) if isinstance(value, date) else value for key, value in self.scope.items()},
                'action_history': self.history, 'counters': self.counts, 'budget': self.budget,
                'stop_reason': stop_reason, 'stop_reason_code': 'research_goal_or_resource_boundary'}}


async def research_science(pipeline, run_id, task, catalog, existing_assets, request):
    return await ArchiveResearch(pipeline, run_id, task, catalog, existing_assets, request).run()
