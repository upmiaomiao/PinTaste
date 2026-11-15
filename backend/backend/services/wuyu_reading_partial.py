"""Explicit source omissions and verified-subset plans; never a coverage waiver."""
from copy import deepcopy


# Fail closed: a new error class is not permission to bypass the reading gate.
SOFT_SOURCE_CODES = {
    'parse_failed', 'parse_pending', 'no_extractable_text', 'unsupported_material',
    'material_incomplete', 'material_timeout', 'material_limit', 'invalid_pdf',
    'invalid_office', 'invalid_csv', 'invalid_encoding', 'invalid_image',
    'encrypted_material', 'pdf_page_text_limit', 'pdf_native_extractor_unavailable',
    'visual_text_not_fully_read', 'source_images_incomplete',
    'source_image_unreadable', 'reading_visual_coverage_missing',
    'reading_visual_transport_limit', 'reading_coverage_missing',
    'output_truncated', 'invalid_reading', 'reader_not_completed', 'reader_failed',
    'reading_quote_mismatch', 'reading_numeric_mismatch', 'reading_source_id_mismatch',
    'source_assets_excluded_by_user',
}
# A rejected model reference is a reader-response error, never trusted evidence.
# File hashes, paths and cache integrity remain hard security failures.
SECURITY_CODES = {'material_hash_mismatch', 'source_asset_hash_mismatch', 'unsafe_path',
                  'unsafe_archive', 'archive_limit', 'invalid_source_asset',
                  'reading_cache_invalid'}
PLAN_CODES = {'reading_cached_plan_invalid', 'reading_synthesis_invalid', 'reading_synthesis_source_mismatch',
              'reading_synthesis_incomplete_statement', 'invalid_slide_spec'}


# A failed reader is not evidence that the user's sources are insufficient.
# Keep existing partial-plan consent rules when a valid subset is available.
READER_RESPONSE_CODES = {'invalid_reading', 'invalid_model_json', 'reading_quote_mismatch',
                         'reading_numeric_mismatch', 'reading_source_id_mismatch', 'output_truncated', 'reader_failed',
                         'reader_not_completed'}


def reading_failure_code(report, results):
    """Report real reader/source failures, not intentionally omitted material.

    Planning-only evidence shortages keep their existing review contract. The
    caller saves the incomplete draft before turning this attempt into failed.
    Public serialization still sanitizes the selected original error code.
    """
    issues = report.get('issues', [])
    for issue in issues:
        if issue.get('code') in SECURITY_CODES:
            return issue['code']
    for result in results:
        if result.get('status') != 'completed':
            return result.get('code') or 'reader_failed'
    for issue in issues:
        if issue.get('code') != 'source_assets_excluded_by_user' and issue.get('waived_by_user_exclusion') is not True:
            return issue.get('code') or 'reader_failed'
    return None


def filter_reading_assets(material, exclusions):
    """Filter a prepared working copy, after original file integrity validation.

    Refreshes and the persisted original record are deliberately left intact.
    Excluded pixels/captions cannot be used as reading or synthesis inputs.
    """
    current = deepcopy(material)
    omitted = []
    selected = []
    for asset in current.get('source_assets', []):
        exclusion = exclusions.get(asset.get('id'))
        if exclusion and exclusion.get('material_id') == current.get('id'):
            omitted.append({'source_asset_id': asset['id'], 'material_id': current['id'],
                            'reason': exclusion['reason']})
        else:
            selected.append(asset)
    current['source_assets'] = selected
    return current, omitted


def verified_subset(plan, results):
    """Only acknowledged source scopes/images may enter partial synthesis.

    In particular, synthesis's complete-original candidates must not silently
    reintroduce the unread tail of a long document or a broken image caption.
    """
    completed = [r for r in results if r.get('status') == 'completed']
    read = {s for row in completed for s in row['result']['read_segment_ids']}
    images = {s for row in completed for s in row['result'].get('read_image_ids', [])}
    subset = deepcopy(plan)
    subset['segments'] = [s for s in subset['segments'] if s['id'] in read]
    subset['source_assets'] = {i: a for i, a in subset.get('source_assets', {}).items() if i in images}
    subset['tasks'] = [t for t in subset['tasks'] if any(r['task_id'] == t['id'] for r in completed)]
    subset['materials'] = [{**m, 'segment_ids': [s for s in m['segment_ids'] if s in read], 'status': 'prepared'}
                           for m in subset['materials'] if set(m['segment_ids']) & read]
    subset['issues'] = []
    return subset


