import { randomUUID } from 'node:crypto';
import { createUserMessage, ToolCallId } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { createMaterialContextHandler, MaterialContextFailure, workflowToolFailure } from "./material-context.js";
/** Existing host services supply authentication, owned sessions and scoped MCP tools. */
export const inject = ['webServer', 'connection', 'agents', 'tools', 'sessionController'];
/** Register only the authenticated material-context route; no credentials or model routes are added. */
export function apply(ctx) {
    const handler = createMaterialContextHandler({
        reject: request => ctx.connection.requestRejection(request),
        async resolveAgent(id) {
            const live = ctx.agents.get(SessionId(id));
            const result = live ? { agent: live } : await ctx.sessionController.resolveAgent(SessionId(id));
            if ('error' in result || !ctx.agents.roots().includes(result.agent))
                return undefined;
            return result.agent;
        },
        async readWorkflow(agent, projectHandle, signal) {
            let result;
            try {
                result = await ctx.tools.execute({ name: 'mcp__banana__get_presentation_workflow',
                    arguments: { project_handle: projectHandle }, callId: ToolCallId('wuyu-material-context-' + randomUUID()), agent, signal });
            }
            catch {
                throw new MaterialContextFailure(502, signal.aborted ? 'CONTEXT_TOOL_TIMEOUT' : 'CONTEXT_EXECUTION_FAILED');
            }
            if (result.isError)
                throw workflowToolFailure(result.error.info?.code);
            const value = result.value;
            if (!value || typeof value !== 'object' || Array.isArray(value) || !('structuredContent' in value))
                throw new MaterialContextFailure(502, 'CONTEXT_TOOL_VALUE_MISSING');
            return value.structuredContent;
        },
        enqueue(agent, text, summary) {
            agent.followup(createUserMessage({ content: [{ type: 'text', text }],
                source: { kind: 'plugin', plugin: 'wuyu-ppt-material-context', form: 'notice', summary } }));
        },
    });
    ctx.effect(() => ctx.webServer.register({ kind: 'exact', path: '/api/wuyu/material-context', handler }), 'wuyu-material-context.route');
}
//# sourceMappingURL=index.js.map