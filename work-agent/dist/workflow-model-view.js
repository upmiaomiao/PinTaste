import { materialFailureView } from "./workflow-action-contract.js";
const row = (v) => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
const rows = (v) => Array.isArray(v) ? v.map(row) : [];
const pick = (v, keys) => Object.fromEntries(keys.filter(k => Object.hasOwn(v, k)).map(k => [k, v[k]]));
function researchReport(value) {
    const report = row(value);
    return { ...pick(report, ['summary', 'uncertainties', 'conflicts', 'suggested_sections', 'partial', 'fully_verified', 'completed_at']),
        findings: rows(report.findings).map(f => ({ ...pick(f, ['statement', 'verification']), evidence: rows(f.evidence).map(e => pick(e, ['source_id', 'url'])) })),
        sources: rows(report.sources).map(s => pick(s, ['id', 'url', 'title', 'content_scope', 'excerpt_truncated', 'provider', 'source_kind', 'doi', 'authors', 'date'])),
        full_evidence_in_details: true };
}
function pagePreview(value) {
    const copy = typeof value.copy === 'string' ? value.copy : typeof value.content === 'string' ? value.content : typeof value.copy_excerpt === 'string' ? value.copy_excerpt : '';
    const points = Array.isArray(value.points) ? value.points : Array.isArray(value.points_preview) ? value.points_preview : [];
    return { ...pick(value, ['number', 'title', 'kind', 'role', 'evidence_ids', 'source_asset_ids', 'data_ids']),
        ...(copy ? { copy_excerpt: copy.slice(0, 240), copy_complete: value.copy_complete === false ? false : copy.length <= 240 } : {}),
        points_preview: points.filter((p) => typeof p === 'string').slice(0, 3).map((p) => p.slice(0, 160)),
        evidence_count: typeof value.evidence_count === 'number' ? value.evidence_count : rows(value.evidence).length, source_image_count: typeof value.source_image_count === 'number' ? value.source_image_count : Array.isArray(value.source_asset_ids) ? value.source_asset_ids.length : rows(row(value.reading_plan).image_materials).length,
        layout: pick(row(value.layout), ['structure', 'intent']), speaking_script_available: value.speaking_script_available === true || typeof value.speaking_script === 'string',
        ...(typeof (value.layout_rationale || value.layout_rationale_excerpt) === 'string' ? { layout_rationale_excerpt: (value.layout_rationale || value.layout_rationale_excerpt).slice(0, 180) } : {}), full_content_in_details: true };
}
function queueView(value) {
    const queue = row(value);
    return { ...pick(queue, ['position', 'queued', 'running', 'workers']),
        ...(['memory_wait', 'worker_state_unknown', 'maintenance_hold'].includes(queue.block_reason) ? { block_reason: queue.block_reason } : {}) };
}
export function compactWorkflowSnapshot(snapshot) {
    const out = { ...snapshot };
    if (snapshot.relayout)
        out.relayout = pick(row(snapshot.relayout), ['status', 'filename', 'page_count', 'selected_pages', 'completed_pages', 'content_policy', 'aspect_ratio', 'error_code']);
    if (snapshot.research) {
        const research = row(snapshot.research), progress = row(research.progress);
        out.research = { ...pick(research, ['required', 'status', 'mode', 'profiles', 'consent_current', 'report_id', 'ready_for_planning', 'can_supplement', 'round', 'can_reuse_existing_report', 'reused_for_current_scope', 'materials_only_current', 'can_use_existing_materials']),
            progress: { ...pick(progress, ['stage', 'source_count', 'query', 'provider', 'model_calls', 'reserved_tokens', 'round_operation_start']),
                operations_offset: (Number(progress.operations_offset) || 0) + Math.max(0, rows(progress.operations).length - 20),
                operations: rows(progress.operations).slice(-20).map(op => pick(op, ['kind', 'status', 'provider', 'query', 'url', 'result_count', 'source_ids', 'source_titles', 'preview', 'error_code'])) },
            ...(research.report ? { report: researchReport(research.report) } : {}),
            ...(research.previous_report ? { previous_report: researchReport(research.previous_report) } : {}),
            feedback_history: rows(research.feedback_history).map(feedback => pick(feedback, ['round', 'prompt', 'confirmed_at'])),
            partial_source_count: rows(research.partial_sources).length || Number(research.partial_source_count) || 0, controls_location: 'conversation', view: 'research' };
    }
    const reading = row(snapshot.reading);
    if (snapshot.reading) {
        const { page_plan, slide_specs, _slide_specs, ...status } = reading;
        out.reading = { ...status, ...(Array.isArray(page_plan) ? { page_plan_total: page_plan.length, page_plan_summary: rows(page_plan).map(pagePreview) } : {}) };
    }
    if (Array.isArray(snapshot.slide_specs))
        out.slide_specs = rows(snapshot.slide_specs).map(pagePreview);
    if (snapshot.outline)
        out.outline = Array.isArray(snapshot.outline) ? rows(snapshot.outline).map(pagePreview) : { ...row(snapshot.outline), pages: rows(row(snapshot.outline).pages).map(pagePreview) };
    if (snapshot.workflow) {
        const workflow = row(snapshot.workflow);
        out.workflow = { ...workflow, pages: rows(workflow.pages).map(page => ({ ...pick(page, ['number', 'title', 'generation_status', 'image_version', 'confirmed_image_version', 'trace_version', 'input_revision', 'error_code', 'locked', 'lock_source']),
                ...(page.trace ? { trace: { ...pick(row(page.trace), ['source_image_version', 'status']), object_count: typeof row(page.trace).object_count === 'number' ? row(page.trace).object_count : rows(row(page.trace).editable_objects).length } } : {}) })) };
    }
    if (Array.isArray(snapshot.materials))
        out.materials = rows(snapshot.materials).map(material => ({ ...pick(material, ['id', 'filename', 'role', 'status', 'selected_for_content', 'exclusion_reason', 'text_chars', 'chunk_count', 'processing_limits', 'ocr_status', 'warnings', 'reading_notice', 'presentation']), ...materialFailureView(material),
            source_asset_count: typeof material.source_asset_count === "number" ? material.source_asset_count : rows(material.source_assets).length,
            source_assets: rows(material.source_assets).map(asset => ({ ...pick(asset, ['id', 'figure_label', 'source']), caption_excerpt: typeof (asset.caption || asset.caption_excerpt) === 'string' ? (asset.caption || asset.caption_excerpt).slice(0, 160) : '' })),
            ...(Array.isArray(material.template_images) ? { template_images: rows(material.template_images).map(image => pick(image, ['number', 'page_role'])) } : {}) }));
    if (Array.isArray(snapshot.artifacts))
        out.artifacts = rows(snapshot.artifacts).map(artifact => ({ ...pick(artifact, ['id', 'kind', 'filename', 'created_at', 'bytes']), metadata: pick(row(artifact.metadata), ['format', 'editability', 'editable_pages', 'image_only_pages', 'image_signature', 'hybrid_signature']) }));
    if (Array.isArray(snapshot.jobs))
        out.jobs = rows(snapshot.jobs).map(job => ({ ...pick(job, ['id', 'task_id', 'action', 'status', 'input_epoch', 'epoch', 'progress', 'error_code', 'error_message', 'retryable', 'delivery_status', 'created_at', 'updated_at']), ...(job.queue && typeof job.queue === 'object' ? { queue: queueView(job.queue) } : {}) }));
    out.detail_content = { tool: 'get_presentation_workflow', arguments: { draft_offset: 0, draft_limit: 2 }, instruction: 'Full current page wording is available on explicit draft_offset/draft_limit reads; the right panel already has all original figures, evidence and speaker notes. Previews are excerpts, never the complete source.' };
    if (snapshot.relayout) {
        // The panel retains the full snapshot in UI metadata. The conversation
        // reads selected wording through the existing bounded draft reader.
        out.slide_spec_count = snapshot.slide_spec_count ?? rows(snapshot.slide_specs).length;
        out.slide_specs = rows(out.slide_specs).slice(0, 4);
        if (Array.isArray(out.outline)) out.outline = out.outline.slice(0, 4);
        if (out.workflow) out.workflow = { ...out.workflow, page_count: out.brief?.page_count, pages: rows(out.workflow.pages).slice(0, 4) };
        out.materials = rows(out.materials).map(m => ({ ...m, source_assets: rows(m.source_assets).slice(0, 4),
            warnings: rows(m.warnings).slice(0, 4), warning_count: m.warning_count ?? rows(m.warnings).length,
            presentation: m.presentation ? pick(row(m.presentation), ['page_count', 'needs_visual_reading']) : undefined }));
        out.artifacts = rows(out.artifacts).slice(-4);
        out.jobs = rows(out.jobs).slice(0, 4);
        if (out.conversation) out.conversation = { ...out.conversation, previews: rows(out.conversation.previews).slice(0, 4) };
        out.model_summary_partial = true;
    }
    return out;
}
