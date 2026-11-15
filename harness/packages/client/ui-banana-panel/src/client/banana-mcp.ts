/**
 * Minimal browser-side MCP Streamable HTTP client for the Banana Slides server.
 * The panel only needs tools/call, resources/read, and tools/list, so this
 * client speaks just enough of the protocol instead of bundling the full SDK.
 */

/** Extract the last JSON-RPC message from an SSE or JSON response body. */
function parseResponse(text: string): unknown {
  const dataLines = text
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
  if (dataLines.length > 0) {
    for (const line of dataLines.reverse()) {
      if (line && line !== '[DONE]') { try { return JSON.parse(line) } catch { /* Ignore SSE keepalives. */ } }
    }
    return null
  }
  try { return JSON.parse(text) } catch { return null }
}

/** A single JSON-RPC response envelope (result only; errors are thrown). */
interface JsonRpcResponse {
  result?: unknown
  error?: { code?: number; message?: string }
}

/** Banana tool result fields the panel consumes. */
export interface BananaCallResult {
  content?: readonly { type: string; text?: string }[]
  structuredContent?: unknown
  _meta?: Record<string, unknown>
  isError?: boolean
}

/** A connected banana MCP client, scoped to one Streamable HTTP session. */
export interface BananaClient {
  /** Call a banana tool and return its result (content + structuredContent). */
  callTool(name: string, args: Record<string, unknown>): Promise<BananaCallResult>
  /** Read a resource's text, e.g. the project panel HTML. */
  readResource(uri: string): Promise<string>
  /** List server tools. */
  listTools(): Promise<unknown[]>
  /** Mark the session dead; later calls reject. */
  dispose(): void
}

/**
 * Open a banana MCP session.
 * @param url - Streamable HTTP endpoint, e.g. https://banana.wuyullm.com/mcp.
 * @param token - Bearer token for the endpoint.
 */
export async function connectBananaClient(url: string, token: string): Promise<BananaClient> {
  let sessionId: string | undefined
  let nextId = 0
  let disposed = false
  const lifetime = new AbortController()

  async function rpc(method: string, params?: unknown, notification = false): Promise<unknown> {
    if (disposed) throw new Error('banana MCP client disposed')
    const id = notification ? undefined : ++nextId
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json, text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(sessionId !== undefined ? { 'Mcp-Session-Id': sessionId } : {}),
      },
      body: JSON.stringify(
        notification
          ? { jsonrpc: '2.0', method, params }
          : { jsonrpc: '2.0', id, method, params },
      ),
      signal: AbortSignal.any([lifetime.signal, AbortSignal.timeout(120000)]),
      credentials: 'same-origin',
      redirect: 'error',
    })
    const nextSessionId = response.headers.get('mcp-session-id')
    if (nextSessionId !== null) sessionId = nextSessionId
    const text = await response.text()
    if (!response.ok) throw new Error(`banana MCP ${method}: HTTP ${response.status}`)
    const message = parseResponse(text) as JsonRpcResponse | null
    if (!notification && (message === null || message.error !== undefined || message.result === undefined)) {
      throw new Error(`banana MCP ${method}: invalid or failed RPC response`)
    }
    return message
  }

  await rpc('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'dsh-banana-panel', version: '0.1.0' },
  })
  await rpc('notifications/initialized', {}, true)

  return {
    async callTool(name, args) {
      const message = (await rpc('tools/call', { name, arguments: args })) as JsonRpcResponse | null
      return (message?.result ?? {}) as BananaCallResult
    },
    async readResource(uri) {
      const message = (await rpc('resources/read', { uri })) as { result?: { contents?: readonly { text?: string }[] } } | null
      return (message?.result?.contents ?? []).map((item) => item.text ?? '').join('')
    },
    async listTools() {
      const message = (await rpc('tools/list', {})) as { result?: { tools?: unknown[] } } | null
      return message?.result?.tools ?? []
    },
    dispose() { disposed = true; lifetime.abort() },
  }
}
