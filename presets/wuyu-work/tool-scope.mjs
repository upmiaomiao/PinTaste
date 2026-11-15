/** Limit this preset's inherited tools without changing standard sessions. */
export const name = 'wuyu-work-tool-scope'
export const inject = ['tools']

/** Apply the existing scoped tool restriction; missing deployment tools fail loud. */
export function apply(ctx, config) {
  const allowed = config?.allowedGlobalTools
  if (!Array.isArray(allowed) || !allowed.length || allowed.some(value => typeof value !== 'string' || !value)) {
    throw new Error('wuyu-work requires an explicit nonempty PPT tool allowlist')
  }
  // A session inherits both global MCP tools and its preset's ask-user tool.
  // Restrict that session after composition, rather than the standing preset:
  // a standing restriction would also hide ask-user from each child session.
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow: [...new Set([...allowed, 'ask_user_question'])] })
  })
}
