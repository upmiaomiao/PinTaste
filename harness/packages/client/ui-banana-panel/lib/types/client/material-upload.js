/** Picker restrictions match each user-selected attachment purpose. */
export const MATERIAL_ACCEPT = {
    material: '.pdf,.pptx,.docx,.xlsx,.csv,.tsv,.txt,.md,.png,.jpg,.jpeg,.webp',
    template: '.pdf,.pptx,.png,.jpg,.jpeg,.webp',
    logo: '.png,.jpg,.jpeg,.webp',
};
/** Use the selected purpose on the actual existing upload tool call. */
export function uploadSelectedMaterial(client, projectHandle, file, dataBase64, role) {
    return client.callTool('upload_presentation_material', { project_handle: projectHandle, filename: file.name, mime_type: file.type || 'application/octet-stream', data_base64: dataBase64, role });
}
/** Read only canonical material ids, never OCR or source instructions. */
export function materialIds(value) {
    const rows = value?.materials;
    if (!Array.isArray(rows))
        return [];
    return [...new Set(rows.flatMap(row => { const id = row?.id; return typeof id === 'string' && id.length > 0 ? [id] : []; }))];
}
/** Idempotent queue admission; the caller reuses this binding after a lost response. */
export async function bindMaterialContext(sessionId, binding, request = fetch) {
    const response = await request('/api/wuyu/material-context', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId, project_handle: binding.projectHandle, material_ids: binding.materialIds, request_id: binding.requestId }) });
    if (!response.ok)
        throw new Error('material-context-unavailable');
    const notice = await response.json();
    if (notice.success !== true || notice.accepted !== true || typeof notice.summary !== 'string' || typeof notice.material_count !== 'number')
        throw new Error('material-context-not-accepted');
    return notice;
}
//# sourceMappingURL=material-upload.js.map