/** Navigation currency contains a public project id, never a capability. */
export const PREVIEW_VIEWS = ['brief', 'materials', 'outline', 'references', 'samples', 'slides', 'history', 'trace', 'export'];
/** Validate the view/page boundary delivered from a link or a tool result. */
export function previewTarget(value) {
    if (!value || typeof value !== 'object')
        return null;
    const v = value;
    if (typeof v.view !== 'string' || !PREVIEW_VIEWS.includes(v.view))
        return null;
    const page = v.page_number;
    if (page !== undefined && (!Number.isInteger(page) || Number(page) < 1 || Number(page) > 80))
        return null;
    if (v.project_id !== undefined && (typeof v.project_id !== 'string' || !/^[\w-]{1,80}$/.test(v.project_id)))
        return null;
    return { view: v.view, ...(page !== undefined ? { page_number: Number(page) } : {}), ...(typeof v.project_id === 'string' && /^[\w-]{1,80}$/.test(v.project_id) ? { project_id: v.project_id } : {}) };
}
/** Match only our same-origin work links; ordinary links retain normal behavior. */
export function previewTargetFromUrl(href, base) {
    try {
        const u = new URL(href, base), current = new URL(base);
        if (u.origin !== current.origin || !['/', '/work', '/work/'].includes(u.pathname) || !u.hash.startsWith('#wuyu-ppt?'))
            return null;
        const q = new URLSearchParams(u.hash.slice('#wuyu-ppt?'.length));
        if (q.has('handle') || q.has('project_handle'))
            return null;
        return previewTarget({ view: q.get('view'), ...(q.has('page') ? { page_number: Number(q.get('page')) } : {}), ...(q.has('project') ? { project_id: q.get('project') } : {}) });
    }
    catch {
        return null;
    }
}
/** Logged MCP text includes one compact JSON context beside human-readable copy. */
export function projectContext(texts, argsRaw) {
    const candidates = [];
    for (const text of texts) {
        try {
            const d = JSON.parse(text);
            if (d && typeof d === 'object')
                candidates.push(d, ...[d.structuredContent, d.project].filter(x => x && typeof x === 'object'));
        }
        catch { /* Plain model-facing copy is not the machine context. */ }
    }
    if (argsRaw) {
        try {
            candidates.push(JSON.parse(argsRaw));
        }
        catch { /* Partial call arguments are not settled JSON. */ }
    }
    const context = candidates.find(d => typeof d.project_handle === 'string' && d.project_handle.length >= 20);
    if (!context) {
        const match = texts.join('\n').match(/Project handle:\s*([A-Za-z0-9_.-]{20,})/);
        return match ? { handle: match[1] } : null;
    }
    const target = previewTarget(context.preview_target || { view: context.view, page_number: context.page_number }) || candidates.filter(d => d.project_handle === context.project_handle).map(d => previewTarget(d.preview_target || { view: d.view, page_number: d.page_number })).find(Boolean);
    return { handle: context.project_handle, ...(typeof context.project_id === 'string' ? { projectId: context.project_id } : {}), ...(target ? { target } : {}) };
}
//# sourceMappingURL=preview-target.js.map