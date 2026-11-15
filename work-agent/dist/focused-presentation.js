const row = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
export function isFocusedCreation(brief) {
    return ['pages', 'asset'].includes(String(row(brief).creation_kind));
}
export function focusedCreationLabel(brief) {
    return row(brief).creation_kind === 'asset' ? '素材' : '少量页面';
}
/** Only the backend's current-image gate can admit PNGs as delivery files. */
export function focusedImageDownloads(snapshot) {
    const state = row(snapshot), brief = row(state.brief), workflow = row(state.workflow);
    if (!isFocusedCreation(brief) || row(state.export_capabilities).image_pptx !== true || !Array.isArray(workflow.pages))
        return [];
    const count = brief.page_count;
    if (typeof count !== 'number' || count < 1 || count > 8 || workflow.pages.length !== count)
        return [];
    const result = [];
    for (const value of workflow.pages) {
        const page = row(value), artifact = row(page.artifact), preview = page.image_url || artifact.image_uri, url = page.image_download_url || preview;
        if (page.generation_status !== 'ready' || !page.image_version || typeof url !== 'string' || !/^https?:\/\//.test(url))
            return [];
        result.push({ format: 'png', url, preview_url: preview, number: page.number, image_version: page.image_version, editability: 'image_based',
            label: brief.creation_kind === 'asset' ? '素材图片（PNG）' : `第 ${page.number} 页图片（PNG）` });
    }
    return result;
}
