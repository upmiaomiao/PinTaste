/** Project only authenticated, backend-approved template preview routes. */
import { BananaApiError } from './api-client.js';
const object = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
const safeKeys = ['id', 'title', 'source_type', 'review_status', 'neutralized', 'preview_kind', 'preview_disclaimer',
    'reference_scope', 'tags', 'page_count', 'body_reference_count', 'reference_sufficient', 'warnings', 'tagging_status', 'tag_source'];
/** Project backend-reviewed cards and cookie-authenticated, same-origin previews. */
export function presentNeutralTemplates(value, frontendUrl) {
    const origin = new URL(frontendUrl);
    if (!['http:', 'https:'].includes(origin.protocol) || origin.username || origin.password || origin.search || origin.hash)
        throw new BananaApiError('Invalid public template URL', 503);
    const preview = (path) => {
        if (path === null)
            return null;
        if (typeof path !== 'string' || !/^\/api\/template-library\/[A-Za-z0-9_.:-]+\/previews\/[1-9][0-9]*\.png$/.test(path))
            throw new BananaApiError('Neutral template preview is unavailable', 503);
        return origin.origin + '/agent-api' + path;
    };
    const card = (raw) => {
        const original = object(raw) && raw.preview_kind === 'original_template_render' && raw.neutralized === false && raw.source_type === 'local' && raw.review_status === 'approved';
        const personal = object(raw) && raw.preview_kind === 'private_generated_design' && raw.neutralized === false && raw.source_type === 'user' && raw.review_status === 'approved' && typeof raw.id === 'string' && /^personal:[0-9a-f-]{36}$/.test(raw.id);
        const sanitized = object(raw) && raw.preview_kind === 'sanitized_original_render' && raw.neutralized === true && raw.source_type === 'local' && raw.review_status === 'approved';
        const external = object(raw) && raw.preview_kind === 'external_template_render' && raw.neutralized === false && ['website', 'wechat'].includes(String(raw.source_type)) && raw.review_status === 'approved';
        if (!object(raw) || !original && !personal && !sanitized && !external && (raw.neutralized !== true || !['neutral_layout_redraw', 'neutral_native_redraw'].includes(String(raw.preview_kind)))
            || typeof raw.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(raw.id))
            throw new BananaApiError('Template preview is not approved for this view', 503);
        const out = Object.fromEntries(safeKeys.filter(k => raw[k] !== undefined).map(k => [k, raw[k]]));
        out.cover_url = preview(raw.cover_url ?? null);
        if (Array.isArray(raw.pages))
            out.pages = raw.pages.slice(0, 12).map(page => {
                if (!object(page))
                    throw new BananaApiError('Neutral template preview is unavailable', 503);
                return Object.fromEntries(['id', 'page_number', 'title', 'layout_family'].map(k => [k, page[k]]).concat([['image_url', preview(page.image_url)]]));
            });
        return out;
    };
    if (!object(value))
        throw new BananaApiError('Neutral template response is unavailable', 503);
    if (Array.isArray(value.items))
        return { items: value.items.map(card), total: value.total, offset: value.offset, limit: value.limit, facets: value.facets,
            preview_disclaimer: value.preview_disclaimer, selection_instruction: 'Offer these reviewed template designs with their actual preview_kind and disclaimer. A choice only selects style_anchor; do not start image generation until the user sends an actual generation request. Original template text and images are design references, not project evidence.' };
    return card(value);
}
