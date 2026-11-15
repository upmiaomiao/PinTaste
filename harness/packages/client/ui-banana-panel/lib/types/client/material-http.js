export class MaterialUploadError extends Error {
    code;
    constructor(code) {
        super(code);
        this.code = code;
    }
}
/** Preserve the deployment's public MCP prefix, replacing only its terminal endpoint. */
export function materialApiUrl(mcpUrl, suffix, base = location.href) {
    const url = new URL(mcpUrl, base);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !/\/mcp\/?$/.test(url.pathname))
        throw new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE');
    url.pathname = url.pathname.replace(/\/mcp\/?$/, '/materials/' + suffix);
    url.search = '';
    url.hash = '';
    return url.href;
}
/** Read the server's limits rather than assuming the old MCP/base64 ceiling. */
export async function readMaterialLimits(mcpUrl, token, request = fetch) {
    const response = await request(materialApiUrl(mcpUrl, 'limits'), { headers: { Authorization: `Bearer ${token}` }, credentials: 'same-origin', redirect: 'error' });
    if (!response.ok)
        throw new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE');
    const body = await response.json(), limits = body.limits;
    if (body.success !== true || !limits || !['max_file_bytes', 'max_project_files', 'max_batch_files', 'max_batch_bytes'].every(key => Number.isSafeInteger(limits[key]) && Number(limits[key]) > 0))
        throw new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE');
    return limits;
}
/** Upload progress reports actual XHR bytes. A retry reuses uploadId and the original File. */
export function uploadBinaryMaterial(mcpUrl, token, file, input, signal, progress, makeRequest = () => new XMLHttpRequest()) {
    return new Promise((resolve, reject) => {
        const xhr = makeRequest(), abort = () => xhr.abort(), finish = () => signal.removeEventListener('abort', abort);
        xhr.open('POST', materialApiUrl(mcpUrl, 'upload'));
        xhr.timeout = 180000;
        xhr.withCredentials = false;
        const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/octet-stream', 'X-Project-Handle': input.projectHandle, 'X-Material-Role': input.role, 'X-Filename': encodeURIComponent(file.name), 'X-File-Type': file.type || 'application/octet-stream', 'X-File-Size': String(file.size), 'X-Upload-Id': input.uploadId, 'X-Batch-Count': String(input.batchCount), 'X-Batch-Bytes': String(input.batchBytes) };
        for (const [key, value] of Object.entries(headers))
            xhr.setRequestHeader(key, value);
        xhr.upload.onprogress = event => {
            if (event.lengthComputable && event.total > 0)
                progress(Math.max(0, Math.min(100, Math.floor(event.loaded / event.total * 100))));
        };
        xhr.onerror = () => { finish(); reject(new MaterialUploadError('UPLOAD_NETWORK')); };
        xhr.ontimeout = () => { finish(); reject(new MaterialUploadError('UPLOAD_TIMEOUT')); };
        xhr.onabort = () => { finish(); reject(new MaterialUploadError('UPLOAD_ABORTED')); };
        xhr.onload = () => {
            finish();
            let data;
            try {
                data = JSON.parse(xhr.responseText);
            }
            catch {
                reject(new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE'));
                return;
            }
            if (!data || typeof data !== 'object' || Array.isArray(data)) {
                reject(new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE'));
                return;
            }
            const body = data;
            if (xhr.status < 200 || xhr.status >= 300 || body.success !== true || !Array.isArray(body.uploaded_material_ids) || !body.uploaded_material_ids.length || body.uploaded_material_ids.some(id => typeof id !== 'string')) {
                reject(new MaterialUploadError(typeof body.error?.code === 'string' ? body.error.code : 'UPLOAD_SERVICE_UNAVAILABLE'));
                return;
            }
            resolve(body);
        };
        signal.addEventListener('abort', abort, { once: true });
        if (signal.aborted) {
            finish();
            reject(new MaterialUploadError('UPLOAD_ABORTED'));
            return;
        }
        xhr.send(file);
    });
}
//# sourceMappingURL=material-http.js.map