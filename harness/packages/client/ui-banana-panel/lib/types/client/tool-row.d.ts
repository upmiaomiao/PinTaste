/** Localized transcript rows never derive visible text from MCP arguments or results. */
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client';
/** Exact keyed slot registrations for the current Banana server tool inventory. */
export declare const BANANA_TOOL_KEYS: string[];
/**
 * Project a localized action and transport status without exposing tool payloads.
 * Completion means the tool returned; it does not assert provider connectivity or job success.
 * @param toolName - keyed MCP wire name.
 * @param block - the original running or settled transcript node.
 * @returns locale keys and a presentation state; no arguments or output text.
 */
export declare function bananaToolRow(toolName: string, block: ToolCallViewProps['block']): {
    titleKey: string;
    statusKey: "row.status.running" | "row.status.completed" | "row.status.failed";
    state: string;
};
//# sourceMappingURL=tool-row.d.ts.map