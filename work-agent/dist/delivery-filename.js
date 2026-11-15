/** A readable download name. Storage identifiers and signed URLs stay untouched. */
export function deliveryFilename(snapshot, format, existing, page) {
    const intake = (v) => typeof v === 'string' && /^(根据用户上传的资料规划|Plan a presentation from the user uploaded|先解析材料|请先上传)/i.test(v.trim());
    if (typeof existing === 'string' && !intake(existing) && !/^[a-f\d-]{24,}\./i.test(existing) && !/[\\/:*?"<>|\x00-\x1f]/.test(existing) && /\.(pptx|pdf|zip|md|json|png)$/i.test(existing) && existing.length <= 100)
        return existing;
    const value = typeof snapshot === 'string' ? snapshot : [snapshot.brief?.title, snapshot.brief?.topic,
        snapshot.title, snapshot.outline?.[0]?.title, snapshot.outline?.pages?.[0]?.title, ...(snapshot.materials || []).filter((m) => m.role === 'material').map((m) => m.filename?.replace(/\.[^.]+$/, ''))].find(v => typeof v === 'string' && v.trim() && !intake(v));
    const clean = (v) => typeof v === 'string' ? v.replace(/[\\/:*?"<>|\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').trim().replace(/[. ]+$/g, '') : '';
    let title = clean(value).split(/[。\n（(]/, 1)[0]?.trim() || '';
    if (!title || /^(根据用户上传的资料规划|Plan a presentation from the user uploaded)/i.test(title)) {
        const filename = clean(existing), old = filename.replace(/\.[a-z\d]+$/i, '');
        if (old && !intake(old) && !/^[a-f\d-]{24,}$/i.test(old) && /\.(pptx|pdf|zip|json|md|png)$/i.test(filename))
            return filename;
        title = '演示报告';
    }
    title = title.slice(0, 40).replace(/[. ]+$/g, '') || '演示报告';
    const suffix = { pptx: '图片版.pptx', image_pptx: '图片版.pptx', editable_pptx: '可编辑版.pptx',
        hybrid_pptx: '混合可编辑版.pptx', pdf: '报告.pdf', images_zip: '页面图片.zip', sources: '来源清单.json', sources_json: '来源清单.json',
        markdown: '阅读草稿.md', reading_evidence: '阅读证据.json', reading_report: '阅读核查.json', png: '页面.png' };
    if (format === 'png' && Number.isInteger(page) && Number(page) >= 1 && Number(page) <= 80)
        suffix.png = `第${page}页.png`;
    if (format === 'png' && typeof snapshot !== 'string' && snapshot.brief?.creation_kind === 'asset')
        suffix.png = '素材.png';
    return `${title}－${suffix[String(format)] || '文件'}`;
}
