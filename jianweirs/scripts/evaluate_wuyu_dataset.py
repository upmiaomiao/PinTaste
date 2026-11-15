"""Opt-in real planner / portal evaluation against the source-backed question bank.

No invocation happens on import. --plan-only uses the configured real DSH planner;
the default posts natural-language tasks to the running portal and polls results.
Expected sensor/date/task labels are scoring references and never execution input.
The suspected-site workbook supplies scenarios, not identification ground truth.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv
from wuyu_harness.auth_portal import COOKIE, PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.harness_planner import plan_task
from wuyu_harness.intent import resolve_research_request
from wuyu_harness.portal_config import PortalConfig
from wuyu_harness.schemas import TaskKind, TaskRequest

DEFAULT_FOLDER = ROOT / 'outputs/dataset-evaluation-20261018'
TERMINAL = {'succeeded', 'failed', 'planning_failed', 'interrupted'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_cases(path):
    content = json.loads(path.read_text(encoding='utf-8'))
    items = content.get('cases', content.get('questions', []))
    if not isinstance(items, list) or not items:
        raise ValueError('Question bank has no cases/questions.')
    result = []
    for original in items:
        item = dict(original)
        item.setdefault('case_id', item.get('question_id', item.get('id')))
        item.setdefault('objective', item.get('prompt', item.get('question')))
        if not isinstance(item['case_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item['case_id']):
            raise ValueError('Every case requires a safe, unique case_id/question_id.')
        if not isinstance(item['objective'], str) or not item['objective'].strip():
            raise ValueError('Every case requires a natural-language objective.')
        result.append(item)
        for followup in item.get('followups', []):
            followup = {**followup, 'parent_case_id': item['case_id']}
            followup.setdefault('case_id', followup.get('question_id'))
            followup.setdefault('objective', followup.get('prompt'))
            followup.setdefault('source_record_ids', item.get('source_record_ids', []))
            followup.setdefault('source_cells', item.get('source_cells', []))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(followup.get('case_id', ''))):
                raise ValueError('Follow-up has no safe case_id.')
            result.append(followup)
    unique = {}
    for item in result:
        old = unique.get(item['case_id'])
        if old is not None:
            if old['objective'] != item['objective']:
                raise ValueError('Different objectives cannot share one evaluation case ID.')
            item = {**item, **old}  # Keep the nested parent relationship.
        unique[item['case_id']] = item
    return content, list(unique.values())


def select_cases(cases, requested, limit):
    indexed = {item['case_id']: item for item in cases}
    selected, visiting, done = [], set(), set()

    def append(identity):
        if identity in done:
            return
        if identity not in indexed:
            raise ValueError(f'Unknown case or parent case: {identity}')
        if identity in visiting:
            raise ValueError('Cyclic parent case dependency.')
        visiting.add(identity)
        item = indexed[identity]
        if item.get('parent_case_id'):
            append(item['parent_case_id'])
        visiting.remove(identity)
        selected.append(item)
        done.add(identity)

    targets = requested or [item['case_id'] for item in cases]
    if limit:
        targets = targets[:limit]
    for identity in targets:
        append(identity)
    return selected


def planner_suite(cases):
    remaining = {item.value for item in TaskKind}
    selected = []
    for case in cases:
        kinds = set(case.get('expected', {}).get('task_types', case.get('task_types', [])))
        if remaining & kinds and not case.get('parent_case_id'):
            selected.append(case)
            remaining -= kinds
    if remaining:
        raise ValueError('Planner bank misses regulatory task types: ' + ', '.join(sorted(remaining)))
    return selected


def submission_body(case, *, parent_task_id=None, assets=None, documents=None):
    identity = case.get('case_id', case.get('question_id', 'case'))
    # Explicit scenario flags win. A policy capability / direct law-search request
    # is a legal use case; merely mentioning laws in a forbidden-claims rubric is not.
    if 'requires_policy_search' in case:
        policy = bool(case['requires_policy_search'])
    else:
        objective = re.sub(r'(?:不需要|无需|不用|不要|别)[^，。；;\n]{0,12}(?:检索|搜索|查询|查阅|查找|查)[^，。；;\n]*(?:法律|法规|政策|法条)', '', case['objective'])
        policy = 'policy_search' in case.get('capability_tags', []) or bool(re.search(
            r'(?:检索|搜索|查询|请查|查阅|查找|引用|补充)[^，。；;\n]{0,50}(?:法律|法规|政策|法条)', objective))
    body = {'objective': f'【清单实测 {identity}】' + case['objective'],
            'search_policy': policy}
    # Date, modality, task_types and research are intentionally left to the model.
    if case.get('center') is not None:
        body['center'] = case['center']
    elif case.get('coordinates'):
        coord = case['coordinates']
        body['center'] = [coord['longitude'], coord['latitude']]
    coordinate_system = case.get('coordinate_system', (case.get('coordinates') or {}).get('coordinate_system'))
    if coordinate_system in ('wgs84', 'unknown'):
        body['coordinate_system'] = coordinate_system
    elif 'center' in body:
        body['coordinate_system'] = 'unknown'
    if assets:
        body['asset_ids'] = list(dict.fromkeys(assets))
    if documents:
        body['document_ids'] = list(dict.fromkeys(documents))
    if parent_task_id:
        body['parent_task_id'] = parent_task_id
    return body


def planner_task(body):
    provided = set(body)
    payload = {**body, 'task_types': ['suspected_site_discovery']}
    payload.pop('parent_task_id', None)
    task = TaskRequest.model_validate(payload)
    task.model_fields_set.intersection_update(provided)
    task.research = resolve_research_request(task)
    task.model_fields_set.discard('research')
    if task.research.mode == 'availability':
        task.consult_shensi = False
        task.acquire_optical = False
    # This matches administrative defaults locked by the actual portal route.
    task.model_fields_set.update({'search_policy', 'consult_shensi', 'selector_mode', 'adaptive_review'})
    return task


def _query_bounds(items):
    ranges = []
    for item in items:
        for query in item.get('catalog_queries', []):
            if query.get('status') not in ('running', 'completed', 'failed'):
                continue
            start, end = query.get('start'), query.get('end')
            period = query.get('time_range')
            if (not start or not end) and isinstance(period, str) and '/' in period:
                start, end = period.split('/', 1)
            if start and end:
                ranges.append((str(start)[:10], str(end)[:10]))
    return {'start': min(pair[0] for pair in ranges), 'end': max(pair[1] for pair in ranges)} if ranges else {}


def _scientific_windows(items):
    identities = set()
    for item in items:
        subsets = [*item.get('subsets', [])]
        if isinstance(item.get('subset'), dict):
            subsets.append(item['subset'])
        for subset in subsets:
            if not isinstance(subset, dict):
                continue
            asset_id = subset.get('preview_asset_id')
            identity = asset_id or json.dumps([item.get('source'), subset.get('scene_id'), subset.get('bbox')], sort_keys=True)
            if asset_id or subset.get('scene_id'):
                identities.add(identity)
    return len(identities)


def _scientific_window_dates(items):
    dates = set()
    for item in items:
        subsets = [*item.get('subsets', [])]
        if isinstance(item.get('subset'), dict):
            subsets.append(item['subset'])
        for subset in subsets:
            if isinstance(subset, dict) and subset.get('preview_asset_id') and subset.get('scene_acquired_at'):
                dates.add(str(subset['scene_acquired_at'])[:10])
    return sorted(dates)


def _numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: _numeric_usage(item) if isinstance(item, dict) else item
            for key, item in value.items() if isinstance(item, (int, float, dict)) and not isinstance(item, bool)}


def audit_facts(db, portal_folder, task_id, user_id):
    """Read only this user's completed run; export factual whitelists, no prompts."""
    record = db.task(task_id, user_id)
    run_id = record.get('engine_run_id')
    if not run_id:
        return {}
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Invalid owned engine run ID.')
    db.require_owner('run', run_id, user_id)
    root = (portal_folder / 'engine/runs').resolve()
    package_path = (root / run_id / 'finding-package.json').resolve()
    if not package_path.is_relative_to(root) or not package_path.is_file():
        return {'run_id': run_id, 'package_available': False}
    package = json.loads(package_path.read_text(encoding='utf-8'))
    calls = [{key: call[key] for key in ('role', 'model', 'image_input_count') if key in call}
             | {'usage': _numeric_usage(call.get('usage', {}))}
             for call in package.get('model_calls', []) if isinstance(call, dict)]
    review = package.get('adaptive_review') or {}
    counters = {key: value for key, value in (review.get('counters') or {}).items() if isinstance(value, (int, float))}
    return {'run_id': run_id, 'package_available': True,
            'narrative_status': package.get('narrative_status'), 'recognition_status': package.get('recognition_status'),
            'model_calls': calls, 'model_call_count': len(calls),
            'replan_count': counters.get('replan_rounds'), 'research_counters': counters,
            'time_scope': {key: value for key, value in (review.get('time_scope') or {}).items()
                           if key in ('start', 'end', 'requested_anchor_start', 'requested_anchor_end', 'time_basis')},
            'scientific_window_count': _scientific_windows(package.get('scientific_evidence', [])),
            'scientific_window_dates': _scientific_window_dates(package.get('scientific_evidence', [])),
            'artifact_refs': {'finding_package': str(package_path), 'report_html': str(package_path.parent / 'report.html')}}


