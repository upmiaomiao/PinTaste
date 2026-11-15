/** Navigation currency contains a public project id, never a capability. */
export declare const PREVIEW_VIEWS: readonly ["brief", "materials", "outline", "references", "samples", "slides", "history", "trace", "export"];
export type PreviewView = typeof PREVIEW_VIEWS[number];
export interface PreviewTarget {
    view: PreviewView;
    page_number?: number;
    project_id?: string;
}
/** Validate the view/page boundary delivered from a link or a tool result. */
export declare function previewTarget(value: unknown): PreviewTarget | null;
/** Match only our same-origin work links; ordinary links retain normal behavior. */
export declare function previewTargetFromUrl(href: string, base: string): PreviewTarget | null;
/** Logged MCP text includes one compact JSON context beside human-readable copy. */
export declare function projectContext(texts: readonly string[], argsRaw?: string): {
    handle: string;
    projectId?: string;
    target?: PreviewTarget;
} | null;
//# sourceMappingURL=preview-target.d.ts.map