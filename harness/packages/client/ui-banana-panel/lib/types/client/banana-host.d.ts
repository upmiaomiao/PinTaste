/**
 * Host-side MCP Apps bridge for the Banana Slides panel. The panel runs in a
 * sandboxed iframe and speaks JSON-RPC over postMessage (via the ext-apps App
 * class); this bridge answers the ui/initialize handshake, proxies tools/call
 * and resources/read to the banana MCP server, and delivers the initial
 * tool-input/tool-result so the panel renders the project.
 */
import type { BananaClient } from './banana-mcp.ts';
interface BananaHostOptions {
    iframe: HTMLIFrameElement;
    client: BananaClient;
    /** Initial tool arguments (project_handle) delivered as tool-input once ready. */
    toolInput?: Record<string, unknown>;
    /** External-link open policy; defaults to opening a new tab. */
    onOpenLink?: (url: string) => void;
    /** User-initiated panel text goes to the existing conversation draft. */
    onMessage?: (text: string) => Promise<void>;
}
/** Live bridge to an attached panel: dispose the listener or push a refresh. */
export interface BananaHostBridge {
    /** Remove the message listener. */
    dispose(): void;
    /** Push a fresh tool-result so the panel re-renders the current project. */
    notifyToolResult(result: unknown): void;
    /** Navigate inside the already mounted iframe without replacing its project. */
    notifyPreview(target: Record<string, unknown>): void;
}
/**
 * Attach the host bridge to an iframe. Returns a bridge exposing disposal and
 * a refresh push; the panel re-renders on any pushed tool-result.
 */
export declare function attachBananaHost(options: BananaHostOptions): BananaHostBridge;
export {};
//# sourceMappingURL=banana-host.d.ts.map