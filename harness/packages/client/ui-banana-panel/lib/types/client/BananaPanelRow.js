import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * Banana Slides deck row, browser half: extracts the opaque project handle
 * from a settled banana MCP call, then hands it to the right details column
 * through the inject-bound `openPanel` callback (which writes the shared
 * store and opens the details panel). The row itself stays a compact status
 * line; the official project panel lives in the right column.
 */
import { useEffect } from 'react';
import { projectContext } from "./preview-target.js";
import { bananaToolRow } from "./tool-row.js";
import css from './BananaPanelRow.module.css';
/** Inline row that routes one deck project into the right details panel. */
export function BananaPanelRow(props) {
    const { block, toolName, t, openPanel, rememberProject } = props;
    const row = bananaToolRow(toolName, block);
    const settled = 'kind' in block;
    const context = settled ? projectContext(block.content.filter(x => x.type === 'text').map(x => x.text), block.call?.argsRaw) : null;
    const handle = context?.handle || null;
    useEffect(() => { if (context) {
        rememberProject(context.handle, context.projectId);
        if (settled && !block.isError && toolName === 'mcp__banana__open_presentation_panel')
            openPanel(context.handle, context.projectId, context.target);
    } }, [handle, toolName, settled && block.isError, context?.target?.view, context?.target?.page_number]);
    return (_jsx("div", { className: css.card, children: _jsxs("div", { className: row.state === 'failed' ? css.error : css.rowOpened, children: [_jsx("span", { children: t(row.titleKey) }), _jsx("span", { children: t(row.statusKey) }), context ? _jsx("button", { type: "button", className: css.previewButton, onClick: () => openPanel(context.handle, context.projectId, context.target), children: t('row.opened') }) : null] }) }));
}
//# sourceMappingURL=BananaPanelRow.js.map