import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * React wrapper for the Banana Slides project panel. Connects to the banana
 * MCP server from the browser, loads the official single-file panel HTML into
 * a sandboxed iframe, and attaches the postMessage host bridge.
 */
import { useEffect, useRef, useState } from 'react';
import { BANANA_MCP_TOKEN, BANANA_MCP_URL, PANEL_RESOURCE_URI } from "./config.js";
import { connectBananaClient } from "./banana-mcp.js";
import { attachBananaHost } from "./banana-host.js";
import css from './BananaPanelRow.module.css';
/** Host the banana panel for one project handle. */
export function BananaPanelHost({ projectHandle, target, targetRevision, writeToConversation, t }) {
    const [phase, setPhase] = useState('connecting');
    const [error, setError] = useState(null);
    const iframeRef = useRef(null);
    const bridgeRef = useRef(null), targetRef = useRef(target), messageRef = useRef(writeToConversation);
    targetRef.current = target;
    messageRef.current = writeToConversation;
    useEffect(() => { bridgeRef.current?.notifyPreview({ ...target }); }, [targetRevision, target.view, target.page_number]);
    useEffect(() => {
        let disposed = false;
        let client = null;
        let bridge = null;
        async function boot() {
            try {
                setPhase('connecting');
                client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN);
                if (disposed) {
                    client.dispose();
                    return;
                }
                setPhase('loading');
                const html = await client.readResource(PANEL_RESOURCE_URI);
                if (disposed) {
                    client.dispose();
                    return;
                }
                const iframe = iframeRef.current;
                if (iframe === null) {
                    client.dispose();
                    return;
                }
                iframe.srcdoc = html;
                bridge = attachBananaHost({
                    iframe,
                    client,
                    toolInput: { project_handle: projectHandle, preview_target: targetRef.current },
                    onMessage: (text) => messageRef.current(text),
                });
                bridgeRef.current = bridge;
                setPhase('ready');
            }
            catch (err) {
                if (!disposed) {
                    setPhase('error');
                    setError(t('host.safeError'));
                }
            }
        }
        void boot();
        return () => {
            disposed = true;
            bridge?.dispose();
            bridgeRef.current = null;
            client?.dispose();
        };
    }, [projectHandle]);
    return (_jsxs("div", { className: css.host, "data-phase": phase, children: [phase === 'connecting' || phase === 'loading' ? (_jsx("div", { className: css.loading, children: phase === 'connecting' ? t('host.connecting') : t('host.loading') })) : null, phase === 'error' ? (_jsxs("div", { className: css.error, children: [t('host.error'), error ?? ''] })) : null, _jsx("iframe", { ref: iframeRef, className: css.iframe, title: t('host.iframeTitle'), sandbox: "allow-scripts allow-same-origin allow-popups allow-forms allow-downloads" })] }));
}
//# sourceMappingURL=BananaPanelHost.js.map