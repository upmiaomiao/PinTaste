"""Regenerate only a saved run's report through the configured live main model.

Stop the managed portal first. The normal engine lock prevents concurrent use.
By default only the final report is regenerated. --retry-science-source may
retry an unfinished source's analysis once using its existing scientific
previews; it never searches or downloads imagery. Original evidence and
provider audits are preserved, including earlier failures.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from wuyu_harness.adaptive import image_parts
from wuyu_harness.business_narrative import REPORT_INSTRUCTION, final_summary, narrative_material
from wuyu_harness.config import Settings
from wuyu_harness.evidence import reject_unsupported_claims
from wuyu_harness.fusion import fuse_findings
from wuyu_harness.models import AUDIT_DIR
from wuyu_harness.portal import PortalRuntime
from wuyu_harness.presentation import evidence_presentation
from wuyu_harness.report import render_report
from wuyu_harness.science import analyze_science
from wuyu_harness.schemas import Recognition, ReportNarrative, TaskRequest
from wuyu_harness.store import now
from wuyu_harness.strategy import task_strategy


def refresh_saved_fusion(task, package, assets, folder):
    """Recompute screening from saved observations, preserving the original."""
    if package.get('fusion', {}).get('screening_applicable') is False or package.get('research_result'):
        raise ValueError('A scientific-only/research answer cannot be remapped to screening by this utility.')
    previous = copy.deepcopy(package['fusion'])
    fresh = fuse_findings(task, assets, package.get('image_assessments', []),
        package.get('strategy') or task_strategy(task),
        package.get('adaptive_review', {}).get('main_visual_review', {}))
    backup = folder / 'finding-package.before-fusion-refresh.json'
    if not backup.exists():
        backup.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
    package['fusion'] = fresh
    if package.get('status') in {'draft_pending_human_review', 'no_reviewable_finding', 'needs_more_data'}:
        package['status'] = ('needs_more_data' if fresh['level'] == 'insufficient' else
                            'no_reviewable_finding' if fresh['level'] == 'low' else 'draft_pending_human_review')
    package.setdefault('fusion_revisions', []).append({'at': now(), 'previous_fusion': previous,
        'new_fusion': copy.deepcopy(fresh), 'method': 'deterministic_refresh_from_saved_observations',
        'reacquired_evidence': False, 'recognition_replayed': False})
    ledger, presentation = evidence_presentation(assets, Recognition.model_validate(package['recognition']),
        package.get('image_assessments', []), package.get('scientific_evidence', []), fresh)
    package['evidence_summary'], package['evidence_presentation'] = ledger, presentation
    return fresh


async def retry_science_sources(pipeline, run_id, task, package, assets, sources):
    """One explicit retry per unfinished source, strictly from saved previews."""
    items = {item['source']: item for item in package.get('scientific_evidence', [])}
    by_id = {asset['id']: asset for asset in assets}
    plans = []
    # Validate all requested sources before making any paid call.
    for source in dict.fromkeys(sources):
        if source not in items:
            raise ValueError('Requested science source is absent from the saved package.')
        item = items[source]
        if (item.get('analysis') or {}).get('status') == 'completed':
            plans.append((source, item, [], []))
            continue
        windows = [window for window in (item.get('subsets') or [item.get('subset') or {}])
                   if window.get('preview_asset_id')]
        if not windows:
            raise ValueError('Requested source has no saved scientific preview.')
        selected = []
        for window in windows:
            asset = by_id.get(window['preview_asset_id'])
            if (not asset or asset.get('metadata', {}).get('source') != source
                    or asset.get('metadata', {}).get('modality') not in {'multispectral', 'sar', 'hyperspectral'}
                    or not pipeline.store.asset_path(asset).is_file()):
                raise ValueError('Saved scientific preview is missing or does not belong to the requested source.')
            if asset['id'] not in {row['id'] for row in selected}:
                selected.append(asset)
        plans.append((source, item, windows, selected))
    results = []
    folder = pipeline.store.run_dir(run_id)
    for source, item, windows, selected in plans:
        if not selected:
            results.append({'source': source, 'status': 'skipped_already_completed'})
            continue
        backup = folder / 'finding-package.before-science-retry.json'
        if not backup.exists():
            backup.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        previous_analysis = copy.deepcopy(item.get('analysis') or {})
        previous_source_status = item.get('status')
        audit_folder = folder / 'model-calls'
        audit_folder.mkdir(exist_ok=True)
        previous_audits = {path.name for path in audit_folder.glob('*-request.json')}
        traces = []
        token = AUDIT_DIR.set(audit_folder)
        try:
            analysis = await analyze_science(pipeline, task, selected,
                {'source': source, 'windows': windows, 'spatial_basis': item.get('spatial_basis')}, traces)
        finally:
            AUDIT_DIR.reset(token)
        audit_ids = {path.name.removesuffix('-request.json') for path in audit_folder.glob('*-request.json')
                     if path.name not in previous_audits}
        audit_ids.update(trace['audit_id'] for trace in traces if trace.get('audit_id'))
        for trace in traces:
            trace['purpose'] = 'source_science_retry_from_saved_evidence'
        package.setdefault('model_calls', []).extend(traces)
        item['analysis'] = analysis
        item['science_pixels_analyzed'] = analysis.get('science_pixels_analyzed') is True
        item['status'] = 'analyzed' if item['science_pixels_analyzed'] else 'downloaded_unanalyzed'
        package.setdefault('source_science_regenerations', []).append({
            'at': now(), 'source': source,
            'previous_status': previous_analysis.get('status'), 'new_status': analysis.get('status'),
            'previous_source_status': previous_source_status, 'new_source_status': item['status'],
            'previous_analysis': previous_analysis, 'new_analysis': copy.deepcopy(analysis),
            'model_audit_ids': sorted(audit_ids), 'reacquired_evidence': False})
        package.setdefault('adaptive_review', {})['science_analyses'] = [
            row['analysis'] for row in package.get('scientific_evidence', []) if isinstance(row.get('analysis'), dict)]
        ledger, presentation = evidence_presentation(assets, Recognition.model_validate(package['recognition']),
            package.get('image_assessments', []), package.get('scientific_evidence', []), package['fusion'])
        package['evidence_summary'], package['evidence_presentation'] = ledger, presentation
        # Persist the real retry even if the subsequent report request fails.
        (folder / 'finding-package.json').write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        results.append({'source': source, 'status': analysis.get('status'), 'model_audit_ids': sorted(audit_ids)})
    return results


async def regenerate(run_id: str, retry_science_source: list[str] | None = None, *, refresh_fusion: bool = False) -> None:
    load_dotenv(ROOT / '.env.wuyu', override=True)
    runtime = PortalRuntime(ROOT / 'outputs/portal', Settings.from_env())
    try:
        await runtime.start_engine()
        pipeline = runtime.service.pipeline
        store = runtime.service.store
        folder = store.run_dir(run_id)
        record = store.get(run_id, 'run')
        if record['status'] != 'succeeded':
            raise ValueError('Only an already completed run can regenerate its saved report.')
        package = json.loads((folder / 'finding-package.json').read_text(encoding='utf-8'))
        task = TaskRequest.model_validate(package['task'])
        assets = [store.get(row['id'], 'image') for row in package['assets']]
        if refresh_fusion:
            refresh_saved_fusion(task, package, assets, folder)
        science_retries = await retry_science_sources(pipeline, run_id, task, package, assets,
                                                     retry_science_source or [])
        documents = [store.get(identity, 'document') for identity in task.document_ids]
        policy = package.get('policy', {})
        payload = narrative_material(task, package['fusion'], assets,
            Recognition.model_validate(package['recognition']), package.get('native_rs_results', []),
            package.get('image_assessments', []), package.get('adaptive_review', {}),
            package.get('specialist_review', {}), package.get('scientific_evidence', []),
            policy, documents, selection=package.get('selection', {}))
        config = pipeline.settings.models['main']
        audit_token = AUDIT_DIR.set(folder / 'model-calls')
        try:
            summary, trace = await pipeline.models.complete(config, REPORT_INSTRUCTION, payload,
                ReportNarrative, image_parts(pipeline, assets) if 'image' in config.input_modalities else None)
        finally:
            AUDIT_DIR.reset(audit_token)
        if not set(summary.cited_policy_ids) <= {row['id'] for row in policy.get('sources', [])}:
            raise ValueError('The regenerated report cites a policy that was not retrieved.')
        reject_unsupported_claims(summary.model_dump_json())
        backup = folder / 'finding-package.before-report-regeneration.json'
        if not backup.exists():
            backup.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        previous_status = package.get('narrative_status')
        previous_warnings = list(package.get('warnings', []))
        package['narrative'] = summary.model_dump()
        package['narrative']['summary'] = final_summary(summary.summary, package['fusion']['judgment'])
        package['narrative_status'] = 'completed'
        # Remove only the old final-writer failure. Other acquisition and review
        # warnings remain, and the previous list is kept in the repair audit.
        if previous_status == 'failed':
            package['warnings'] = [warning for warning in previous_warnings if warning not in {
                'main: 输出不符合 JSON 结构，停止依赖该结果的分析',
                '主模型未完成报告综合；当前提供结构化证据模板',
                'main: 输出达到 token 上限，停止使用未完整生成的结果'}]
        trace['purpose'] = 'report_regeneration_from_saved_evidence'
        package.setdefault('model_calls', []).append(trace)
        package.setdefault('report_regenerations', []).append({
            'at': now(), 'previous_status': previous_status, 'previous_warnings': previous_warnings,
            'model_audit_id': trace['audit_id'], 'reacquired_evidence': False})
        for stage in package.get('workflow_stages', []):
            if stage['id'] == 'synthesis':
                stage['narrative_status'] = 'completed'
                stage['result_status'] = package['status']
        render_report(package, store, run_id)
        pipeline.event(run_id, 'report_synthesis', 'completed', {'regenerated_from_saved_evidence': True})
        record = store.get(run_id, 'run')
        record['updated_at'] = now()
        record['result_status'] = package['status']
        store.put('run', record)
        print(json.dumps({'run_id': run_id, 'narrative_status': package['narrative_status'],
            'summary_characters': len(summary.summary), 'max_tokens': config.max_tokens,
            'enable_thinking': config.enable_thinking, 'model': config.model,
            'image_count': trace.get('image_input_count'), 'usage': trace.get('usage'),
            'science_retries': science_retries,
            'reacquired_evidence': False}, ensure_ascii=False), flush=True)
    finally:
        await runtime.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--retry-science-source', action='append', default=[], metavar='SOURCE',
                        help='Retry unfinished scientific analysis once from saved previews; repeat for another source.')
    parser.add_argument('--refresh-fusion', action='store_true',
                        help='Recompute screening from the original saved observations before rewriting its report.')
    args = parser.parse_args()
    asyncio.run(regenerate(args.run_id, args.retry_science_source, refresh_fusion=args.refresh_fusion))