def apply_omission_audit(report, plan, results):
    """Keep original coverage plus exact omitted IDs; all counters stay honest."""
    report = deepcopy(report)
    failure_by_task = {r['task_id']: r for r in results if r.get('status') != 'completed'}
    for issue in report['issues']:
        failure = failure_by_task.get(issue.get('task_id'), {})
        for key in ('source_asset_id', 'material_id'):
            if failure.get(key):issue[key] = failure[key]
    omitted = deepcopy(plan.get('omitted_source_assets', []))
    if omitted:
        report['issues'].append({'code': 'source_assets_excluded_by_user',
                                'source_asset_ids': [a['source_asset_id'] for a in omitted]})
        report['coverage_complete'] = False
        report['all_uploaded_materials_read'] = False
    selected_complete = (report['completed_segments'] == report['total_segments']
                         and report['total_segments'] > 0
                         and not [i for i in report['issues'] if i['code'] != 'source_assets_excluded_by_user'
                                  and i.get('waived_by_user_exclusion') is not True])
    visual = report.setdefault('visual_coverage', {})
    visual['selected_images'] = visual.get('available_images', 0)
    visual['available_images'] = visual['selected_images'] + len(omitted)
    visual['omitted_images'] = len(omitted)
    visual['omitted_source_asset_ids'] = [a['source_asset_id'] for a in omitted]
    report['omitted_source_assets'] = omitted
    report['omitted_material_ids'] = sorted(m['material_id'] for m in report['materials']
        if m.get('status') != 'excluded_by_user' and m.get('read') is not True)
    report['selected_scope_complete'] = selected_complete
    report['acknowledged_source_asset_ids'] = sorted({i for row in results if row.get('status') == 'completed'
                                                    for i in row['result'].get('read_image_ids', [])})
    return report


def continuation_options(value, body=None):
    issues = value.get('issues', [])
    codes = {str(i.get('code', 'unknown')).lower() for i in issues}
    if codes & SECURITY_CODES:category = 'security'
    elif any('quota' in c or c in {'reading_policy_budget', 'reading_retry_limit'} for c in codes):category = 'quota_or_policy'
    elif codes & READER_RESPONSE_CODES and value.get('content_plan_complete') is not True:category = 'runtime'
    elif codes & PLAN_CODES:category = 'content_plan_invalid'
    elif codes - SOFT_SOURCE_CODES:category = 'runtime'
    elif value.get('content_plan_complete') is not True:category = 'content_plan_invalid'
    elif not value.get('coverage_complete'):category = 'source_unread'
    else:category = 'none'
    count = value.get('evidence_validation', {}).get('accepted_findings', 0)
    valid_plan = (value.get('content_plan_complete') is True and not codes & PLAN_CODES
                  and bool(value.get('_slide_specs', value.get('slide_specs'))))
    reasons, material_ids, asset_ids, excludable = [], set(value.get('omitted_material_ids', [])), set(), set()
    for issue in issues:
        reason = {k: deepcopy(issue[k]) for k in ('code', 'material_id', 'source_asset_id') if k in issue}
        if isinstance(issue.get('segment_ids'), list):reason['affected_source_ranges'] = len(issue['segment_ids'])
        reasons.append(reason)
        if issue.get('material_id'):material_ids.add(issue['material_id'])
        if issue.get('source_asset_id'):
            asset_ids.add(issue['source_asset_id'])
            # The bad image itself may be explicitly omitted, but none of its
            # data is trusted. Whole-document integrity failures cannot use this.
            if issue.get('code') in {'source_image_unreadable', 'source_asset_hash_mismatch'}:
                excludable.add(issue['source_asset_id'])
        asset_ids.update(issue.get('source_asset_ids', []))
    for omitted in value.get('omitted_source_assets', []):
        asset_ids.add(omitted['source_asset_id'])
        reasons.append({'code': 'source_assets_excluded_by_user', **deepcopy(omitted)})
    if body:
        known = {a['id'] for m in body.get('materials', []) if m.get('role') in (None, '', 'material')
                 for a in m.get('source_assets', [])}
        excludable &= known
        material_ids.update(body.get('reading_exclusions', {}))
    return {'can_continue_with_omissions': value.get('status') in {'draft_incomplete', 'draft_ready'}
                and not value.get('coverage_complete') and category == 'source_unread'
                and valid_plan and type(count) is int and count > 0,
            'can_exclude_source_assets': bool(excludable)
                and not (codes - SOFT_SOURCE_CODES - {'source_asset_hash_mismatch'}),
            'content_plan_valid': valid_plan, 'failure_category': category,
            'omitted_material_ids': sorted(material_ids), 'source_asset_ids': sorted(asset_ids),
            'excludable_source_asset_ids': sorted(excludable),
            'omission_reasons': reasons[:30], 'verified_findings': count}


def accepted_reading(value, revision):
    if value.get('status') != 'accepted' or value.get('source_revision') != revision:
        return False
    # Recheck at dispatch as well as the original consent action: persisted or
    # upgraded states cannot carry a stale approval past a newly recorded hard
    # failure. Existing complete selected-scope approvals stay compatible.
    if {str(i.get('code','unknown')).lower() for i in value.get('issues', [])} - SOFT_SOURCE_CODES:
        return False
    if value.get('coverage_complete') is True:return True
    consent = value.get('partial_acceptance', {})
    return (value.get('accepted_with_omissions') is True
            and value.get('accepted_draft_revision') == value.get('draft_revision')
            and consent.get('draft_revision') == value.get('draft_revision')
            and consent.get('source_revision') == revision
            and consent.get('continue_requested') is True
            and consent.get('allow_incomplete') is True
            and continuation_options(dict(value, status='draft_incomplete'))['can_continue_with_omissions'])
