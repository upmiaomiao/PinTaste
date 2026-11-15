/**
 * Minimal browser-side MCP Streamable HTTP client for the Banana Slides server.
 * The panel only needs tools/call, resources/read, and tools/list, so this
 * client speaks just enough of the protocol instead of bundling the full SDK.
 */
/** Extract the last JSON-RPC message from an SSE or JSON response body. */
function parseResponse(text) {
    const dataLines = text
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim());
    if (dataLines.length > 0) {
        for (const line of dataLines.reverse()) {
            if (line && line !== '[DONE]') {
                try {
                    return JSON.parse(line);
                }
                catch { /* Ignore SSE keepalives. */ }
            }
        }
        return null;
    }
    try {
        return JSON.parse(text);
    }
    catch {
        return null;
    }
}
/**
 * Open a banana MCP session.
 * @param url - Streamable HTTP endpoint, e.g. https://banana.wuyullm.com/mcp.
 * @param token - Bearer token for the endpoint.
 */
export async function connectBananaClient(url, token) {
    let sessionId;
    let nextId = 0;
    let disposed = false;
    const lifetime = new AbortController();
    async function rpc(method, params, notification = false) {
        if (disposed)
            throw new Error('banana MCP client disposed');
        const id = notification ? undefined : ++nextId;
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Accept: 'application/json, text/event-stream',
                Authorization: `Bearer ${token}`,
                ...(sessionId !== undefined ? { 'Mcp-Session-Id': sessionId } : {}),
            },
            body: JSON.stringify(notification
                ? { jsonrpc: '2.0', method, params }
                : { jsonrpc: '2.0', id, method, params }),
            signal: AbortSignal.any([lifetime.signal, AbortSignal.timeout(120000)]),
            redirect: 'error',
        });
        const nextSessionId = response.headers.get('mcp-session-id');
        if (nextSessionId !== null)
            sessionId = nextSessionId;
        const text = await response.text();
        if (!response.ok)
            throw new Error(`banana MCP ${method}: HTTP ${response.status}`);
        const message = parseResponse(text);
        if (!notification && (message === null || message.error !== undefined || message.result === undefined)) {
            throw new Error(`banana MCP ${method}: invalid or failed RPC response`);
        }
        return message;
    }
    await rpc('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'dsh-banana-panel', version: '0.1.0' },
    });
    await rpc('notifications/initialized', {}, true);
    return {
        async callTool(name, args) {
            const message = (await rpc('tools/call', { name, arguments: args }));
            return (message?.result ?? {});
        },
        async readResource(uri) {
            const message = (await rpc('resources/read', { uri }));
            return (message?.result?.contents ?? []).map((item) => item.text ?? '').join('');
        },
        async listTools() {
            const message = (await rpc('tools/list', {}));
            return message?.result?.tools ?? [];
        },
        dispose() { disposed = true; lifetime.abort(); },
    };
}
//# sourceMappingURL=banana-mcp.js.map