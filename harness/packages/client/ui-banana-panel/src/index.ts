/** Authenticated host association between uploaded PPT materials and one chat. */
import type { Context } from '@deepseek-ai/cordis'
import { randomUUID } from 'node:crypto'
import { join, isAbsolute } from 'node:path'
import { createPresentationConfigHandler } from './presentation-config-http.ts'
import { PresentationRequirementsStore } from './presentation-requirements.ts'
import { installPresentationRequirements } from './presentation-requirements-host.ts'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-host-webserver'
import type {} from '@deepseek-ai/dsh-client-connection'
import type {} from '@deepseek-ai/dsh-api-session-controller'
import { createMaterialContextHandler, MaterialContextFailure, workflowToolFailure } from './material-context.ts'
import { installTextQuota, type TextQuotaConfig } from './text-quota.ts'

/** Optional server-only quota settings for one independently isolated account. */
export interface Config { quota?: TextQuotaConfig }

/** Existing host services supply authentication, owned sessions and scoped MCP tools. */
export const inject = ['webServer', 'connection', 'agents', 'tools', 'sessionController']

/** Register authenticated conversation-preference and material-context routes; neither route grants generation approval. */
export function apply(ctx: Context, config?: Config): void {
  installTextQuota(ctx, config?.quota)
  const home = process.env.DSH_HOME
  if (!home || !isAbsolute(home)) throw new Error('Conversation preferences require an absolute DSH_HOME')
  const resolveAgent = async (id: string) => {
    const live = ctx.agents.get(SessionId(id))
    const result = live ? { agent: live } : await ctx.sessionController.resolveAgent(SessionId(id))
    if ('error' in result || result.agent.id !== id || !ctx.agents.roots().includes(result.agent)) return undefined
    return result.agent
  }
  const requirements = new PresentationRequirementsStore(join(home, 'wuyu-presentation-settings'))
  const requirementHost = installPresentationRequirements(ctx, requirements, resolveAgent)
  const preferences = createPresentationConfigHandler({
    directory: requirements.directory,
    requirements,
    reject: request => ctx.connection.requestRejection(request),
    ownsSession: async id => !!await resolveAgent(id),
    ...requirementHost,
  })
  ctx.effect(() => ctx.webServer.register({ kind: 'exact', path: '/api/wuyu/presentation-settings', handler: preferences }), 'wuyu-presentation-settings.route')
  const handler = createMaterialContextHandler({
    reject: request => ctx.connection.requestRejection(request),
    async resolveAgent(id) {
      const live = ctx.agents.get(SessionId(id))
      const result = live ? { agent: live } : await ctx.sessionController.resolveAgent(SessionId(id))
      if ('error' in result || !ctx.agents.roots().includes(result.agent)) return undefined
      return result.agent
    },
    async readWorkflow(agent, projectHandle, signal) {
      let result
      try {
        result = await ctx.tools.execute({ name: 'mcp__banana__get_presentation_workflow',
          arguments: { project_handle: projectHandle }, callId: ToolCallId('wuyu-material-context-' + randomUUID()), agent, signal })
      } catch {
        throw new MaterialContextFailure(502, signal.aborted ? 'CONTEXT_TOOL_TIMEOUT' : 'CONTEXT_EXECUTION_FAILED')
      }
      if (result.isError) throw workflowToolFailure(result.error.info?.code)
      const value = result.value
      if (!value || typeof value !== 'object' || Array.isArray(value) || !('structuredContent' in value)) throw new MaterialContextFailure(502, 'CONTEXT_TOOL_VALUE_MISSING')
      return value.structuredContent
    },
  })
  ctx.effect(() => ctx.webServer.register({ kind: 'exact', path: '/api/wuyu/material-context', handler }), 'wuyu-material-context.route')
}
