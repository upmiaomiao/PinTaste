/**
 * Attach the host bridge to an iframe. Returns a bridge exposing disposal and
 * a refresh push; the panel re-renders on any pushed tool-result.
 */
export function attachBananaHost(options) {
    const { iframe, client } = options;
    const target = iframe.contentWindow;
    let ready = false;
    let preview = options.toolInput?.preview_target;
    let messageBusy = false;
    function post(message) {
        target?.postMessage(message, '*');
    }
    function notifyToolResult(result) {
        post({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result });
    }
    function respond(id, result) {
        if (id === undefined)
            return;
        post({ jsonrpc: '2.0', id, result });
    }
    async function handle(message) {
        const { id, method, params } = message;
        switch (method) {
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
                    hostContext: { displayMode: 'inline', platform: 'web', theme: 'light' },
                });
                return;
            }
            case 'tools/call': {
                const name = params?.name;
                const args = params?.arguments;
                if (typeof name !== 'string' || !['get_presentation_workflow', 'list_presentation_templates', 'get_presentation_template', 'read_presentation_material'].includes(name)) {
                    respond(id, { isError: true, content: [{ type: 'text', text: 'tools/call: missing tool name' }] });
                    return;
                }
                try {
                    respond(id, await client.callTool(name, (args ?? {})));
                }
                catch {
                    respond(id, { isError: true, content: [{ type: 'text', text: '连接暂不可用，请保留当前内容并刷新详情。' }] });
                }
                return;
            }
            case 'resources/read': {
                const uri = params?.uri;
                if (typeof uri !== 'string') {
                    respond(id, { contents: [] });
                    return;
                }
                const text = await client.readResource(uri);
                respond(id, { contents: [{ uri, mimeType: 'text/html', text }] });
                return;
            }
            case 'tools/list': {
                respond(id, { tools: await client.listTools() });
                return;
            }
            case 'resources/list':
                respond(id, { resources: [] });
                return;
            case 'ping':
                respond(id, {});
                return;
            case 'ui/open-link': {
                const url = params?.url;
                if (typeof url === 'string' && /^https?:\/\//i.test(url)) {
                    if (options.onOpenLink)
                        options.onOpenLink(url);
                    else
                        window.open(url, '_blank', 'noopener');
                }
                respond(id, {});
                return;
            }
            case 'ui/download-file':
                // The panel downloads exports through plain <a target=_blank> links.
                respond(id, {});
                return;
            case 'ui/message': {
                const content = params?.content;
                const text = Array.isArray(content) ? content.filter(x => x && typeof x === 'object' && x.type === 'text' && typeof x.text === 'string').map(x => x.text).join('\n') : '';
                if (!options.onMessage || params?.role !== 'user' || !text.trim() || text.length > 12000 || messageBusy) {
                    respond(id, { isError: true });
                    return;
                }
                messageBusy = true;
                try {
                    await options.onMessage(text);
                    respond(id, { isError: false, delivery: 'draft' });
                }
                catch {
                    respond(id, { isError: true });
                }
                finally {
                    messageBusy = false;
                }
                return;
            }
            case 'ui/notifications/initialized': {
                ready = true;
                // Panel is ready: deliver the project handle, then the full project.
                if (options.toolInput) {
                    post({ jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: { arguments: { ...options.toolInput, preview_target: preview } } });
                    const handle = options.toolInput.project_handle;
                    if (typeof handle === 'string') {
                        const result = await client.callTool('get_presentation_workflow', { project_handle: handle });
                        post({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result });
                    }
                }
                return;
            }
            default:
                // Unknown requests still get a minimal result; notifications are ignored.
                if (id !== undefined)
                    respond(id, {});
        }
    }
    function onMessage(event) {
        if (event.source !== target)
            return;
        const data = event.data;
        if (data === null || typeof data !== 'object')
            return;
        void handle(data);
    }
    window.addEventListener('message', onMessage);
    return {
        dispose: () => window.removeEventListener('message', onMessage),
        notifyToolResult,
        notifyPreview: (value) => { preview = value; if (ready)
            post({ jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: { arguments: { ...options.toolInput, preview_target: preview } } }); },
    };
}
//# sourceMappingURL=banana-host.js.map