import type { BananaPanelKey } from './locales.ts';
export type MaterialUploadStage = 'access' | 'metadata' | 'project' | 'receive' | 'register' | 'result';
export interface MaterialUploadDiagnostics { status?: number; requestId?: string; stage?: MaterialUploadStage }
const stages = new Set<MaterialUploadStage>(['access', 'metadata', 'project', 'receive', 'register', 'result']);
/** One client owner for accepted public codes, status fallbacks, diagnostics and presentation keys. */
const errorKeys: Record<string, BananaPanelKey> = {
    RELAYOUT_SOURCE_INVALID: 'taskEntry.sourceInvalid', MATERIAL_UPLOAD_LIMIT: 'upload.error.fileLimit',
    MATERIAL_COUNT_LIMIT: 'upload.error.projectLimit', MATERIAL_BATCH_LIMIT: 'upload.error.batchLimit',
    MATERIAL_PROJECT_LIMIT: 'upload.error.projectCapacity', MATERIAL_DELETE_PENDING: 'upload.error.deletePending',
    MATERIAL_HASH_MISMATCH: 'upload.error.checksum',
    UPLOAD_ID_CONFLICT: 'upload.error.conflict', INVALID_UPLOAD_METADATA: 'upload.error.metadata',
    UPLOAD_LENGTH_MISMATCH: 'upload.error.length', UPLOAD_TYPE_UNSUPPORTED: 'upload.error.type',
    BINARY_FILE_REQUIRED: 'upload.error.format', PROJECT_CONTEXT_DENIED: 'upload.error.project',
    UNAUTHORIZED: 'upload.error.auth', UPLOAD_ORIGIN_DENIED: 'upload.error.access',
    UPLOAD_ACCESS_DENIED: 'upload.error.access', UPLOAD_TARGET_UNAVAILABLE: 'upload.error.target',
    UPLOAD_STATE_CONFLICT: 'upload.error.state', UPLOAD_REQUEST_TOO_LARGE: 'upload.error.tooLarge',
    UPLOAD_REQUEST_REJECTED: 'upload.error.rejected', UPLOAD_RATE_LIMITED: 'upload.error.rate',
    UPLOAD_NETWORK: 'upload.error.network', UPLOAD_TIMEOUT: 'upload.error.timeout', UPLOAD_ABORTED: 'upload.error.aborted',
    UPLOAD_SERVICE_UNAVAILABLE: 'upload.error.service', UPLOAD_RESULT_UNAVAILABLE: 'upload.error.result', UPLOAD_EMPTY: 'upload.error.empty',
};
const safeCodes = new Set(Object.keys(errorKeys));
export const materialUploadErrorKey = (code: string): BananaPanelKey => errorKeys[code] || 'upload.error.service';
const stageKeys: Record<MaterialUploadStage, BananaPanelKey> = { access: 'upload.error.stage.access', metadata: 'upload.error.stage.metadata', project: 'upload.error.stage.project', receive: 'upload.error.stage.receive', register: 'upload.error.stage.register', result: 'upload.error.stage.result' };
export const materialUploadStageKey = (stage: MaterialUploadStage): BananaPanelKey => stageKeys[stage];
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export class MaterialUploadError extends Error {
    readonly status?: number;
    readonly requestId?: string;
    readonly stage?: MaterialUploadStage;
    constructor(readonly code: string, details: MaterialUploadDiagnostics = {}) {
        super(code);
        if (Number.isInteger(details.status) && Number(details.status) >= 400 && Number(details.status) <= 599) this.status = details.status;
        if (typeof details.requestId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(details.requestId)) this.requestId = details.requestId;
        if (details.stage && stages.has(details.stage)) this.stage = details.stage;
    }
}
/** Status and allowlisted diagnostics survive JSON/HTML/proxy refusals; upstream text never does. */
export function materialUploadFailure(status: number, data?: unknown, headerRequestId?: string | null): MaterialUploadError {
    const error = record(record(data).error);
    const code = status >= 200 && status < 300 ? 'UPLOAD_RESULT_UNAVAILABLE' : typeof error.code === 'string' && safeCodes.has(error.code) && !(error.code === 'UPLOAD_SERVICE_UNAVAILABLE' && status >= 400 && status < 500) ? error.code : materialUploadHttpCodes[status] || (status >= 400 && status < 500 ? 'UPLOAD_REQUEST_REJECTED' : 'UPLOAD_SERVICE_UNAVAILABLE');
    return new MaterialUploadError(code, { status, requestId: typeof error.request_id === 'string' ? error.request_id : headerRequestId || undefined, stage: error.stage as MaterialUploadStage });
}
export const materialUploadHttpCodes: Readonly<Record<number, string>> = { 400: 'UPLOAD_REQUEST_REJECTED', 401: 'UNAUTHORIZED', 403: 'UPLOAD_ACCESS_DENIED', 404: 'UPLOAD_TARGET_UNAVAILABLE', 408: 'UPLOAD_TIMEOUT', 409: 'UPLOAD_STATE_CONFLICT', 413: 'UPLOAD_REQUEST_TOO_LARGE', 415: 'BINARY_FILE_REQUIRED', 422: 'UPLOAD_REQUEST_REJECTED', 429: 'UPLOAD_RATE_LIMITED' };