def assess(case, view, *, plan_only=False, audit=None):
    expected = case.get('expected', {})
    task = view.get('task', {})
    research = task.get('research') or {}
    items = [item for item in view.get('scientific_evidence', []) if isinstance(item, dict)]
    audit = audit or {}
    queried = sorted({item.get('source') for item in items if item.get('source') and
                      any(query.get('status') in ('running', 'completed', 'failed')
                          for query in item.get('catalog_queries', []) if isinstance(query, dict))})
    dates = sorted({str(scene.get('acquired_at', ''))[:10] for item in items
                    for scene in item.get('scenes', []) if scene.get('acquired_at')})
    for item in items:
        dates.extend(str(value)[:10] for row in item.get('monthly_availability', [])
                     for value in row.get('acquisition_dates', []))
    pixels = any(item.get('science_pixels_analyzed') is True or
                 (item.get('analysis') or {}).get('science_pixels_analyzed') is True for item in items)
    analyzed_sources = sorted({item.get('source') for item in items if item.get('source') and
                              (item.get('science_pixels_analyzed') is True or
                               (item.get('analysis') or {}).get('science_pixels_analyzed') is True)})
    summary = view.get('summary') or ''
    checks = []

    def check(identity, passed, desired, actual):
        checks.append({'id': identity, 'passed': bool(passed), 'expected': desired, 'actual': actual})

    check('execution_completed', view.get('status') == 'succeeded', 'succeeded', view.get('status'))
    kinds = set(expected.get('task_types', case.get('task_types', [])))
    if kinds and plan_only:
        check('task_routing', kinds <= set(task.get('task_types', [])), sorted(kinds), task.get('task_types', []))
    desired_mode = expected.get('research_mode')
    window_dates = audit.get('scientific_window_dates', _scientific_window_dates(items))
    equivalent_mode = (not plan_only and desired_mode == 'timeseries' and research.get('mode') == 'analysis'
                       and len(window_dates) >= 2)
    if desired_mode:
        check('research_mode', research.get('mode') == desired_mode or equivalent_mode, desired_mode,
              {'mode': research.get('mode'), 'equivalent_mode': equivalent_mode, 'actual_window_dates': window_dates})
    sources = set(expected.get('sources', []))
    if sources:
        actual = set(research.get('sources', [])) if plan_only else set(queried)
        check('requested_sources_planned' if plan_only else 'requested_sources_queried', sources <= actual,
              sorted(sources), sorted(actual))
    bounds = audit.get('time_scope') or _query_bounds(items)
    for key in ('date_start', 'date_end'):
        if expected.get(key):
            actual = task.get(key)
            anchor = research.get('anchor_start' if key == 'date_start' else 'anchor_end')
            effective = bounds.get('start' if key == 'date_start' else 'end')
            check(key + '_parsed', expected[key] in (actual, anchor, effective), expected[key],
                  {'task': actual, 'anchor': anchor, 'effective_query_scope': effective})
    if not plan_only:
        check('narrative_nonempty', bool(summary.strip()), 'a nonempty final answer', len(summary))
        if 'narrative_status' in audit:
            check('narrative_completed', audit['narrative_status'] == 'completed', 'completed', audit['narrative_status'])
        if desired_mode in ('analysis', 'timeseries') and sources:
            check('science_pixels_analyzed', pixels, True, pixels)
            check('requested_sources_analyzed', sources <= set(analyzed_sources), sorted(sources), analyzed_sources)
        if desired_mode == 'timeseries':
            check('temporal_multiple_actual_dates', len(window_dates) >= 2, 'at least two actual window acquisition dates',
                  {'dates': window_dates, 'first': min(window_dates) if window_dates else None,
                   'last': max(window_dates) if window_dates else None})
        if desired_mode == 'availability':
            check('catalogue_response_has_actual_search', bool(queried), 'actual requested catalog query', queried)
            check('availability_skips_optical_images', not view.get('images'), 'no acquired image assets', len(view.get('images', [])))
            if 'model_calls' in audit:
                visual = [call for call in audit['model_calls'] if call.get('role') in
                          ('recognizer', 'science_analyst', 'rs', 'remote_sensing') or call.get('image_input_count', 0) > 0]
                check('availability_skips_visual_models', not visual, 'no recognition/science visual calls',
                      [{'role': call.get('role'), 'image_input_count': call.get('image_input_count')} for call in visual])
    flags = []
    if kinds - set(task.get('task_types', [])) and not plan_only:
        flags.append('Task labels differ from scenario labels; review equivalent user actions/data rather than scoring labels as identification failure.')
    # These are review flags, never an automated legal/material truth classifier.
    if re.search(r'(?<![未不无])已确认(?:为)?(?:危险废物|违法倾倒|污染超标)', summary):
        flags.append('Review affirmative legal/hazard assertion against source evidence.')
    if case.get('attachments'):
        flags.append('Workbook imagery/annotations are supplied priors: this is not blind identification accuracy.')
    metrics = {'sources_queried': queried, 'science_scene_count': sum(item.get('scene_count', 0) or 0 for item in items),
               'science_pixels_analyzed': pixels, 'sources_analyzed': analyzed_sources,
               'image_count': len(view.get('images', [])),
               'scientific_window_count': audit.get('scientific_window_count', _scientific_windows(items)),
               'dates': sorted(set(dates)), 'task_types': task.get('task_types', []),
               'research_mode': research.get('mode'), 'narrative_chars': len(summary),
               'equivalent_mode': equivalent_mode, 'actual_scientific_window_dates': window_dates,
               'model_call_count': audit.get('model_call_count'), 'replan_count': audit.get('replan_count'),
               'narrative_status': audit.get('narrative_status')}
    return {'checks': checks, 'passed': sum(row['passed'] for row in checks),
            'failed': sum(not row['passed'] for row in checks), 'metrics': metrics,
            'identification_accuracy': None, 'review_flags': flags,
            'interpretation': 'Functional checks only; suspected records are not independent field-confirmed ground truth.'}


