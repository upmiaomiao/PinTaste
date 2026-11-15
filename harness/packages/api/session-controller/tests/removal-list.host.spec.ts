import { describe, expect, it, vi } from 'vitest'
import SessionController from '../src/index.ts'
import { TYPERT } from '../lib/typert.host.js'
import { TYPERT_REMOTE } from '../lib/typert.remote-client.js'

describe('account history archive filtering', () => {
  const signal = new AbortController().signal
  const items = [{ sessionId: 'session-visible' }, { sessionId: 'session-removed' }]
  function owner(archived: readonly string[] | undefined) {
    return { listState: { list: vi.fn(async () => items) }, ctx: { get: vi.fn(() => archived === undefined ? undefined : { archivedSessionIds: archived }) } }
  }
  it('keeps the full authorized list for existing callers', async () => {
    const service = owner(['session-removed'])
    const result = await SessionController.prototype.list.call(service as never, {}, signal)
    expect(result.items).toEqual(items)
    expect(service.ctx.get).not.toHaveBeenCalled()
  })
  it('excludes removed sessions only when requested for history', async () => {
    const service = owner(['session-removed'])
    const result = await SessionController.prototype.list.call(service as never, { excludeArchived: true }, signal)
    expect(result.items).toEqual([items[0]])
    expect(items).toHaveLength(2)
  })
  it('keeps authorized rows in a profile without workspace archival', async () => {
    const service = owner(undefined)
    expect((await SessionController.prototype.list.call(service as never, { excludeArchived: true }, signal)).items).toEqual(items)
  })
  it('reads archive membership after the asynchronous list completes', async () => {
    const archived: string[] = []
    const service = owner(archived)
    service.listState.list = vi.fn(async () => { archived.push('session-removed'); return items })
    expect((await SessionController.prototype.list.call(service as never, { excludeArchived: true }, signal)).items).toEqual([items[0]])
  })
})


describe('published history request codecs', () => {
  const host = TYPERT.invocations.find(item => item.namespace === 'session' && item.method === 'list')!
  const client = TYPERT_REMOTE.descriptors.find(item => item.namespace === 'session' && item.method === 'list')!
  for (const [name, descriptor] of [['host', host], ['client', client]] as const) {
    it(`${name} retains the boolean history flag and legacy cursor`, () => {
      const schema = descriptor.parameters[0]!.codec!.schema
      expect(schema.parse({ excludeArchived: true, cursor: 'legacy' })).toEqual({ excludeArchived: true, cursor: 'legacy' })
      expect(schema.parse({})).toEqual({})
    })
    it(`${name} rejects a string in place of the history flag`, () => {
      expect(descriptor.parameters[0]!.codec!.schema.safeParse({ excludeArchived: 'true' }).success).toBe(false)
    })
  }
})
