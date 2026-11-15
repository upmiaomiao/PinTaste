/**
 * Host-side MCP Apps bridge for the Banana Slides panel. The panel runs in a
 * sandboxed iframe and speaks JSON-RPC over postMessage (via the ext-apps App
 * class); this bridge answers the ui/initialize handshake, proxies tools/call
 * and resources/read to the banana MCP server, and delivers the initial
 * tool-input/tool-result so the panel renders the project.
 */
import type { CurrentRequirements } from '../presentation-config.ts'
import type { BananaClient } from './banana-mcp.ts'
import type { UiLanguage } from './ui-language-bridge.ts'
/** One JSON-RPC message arriving from the panel. */
interface JsonRpcRequest {
  jsonrpc?: string
  id?: number | string
  method?: string
  params?: Record<string, unknown>
}
interface BananaHostOptions {
  iframe: HTMLIFrameElement
  client: BananaClient
  /** Account UI locale, independent of presentation content language. */
  uiLanguage?: UiLanguage
  /** Current conversation requirements; execution results cannot overwrite them. */
  currentRequirements?: CurrentRequirements | null
  /** Initial tool arguments (project_handle) delivered as tool-input once ready. */
  toolInput?: Record<string, unknown>
  /** External-link open policy; defaults to opening a new tab. */
  onOpenLink?: (url: string) => void
  /** User-initiated panel text goes to the existing conversation draft. */
  onMessage?: (text: string) => Promise<void>
  /** First-party template-tab operations remain bound to this mounted project's handle. */
  onTemplates?: (handle: string, request: unknown) => Promise<unknown>
}
/** Live bridge to an attached panel: dispose the listener or push a refresh. */
export interface BananaHostBridge {
  /** Remove the message listener. */
  dispose(): void
  /** Push a fresh tool-result so the panel re-renders the current project. */
  notifyToolResult(result: unknown): void
  /** Update host context without navigating or reloading the mounted project. */
  notifyLocale(language: UiLanguage): void
  /** Refresh the current requirements without navigating the frame. */
  notifyRequirements(record: CurrentRequirements | null): void
  /** Navigate inside the already mounted iframe without replacing its project. */
  notifyPreview(target: Record<string, unknown>): void
}
/**
 * Attach the host bridge to an iframe. Returns a bridge exposing disposal and
 * a refresh push; the panel re-renders on any pushed tool-result.
 */