class LivePortal:
    def __init__(self, url, folder):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('This acceptance runner only authenticates to the local portal.')
        self.url, self.db = url.rstrip('/'), PortalDB(folder)
        admins = [user for user in self.db.users() if user['role'] == 'admin' and user['active']]
        if len(admins) != 1:
            raise ValueError('Evaluation requires one unambiguous active local administrator.')
        self.user = admins[0]
        self.client = None
        self.token = None

    async def __aenter__(self):
        token, csrf = self.db.issue_session(self.user['id'])
        self.token = token
        self.client = httpx.AsyncClient(base_url=self.url, trust_env=False, timeout=90,
            cookies={COOKIE: token}, headers={'X-CSRF-Token': csrf, 'Origin': self.url})
        try:
            response = await self.client.get('/portal/api/session')
            response.raise_for_status()
            if (response.json().get('user') or {}).get('id') != self.user['id']:
                raise ValueError('The running portal does not use the selected local database.')
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *_):
        try:
            if self.token:
                self.db.logout(self.token)
        finally:
            if self.client:
                await self.client.aclose()

    async def json(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.is_error:
            detail = response.json().get('detail', 'Request failed') if response.headers.get('content-type', '').startswith('application/json') else 'Request failed'
            raise RuntimeError(f'Portal HTTP {response.status_code}: {str(detail)[:600]}')
        return response.json()

    async def attachments(self, case, bank):
        attachments = case.get('attachments') or []
        if not attachments:
            return [], []
        workbook_path = Path(bank['source_workbook'])
        wanted_sha = bank.get('source_sha256')
        if wanted_sha and hashlib.sha256(workbook_path.read_bytes()).hexdigest() != wanted_sha:
            raise ValueError('Source workbook changed since question-bank creation.')
        uploads = await self.json('GET', '/portal/api/uploads')
        sha = wanted_sha or hashlib.sha256(workbook_path.read_bytes()).hexdigest()
        document = next((item for item in uploads['items'] if item.get('kind') == 'document' and item.get('sha256') == sha), None)
        if document is None:
            with workbook_path.open('rb') as content:
                document = await self.json('POST', '/portal/api/uploads', files={'file': (workbook_path.name, content,
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        asset_ids = []
        for record_id in dict.fromkeys(item.get('source_record_id') for item in attachments):
            if not record_id:
                raise ValueError('Workbook attachment needs its exact source_record_id.')
            result = await self.json('POST', f'/portal/api/uploads/{document["id"]}/workbook/{record_id}/images')
            if result.get('errors'):
                raise ValueError('Workbook embedded image extraction was incomplete.')
            requested = [item for item in attachments if item.get('source_record_id') == record_id]
            # Extracted asset provenance is returned by the real product endpoint.
            for asset in result['assets']:
                provenance = asset.get('provenance') or {}
                if any((not item.get('archive_member') or provenance.get('archive_member') == item['archive_member']) and
                       (not item.get('anchor_cell') or provenance.get('anchor_cell') == item['anchor_cell']) for item in requested):
                    asset_ids.append(asset['id'])
        if not asset_ids:
            raise ValueError('No exact requested workbook images were returned.')
        return list(dict.fromkeys(asset_ids))[:8], [document['id']]


def result_base(case, level):
    return {key: case.get(key) for key in ('case_id', 'question_id', 'title', 'objective', 'source_record_ids', 'source_cells')} | {
        'execution_level': level, 'state': 'running', 'started_at': timestamp()}


def summarize(folder, selected):
    records = []
    for case in selected:
        path = folder / (case['case_id'] + '.json')
        if path.is_file():
            records.append(json.loads(path.read_text(encoding='utf-8')))
    summary = {'updated_at': timestamp(), 'selected': len(selected), 'recorded': len(records),
               'completed': sum(row.get('state') == 'completed' for row in records),
               'failed_or_timeout': sum(row.get('state') in ('failed', 'timeout') for row in records),
               'functional_checks_passed': sum((row.get('assessment') or {}).get('passed', 0) for row in records),
               'functional_checks_failed': sum((row.get('assessment') or {}).get('failed', 0) for row in records),
               'identification_accuracy': None,
               'results': [{key: row.get(key) for key in ('case_id', 'execution_level', 'state', 'task_id', 'run_id', 'assessment', 'error')} for row in records]}
    write_json(folder / 'summary.json', summary)
    return summary


async def run(args):
    load_dotenv(ROOT / '.env.wuyu', override=True)
    folder = args.output.resolve()
    bank_path = args.bank or folder / ('question-bank.json' if args.plan_only else 'live-cases.json')
    bank, cases = load_cases(bank_path)
    if args.plan_only and not args.case_id:
        cases = planner_suite(cases)
    selected = select_cases(cases, args.case_id, args.limit)
    target = folder / ('planner-results' if args.plan_only else 'live-results')
    target.mkdir(parents=True, exist_ok=True)
    portal_folder = args.portal_dir.resolve()
    session = LivePortal(args.portal_url, portal_folder)
    settings = PortalConfig(session.db, replace(Settings.from_env(), output_dir=portal_folder / 'engine')).settings()
    async with session:
        for case in selected:
            path = target / (case['case_id'] + '.json')
            previous = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            if path.is_file() and not args.resume:
                raise ValueError(f'Evaluation state already exists for {case["case_id"]}; use --resume or a new --output.')
            if args.resume and previous.get('state') == 'completed':
                continue
            record = previous if args.resume and previous.get('task_id') else result_base(case, 'plan_only' if args.plan_only else 'full_pipeline')
            try:
                parent_id = None
                if case.get('parent_case_id'):
                    parent_path = target / (case['parent_case_id'] + '.json')
                    parent = json.loads(parent_path.read_text(encoding='utf-8'))
                    if parent.get('state') != 'completed' or not parent.get('task_id'):
                        raise ValueError('A real completed parent task is required for follow-up evaluation.')
                    parent_id = parent['task_id']
                if args.plan_only:
                    assets, documents = await session.attachments(case, bank)
                    body = submission_body(case, assets=assets, documents=documents)
                    record['input'] = body
                    write_json(path, record)
                    plan = await plan_task(session.user, planner_task(body), settings)
                    view = {'status': 'succeeded', 'task': plan['task']}
                    record.update(plan=plan, planned_task=plan['task'], assessment=assess(case, view, plan_only=True), state='completed')
                else:
                    if not record.get('task_id'):
                        assets, documents = await session.attachments(case, bank)
                        body = submission_body(case, parent_task_id=parent_id, assets=assets, documents=documents)
                        record['input'] = body
                        write_json(path, record)
                        view = await session.json('POST', '/portal/api/tasks', json=body)
                        record.update(task_id=view['id'], state='running')
                        write_json(path, record)
                    started = time.monotonic()
                    last_status = None
                    while True:
                        view = await session.json('GET', '/portal/api/tasks/' + record['task_id'])
                        record.update(public_view=view, plan=view.get('plan'))
                        # The portal DB provides only the run ID here; no raw model logs/secrets are exported.
                        record['run_id'] = session.db.task(record['task_id'], session.user['id']).get('engine_run_id')
                        state = (view.get('status'), len(view.get('activity', [])))
                        if state != last_status:
                            print(json.dumps({'case_id': case['case_id'], 'task_id': record['task_id'], 'status': state[0], 'activity_count': state[1]}), flush=True)
                            last_status = state
                        write_json(path, record)
                        if view.get('status') in TERMINAL:
                            audit = audit_facts(session.db, portal_folder, record['task_id'], session.user['id'])
                            record.update(state='completed' if view['status'] == 'succeeded' else 'failed',
                                          audit=audit, assessment=assess(case, view, audit=audit))
                            if view.get('error'):
                                record['error'] = view['error']
                            break
                        if time.monotonic() - started > args.timeout:
                            record.update(state='timeout', error='Polling timeout; the product task may still be running. Resume polls the same task.')
                            break
                        await asyncio.sleep(args.poll_seconds)
            except Exception as error:
                # No auth/config values are serialized. Network errors only include the local request path.
                record.update(state='failed', error=f'{type(error).__name__}: {str(error)[:700]}')
            record['finished_at'] = timestamp()
            write_json(path, record)
            summarize(target, selected)
            print(json.dumps({'case_id': case['case_id'], 'state': record['state'],
                              'checks_failed': (record.get('assessment') or {}).get('failed'),
                              'error': record.get('error')}, ensure_ascii=False), flush=True)
    summary = summarize(target, selected)
    print(json.dumps({key: summary[key] for key in ('selected', 'completed', 'failed_or_timeout', 'functional_checks_passed', 'functional_checks_failed')}), flush=True)
    return 1 if summary['failed_or_timeout'] else 0


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-only', action='store_true', help='Call the real DSH planner; do not run the imagery pipeline.')
    parser.add_argument('--bank', type=Path)
    parser.add_argument('--output', type=Path, default=DEFAULT_FOLDER)
    parser.add_argument('--portal-dir', type=Path, default=ROOT / 'outputs/portal')
    parser.add_argument('--portal-url', default='http://127.0.0.1:3080')
    parser.add_argument('--case-id', action='append', help='May be repeated; parents are included in dependency order.')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--timeout', type=float, default=10800)
    parser.add_argument('--poll-seconds', type=float, default=10)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1 or args.timeout <= 0 or args.poll_seconds <= 0:
        parser.error('Limits and polling intervals must be positive.')
    return args


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run(arguments())))
