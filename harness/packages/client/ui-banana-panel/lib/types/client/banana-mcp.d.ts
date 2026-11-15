/**
 * Minimal browser-side MCP Streamable HTTP client for the Banana Slides server.
 * The panel only needs tools/call, resources/read, and tools/list, so this
 * client speaks just enough of the protocol instead of bundling the full SDK.
 */
/** Banana tool result fields the panel consumes. */
export interface BananaCallResult {
    content?: readonly {
        type: string;
        text?: string;
    }[];
    structuredContent?: unknown;
    isError?: boolean;
}
/** A connected banana MCP client, scoped to one Streamable HTTP session. */
export interface BananaClient {
    /** Call a banana tool and return its result (content + structuredContent). */
    callTool(name: string, args: Record<string, unknown>): Promise<BananaCallResult>;
    /** Read a resource's text, e.g. the project panel HTML. */
    readResource(uri: string): Promise<string>;
    /** List server tools. */
    listTools(): Promise<unknown[]>;
    /** Mark the session dead; later calls reject. */
    dispose(): void;
}
/**
 * Open a banana MCP session.
 * @param url - Streamable HTTP endpoint, e.g. https://banana.wuyullm.com/mcp.
 * @param token - Bearer token for the endpoint.
 */
export declare function connectBananaClient(url: string, token: string): Promise<BananaClient>;
//# sourceMappingURL=banana-mcp.d.ts.map