export function attachBananaHost(options: BananaHostOptions): BananaHostBridge {
  const { iframe, client } = options
  const target = iframe.contentWindow
  let ready = false
  let locale: UiLanguage = options.uiLanguage === 'en' ? 'en' : 'zh-CN'
  let preview = options.toolInput?.preview_target
  let messageBusy = false
  let requirements = options.currentRequirements ?? null
  function mountedRequirements(): CurrentRequirements | null {
    return requirements?.binding && requirements.binding.project_handle !== options.toolInput?.project_handle ? null : requirements
  }
  function notifyRequirements(record: CurrentRequirements | null): void {
    if (JSON.stringify(record) === JSON.stringify(requirements)) return
    requirements = record
    if (ready) post({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { locale, current_requirements: mountedRequirements() } })
  }
  function post(message: unknown): void {
    target?.postMessage(message, '*')
  }
  function notifyLocale(language: UiLanguage): void {
    if (language !== 'zh-CN' && language !== 'en') return
    locale = language
    if (ready) post({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { locale, current_requirements: mountedRequirements() } })
  }
  function notifyToolResult(result: unknown): void {
    post({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result })
  }
  function respond(id: number | string | undefined, result: unknown): void {
    if (id === undefined) return
    post({ jsonrpc: '2.0', id, result })
  }
  async function handle(message: JsonRpcRequest): Promise<void> {
    const { id, method, params } = message
    switch (method) {
      case 'wuyu/templates/request': {
        const handle = options.toolInput?.project_handle
        if (typeof handle !== 'string' || !options.onTemplates) { respond(id, { error: 'TEMPLATE_PANEL_UNAVAILABLE' }); return }
        try { respond(id, { data: await options.onTemplates(handle, params) }) }
        catch { respond(id, { error: 'TEMPLATE_PANEL_REQUEST_FAILED' }) }
        return
      }
      case 'ui/initialize': {
        respond(id, {
          protocolVersion: typeof params?.protocolVersion === 'string' ? params.protocolVersion : '2026-01-26',
          hostInfo: { name: 'dsh', version: '0.1.0' },
          hostCapabilities: {
            serverTools: { listChanged: false },
            serverResources: { listChanged: false },
            openLinks: {},
            downloadFile: {},
            message: {},
          },
          hostContext: { displayMode: 'inline', platform: 'web', theme: 'light', locale, current_requirements: mountedRequirements() },
        })
        return
      }
      case 'tools/call': {
        const name = params?.name
        const args = params?.arguments
        if (typeof name !== 'string' || !['get_presentation_workflow','list_presentation_templates','get_presentation_template','read_presentation_material','set_presentation_page_lock','open_presentation_editor'].includes(name) || (name === 'set_presentation_page_lock' && (args as Record<string, unknown> | undefined)?.source !== 'user_ui')) {
          respond(id, { isError: true, content: [{ type: 'text', text: 'tools/call: missing tool name' }] })
          return
        }
        try {
          respond(id, await client.callTool(name, (args ?? {}) as Record<string, unknown>))
        } catch {
          respond(id, { isError: true, content: [{ type: 'text', text: '连接暂不可用，请保留当前内容并刷新详情。' }] })
        }
        return
      }
      case 'resources/read': {
        const uri = params?.uri
        if (typeof uri !== 'string') {
          respond(id, { contents: [] })
          return
        }
        const text = await client.readResource(uri)
        respond(id, { contents: [{ uri, mimeType: 'text/html', text }] })
        return
      }
      case 'tools/list': {
        respond(id, { tools: await client.listTools() })
        return
      }
      case 'resources/list':
        respond(id, { resources: [] })
        return
      case 'ping':
        respond(id, {})
        return
      case 'ui/open-link': {
        const url = params?.url
        if (typeof url === 'string' && /^https?:\/\//i.test(url)) {
          if (options.onOpenLink) options.onOpenLink(url)
          else window.open(url, '_blank', 'noopener')
        }
        respond(id, {})
        return
      }
      case 'ui/download-file':
        // The panel downloads exports through plain <a target=_blank> links.
        respond(id, {})
        return
      case 'ui/message': {
        const content = params?.content
        const text = Array.isArray(content) ? content.filter(x => x && typeof x === 'object' && x.type === 'text' && typeof x.text === 'string').map(x => x.text as string).join('\n') : ''
        if (!options.onMessage || params?.role !== 'user' || !text.trim() || text.length > 12000 || messageBusy) { respond(id, {isError:true}); return }
        messageBusy = true
        try { await options.onMessage(text); respond(id, {isError:false, delivery:'draft'}) }
        catch { respond(id, {isError:true}) }
        finally { messageBusy=false }
        return
      }
      case 'ui/notifications/initialized': {
        ready = true
        notifyLocale(locale)
        // Panel is ready: deliver the project handle, then the full project.
        if (options.toolInput) {
          post({ jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: { arguments: {...options.toolInput,preview_target:preview} } })
          const handle = options.toolInput.project_handle
          if (typeof handle === 'string') {
            const result = await client.callTool('get_presentation_workflow', { project_handle: handle })
            post({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result })
          }
        }
        return
      }
      default:
        // Unknown requests still get a minimal result; notifications are ignored.
        if (id !== undefined) respond(id, {})
    }
  }
  function onMessage(event: MessageEvent): void {
    if (event.source !== target) return
    const data = event.data as JsonRpcRequest | null
    if (data === null || typeof data !== 'object') return
    void handle(data)
  }
  window.addEventListener('message', onMessage)
  return {
    dispose: () => window.removeEventListener('message', onMessage),
    notifyToolResult,
    notifyLocale,
    notifyRequirements,
    notifyPreview: (value) => { preview=value;if(ready)post({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{...options.toolInput,preview_target:preview}}}) },
  }
}
