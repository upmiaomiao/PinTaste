/**
 * Banana Slides deck panel, browser half. Two contributions share one
 * Session-scoped store:
 * - `tool.call.toolview` rows for the entry-point banana MCP tools extract the
 *   project handle and route it into the right details column.
 * - the `details` slot (right column) hosts the official project panel at a
 *   lower priority than the generic tool-details panel, so it replaces that
 *   column while a banana project is being worked on.
 *
 * The wire tool names follow the mcp-client convention
 * `mcp__<serverName>__<tool>`, with the banana server mounted as `banana`.
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis';
import { type BananaPanelKey } from './locales.ts';
declare module '@deepseek-ai/dsh-client-ui-slots' {
    interface LocaleNamespaceMap {
        /** The banana deck panel's copy. */
        bananaPanel: BananaPanelKey;
    }
}
/** Required services: the slot registry, the locale registry, and the layout face. */
export declare const inject: string[];
/**
 * Client plugin body: register the banana deck dictionary, the transcript rows
 * that route the current project into the right column, and the right-column
 * deck panel itself.
 * @param ctx - client root context.
 */
export declare function apply(ctx: ClientContext): void;
//# sourceMappingURL=index.d.ts.map