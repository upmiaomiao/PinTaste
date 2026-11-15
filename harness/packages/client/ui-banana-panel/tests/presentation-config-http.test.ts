// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { createServer, type Server } from 'node:http'
import { mkdtemp, readFile, readdir, rm, stat, symlink } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { createPresentationConfigHandler } from '../src/presentation-config-http.ts'

const roots: string[] = [], servers: Server[] = []
const config = { page_count: 20, output_preference: 'image', language: 'en' }
afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => server.close(e => e ? reject(e) : resolve()))))
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })))
})
async function setup(directory?: string) {
  const root = directory ?? await mkdtemp(join(tmpdir(), 'wuyu-preferences-'))
  if (!directory) roots.push(root)
  const server = createServer(createPresentationConfigHandler({ directory: root, reject: req => req.headers.authorization === 'test' ? undefined : 401, ownsSession: async id => ['chat-a', 'chat-b'].includes(id) }))
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Missing fixture port')
  return { root, send: (body: unknown, auth = 'test') => fetch(`http://127.0.0.1:${address.port}/`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: auth }, body: JSON.stringify(body) }) }
}
const save = (extra = {}) => ({ operation: 'save', session_id: 'chat-a', expected_revision: 0, request_id: '00000000-0000-4000-8000-000000000001', config, ...extra })

describe('conversation preference HTTP persistence', () => {
  it('saves independently of generation and restores after a fresh handler is created', async () => {
    const first = await setup()
    expect((await first.send(save())).status).toBe(200)
    const second = await setup(first.root)
    expect(await (await second.send({ operation: 'read', session_id: 'chat-a' })).json()).toMatchObject({ revision: 1, config })
    expect(await (await second.send({ operation: 'read', session_id: 'chat-b' })).json()).toEqual({ revision: 0, config: { page_count: null, output_preference: 'editable', language: 'zh' } })
    expect(await readdir(first.root)).toEqual(['chat-a.json'])
    expect((await stat(join(first.root, 'chat-a.json'))).mode & 0o777).toBe(0o600)
  })
  it('rejects unauthenticated, foreign-session and path-traversal requests before writing', async () => {
    const { send, root } = await setup()
    expect((await send(save(), 'wrong')).status).toBe(401)
    expect((await send(save({ session_id: 'other-account' }))).status).toBe(404)
    expect((await send(save({ session_id: '../chat-a' }))).status).toBe(400)
    expect(await readdir(root)).toEqual([])
  })
  it.each([0, 81, 1.5, '20', undefined])('rejects invalid counts without changing stored data: %j', async page_count => {
    const { send, root } = await setup()
    expect((await send(save({ config: { ...config, page_count } }))).status).toBe(400)
    expect(await readdir(root)).toEqual([])
  })
  it('rejects approval fields and serializes concurrent revision checks', async () => {
    const { send } = await setup()
    expect((await send(save({ config: { ...config, template_confirmed: true } }))).status).toBe(400)
    const responses = await Promise.all([send(save()), send(save({ request_id: '00000000-0000-4000-8000-000000000002', config: { ...config, page_count: 10 } }))])
    expect(responses.map(r => r.status).sort()).toEqual([200, 409])
    const winner = await (await send({ operation: 'read', session_id: 'chat-a' })).json() as { revision: number }
    expect(winner.revision).toBe(1)
  })
  it('deduplicates a lost acknowledgement and refuses changed content under the same request id', async () => {
    const { send } = await setup()
    await send(save())
    expect(await (await send(save())).json()).toMatchObject({ revision: 1 })
    expect((await send(save({ config: { ...config, page_count: 5 } }))).status).toBe(409)
  })
  it('refuses symlink files without reading or overwriting their target', async () => {
    const { send, root } = await setup()
    await send(save({ session_id: 'chat-b' }))
    const target = join(root, 'chat-b.json'), bytes = await readFile(target)
    await symlink(target, join(root, 'chat-a.json'))
    expect((await send(save())).status).toBe(503)
    expect(await readFile(target)).toEqual(bytes)
  })
})
