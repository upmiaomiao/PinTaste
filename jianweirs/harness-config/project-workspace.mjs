/** Register the product project without moving or rewriting existing sessions. */
import { isAbsolute } from 'node:path'

export const name = 'wuyu-project-workspace'
export const inject = ['workspaceRegistry']

/** Initialize fresh installations at this project and make it available to existing installations. */
export async function apply(ctx, config) {
  if (typeof config.directory !== 'string' || !isAbsolute(config.directory)) {
    throw new Error('Wuyu project workspace requires an absolute existing directory')
  }
  // The upstream registry only initializes a default when no workspace or session
  // exists. Existing histories retain their origin and their workspace membership.
  await ctx.workspaceRegistry.initializeDefault(async () => config.directory)
  await ctx.workspaceRegistry.create(config.directory, '无隅见微')
}
