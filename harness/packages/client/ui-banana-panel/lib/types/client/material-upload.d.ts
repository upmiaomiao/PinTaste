/** Private attachment context stays in the authenticated host request body. */
import type { BananaClient } from './banana-mcp.ts';
export type MaterialRole = 'material' | 'template' | 'logo';
/** Picker restrictions match each user-selected attachment purpose. */
export declare const MATERIAL_ACCEPT: Record<MaterialRole, string>;
/** Use the selected purpose on the actual existing upload tool call. */
export declare function uploadSelectedMaterial(client: Pick<BananaClient, 'callTool'>, projectHandle: string, file: Pick<File, 'name' | 'type'>, dataBase64: string, role: MaterialRole): Promise<import("./banana-mcp.ts").BananaCallResult>;
export interface MaterialBinding {
    projectHandle: string;
    materialIds: string[];
    requestId: string;
}
export interface MaterialNotice {
    success: true;
    accepted: true;
    summary: string;
    material_count: number;
    duplicate: boolean;
}
/** Read only canonical material ids, never OCR or source instructions. */
export declare function materialIds(value: unknown): string[];
/** Idempotent queue admission; the caller reuses this binding after a lost response. */
export declare function bindMaterialContext(sessionId: string, binding: MaterialBinding, request?: typeof fetch): Promise<MaterialNotice>;
//# sourceMappingURL=material-upload.d.ts.map