/** Private binary transport: file bodies never enter a model tool call. */
import type { MaterialRole } from './material-upload.ts';
export interface MaterialLimits {
    max_file_bytes: number;
    max_project_files: number;
    max_batch_files: number;
    max_batch_bytes: number;
}
export interface BinaryUploadResult {
    success: true;
    project_id: string;
    uploaded_material_ids: string[];
    received_bytes: number;
    duplicate: boolean;
}
import { MaterialUploadError, materialUploadFailure } from './material-upload-errors.ts';
export { MaterialUploadError, materialUploadFailure } from './material-upload-errors.ts';
export type { MaterialUploadDiagnostics, MaterialUploadStage } from './material-upload-errors.ts';
/** Preserve the deployment's public MCP prefix, replacing only its terminal endpoint. */
export function materialApiUrl(mcpUrl: string, suffix: 'limits' | 'upload', base = location.href): string {
    const url = new URL(mcpUrl, base);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !/\/mcp\/?$/.test(url.pathname))
        throw new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE');
    url.pathname = url.pathname.replace(/\/mcp\/?$/, '/materials/' + suffix);
    url.search = '';
    url.hash = '';
    return url.href;
}
/** Read the server's limits rather than assuming the old MCP/base64 ceiling. */
export async function readMaterialLimits(mcpUrl: string, token: string, request: typeof fetch = fetch): Promise<MaterialLimits> {
    const response = await request(materialApiUrl(mcpUrl, 'limits'), { headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: 'same-origin', redirect: 'error' });
    if (!response.ok)
        throw materialUploadFailure(response.status, undefined, response.headers.get('X-Wuyu-Upload-Request-Id'));
    const body = await response.json() as {
        success?: unknown;
        limits?: Partial<MaterialLimits>;
    }, limits = body.limits;
    if (body.success !== true || !limits || !['max_file_bytes', 'max_project_files', 'max_batch_files', 'max_batch_bytes'].every(key => Number.isSafeInteger(limits[key as keyof MaterialLimits]) && Number(limits[key as keyof MaterialLimits]) > 0))
        throw new MaterialUploadError('UPLOAD_SERVICE_UNAVAILABLE');
    return limits as MaterialLimits;
}
/** Upload progress reports actual XHR bytes. A retry reuses uploadId and the original File. */
export function uploadBinaryMaterial(mcpUrl: string, token: string, file: File, input: {
    projectHandle: string;
    role: MaterialRole;
    uploadId: string;
    batchCount: number;
    batchBytes: number;
}, signal: AbortSignal, progress: (percent: number) => void, makeRequest: () => XMLHttpRequest = () => new XMLHttpRequest()): Promise<BinaryUploadResult> {
    return new Promise((resolve, reject) => {
        const xhr = makeRequest(), abort = () => xhr.abort(), finish = () => signal.removeEventListener('abort', abort);
        xhr.open('POST', materialApiUrl(mcpUrl, 'upload'));
        xhr.timeout = 180000;
        xhr.withCredentials = false;
        const headers = { ...(token ? { Authorization: `Bearer ${token}` } : {}), 'Content-Type': 'application/octet-stream', 'X-Project-Handle': input.projectHandle, 'X-Material-Role': input.role, 'X-Filename': encodeURIComponent(file.name), 'X-File-Type': file.type || 'application/octet-stream', 'X-File-Size': String(file.size), 'X-Upload-Id': input.uploadId, 'X-Batch-Count': String(input.batchCount), 'X-Batch-Bytes': String(input.batchBytes) };
        for (const [key, value] of Object.entries(headers))
            xhr.setRequestHeader(key, value);
        xhr.upload.onprogress = event => { if (event.lengthComputable && event.total > 0)
            progress(Math.max(0, Math.min(100, Math.floor(event.loaded / event.total * 100)))); };
        xhr.onerror = () => { finish(); reject(new MaterialUploadError('UPLOAD_NETWORK')); };
        xhr.ontimeout = () => { finish(); reject(new MaterialUploadError('UPLOAD_TIMEOUT')); };
        xhr.onabort = () => { finish(); reject(new MaterialUploadError('UPLOAD_ABORTED')); };
        xhr.onload = () => {
            finish();
            let data: unknown;
            try {
                data = JSON.parse(xhr.responseText);
            }
            catch {
                reject(materialUploadFailure(xhr.status, undefined, xhr.getResponseHeader?.('X-Wuyu-Upload-Request-Id')));
                return;
            }
            if (!data || typeof data !== 'object' || Array.isArray(data)) {
                reject(materialUploadFailure(xhr.status, undefined, xhr.getResponseHeader?.('X-Wuyu-Upload-Request-Id')));
                return;
            }
            const body = data as Partial<BinaryUploadResult> & {
                error?: {
                    code?: unknown;
                };
            };
            if (xhr.status < 200 || xhr.status >= 300 || body.success !== true || !Array.isArray(body.uploaded_material_ids) || !body.uploaded_material_ids.length || body.uploaded_material_ids.some(id => typeof id !== 'string' || !id.trim())) {
                reject(materialUploadFailure(xhr.status, body, xhr.getResponseHeader?.('X-Wuyu-Upload-Request-Id')));
                return;
            }
            resolve(body as BinaryUploadResult);
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
