import assert from 'node:assert/strict'
import test from 'node:test'
import { apply } from './project-workspace.mjs'

test('registers the project while preserving an existing default and its sessions', async () => {
  const calls = []
  const existing = { id: 'old-workspace', path: 'C:/Users/example/Documents/default', sessions: ['old-user-session'] }
  const ctx = { workspaceRegistry: {
    initializeDefault: async resolve => { calls.push(['initialize', await resolve()]); return existing },
    create: async (directory, title) => { calls.push(['create', directory, title]); return { directory } },
  } }
  await apply(ctx, { directory: 'D:/Wuyu/project' })
  assert.deepEqual(calls, [['initialize', 'D:/Wuyu/project'], ['create', 'D:/Wuyu/project', '无隅见微']])
  assert.deepEqual(existing.sessions, ['old-user-session'])
  assert.equal(existing.path, 'C:/Users/example/Documents/default')
})

test('rejects relative project paths before making registry changes', async () => {
  await assert.rejects(apply({ workspaceRegistry: {} }, { directory: 'relative/project' }), /absolute/)
})
