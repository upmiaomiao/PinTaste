import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * Right details column: hosts the official Banana Slides project panel for the
 * current Session's project. Reads the shared panel store (written by the
 * transcript's tool.call.toolview row) and renders the iframe host, or an
 * empty hint until a project is set. The header mirrors the DSH details-column
 * chrome (title + close) so the panel reads as a native right-side surface.
 */
import { useRef } from 'react';
import { BananaPanelHost } from "./BananaPanelHost.js";
import css from './BananaPanelRow.module.css';
/** Right-column deck panel; renders an empty hint until a project is set. */
export function BananaDetailsPanel({ useStore, t, closeDetails, writeToConversation }) {
    const projectHandle = useStore(s => s.projectHandle);
    const previewTarget = useStore(s => s.previewTarget), previewRequest = useStore(s => s.previewRequest);
    const bodyRef = useRef(null);
    /** Open the panel's own signed preview in a new tab instead of duplicating its URL contract. */
    const openInNewTab = () => { const frame = bodyRef.current?.querySelector('iframe'); if (frame && frame.src)
        window.open(frame.src, '_blank', 'noopener'); };
    return (_jsxs("div", { className: css.detailsRoot, children: [_jsxs("div", { className: css.header, children: [_jsx("div", { className: css.title, children: t('details.title') }), _jsxs("div", { className: css.headerRight, children: [projectHandle !== null ? _jsx("button", { type: "button", className: css.headerAction, onClick: openInNewTab, children: t('details.open') }) : null, _jsx("button", { type: "button", className: css.close, "aria-label": t('details.close'), onClick: closeDetails, children: _jsx("svg", { viewBox: "0 0 16 16", width: "14", height: "14", "aria-hidden": true, children: _jsx("path", { d: "M4 4l8 8M12 4l-8 8", stroke: "currentColor", strokeWidth: "1.5", strokeLinecap: "round" }) }) })] })] }), _jsx("div", { className: css.body, ref: bodyRef, children: projectHandle === null
                    ? _jsx("div", { className: css.detailsEmpty, children: t('details.empty') })
                    : _jsx(BananaPanelHost, { projectHandle: projectHandle, target: previewTarget, targetRevision: previewRequest, writeToConversation: writeToConversation, t: t }) })] }));
}
//# sourceMappingURL=BananaDetailsPanel.js.map