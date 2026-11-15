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
export declare class MaterialUploadError extends Error {
    readonly code: string;
    constructor(code: string);
}
/** Preserve the deployment's public MCP prefix, replacing only its terminal endpoint. */
export declare function materialApiUrl(mcpUrl: string, suffix: 'limits' | 'upload', base?: string): string;
/** Read the server's limits rather than assuming the old MCP/base64 ceiling. */
export declare function readMaterialLimits(mcpUrl: string, token: string, request?: typeof fetch): Promise<MaterialLimits>;
/** Upload progress reports actual XHR bytes. A retry reuses uploadId and the original File. */
export declare function uploadBinaryMaterial(mcpUrl: string, token: string, file: File, input: {
    projectHandle: string;
    role: MaterialRole;
    uploadId: string;
    batchCount: number;
    batchBytes: number;
}, signal: AbortSignal, progress: (percent: number) => void, makeRequest?: () => XMLHttpRequest): Promise<BinaryUploadResult>;
//# sourceMappingURL=material-http.d.ts.map