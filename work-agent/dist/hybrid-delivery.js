const record = (v) => v && typeof v === "object" && !Array.isArray(v) ? v : {};
const numbers = (v) => Array.isArray(v) && v.every(n => typeof n === "number" && Number.isInteger(n) && n > 0 && n <= 80) && new Set(v).size === v.length ? [...v] : undefined;
export function hybridPageSummary(artifact) {
    const metadata = record(record(artifact).metadata), editable = numbers(metadata.editable_pages), images = numbers(metadata.image_only_pages);
    if (!editable || !images || !editable.length && !images.length || editable.some(n => images.includes(n)))
        return undefined;
    const editability = !editable.length ? "image_based" : !images.length ? "native_trace_objects" : "mixed_native_and_images";
    const label = !editable.length ? `PPTX（${images.length}页图片，不可编辑）` : !images.length ? `可编辑 PPTX（${editable.length}页）` : `混合 PPTX（${editable.length}页可编辑，${images.length}页图片）`;
    return { editable_pages: editable, image_only_pages: images, editability, label };
}
export function isCurrentHybrid(artifact, capabilities) {
    const item = record(artifact), meta = record(item.metadata), cap = record(capabilities);
    return (meta.format === "hybrid_pptx" || item.kind === "hybrid_pptx") && cap.hybrid_pptx === true &&
        typeof cap.hybrid_signature === "string" && !!cap.hybrid_signature && meta.hybrid_signature === cap.hybrid_signature;
}
export function currentHybrid(snapshot) {
    const data = record(snapshot), cap = record(data.export_capabilities), workflow = record(data.workflow);
    const count = Array.isArray(workflow.pages) ? workflow.pages.length : record(data.brief).page_count;
    const candidates = [data.hybrid_export, ...(Array.isArray(data.artifacts) ? [...data.artifacts].reverse() : [])];
    for (const candidate of candidates) {
        const item = record(candidate), summary = hybridPageSummary(item), url = item.download_url || item.url;
        if (!isCurrentHybrid(item, cap) || !summary || typeof url !== "string" || !/^https?:\/\//.test(url))
            continue;
        const pageNumbers = [...summary.editable_pages, ...summary.image_only_pages];
        if (typeof count !== "number" || count < 1 || pageNumbers.length !== count || pageNumbers.some(n => n > count))
            continue;
        return { ...item, ...summary, download_url: url };
    }
    return undefined;
}
