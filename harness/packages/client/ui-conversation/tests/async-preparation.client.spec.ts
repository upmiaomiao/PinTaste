// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import { ConversationController } from '../src/client/service.ts'
import type { SessionFace } from '@deepseek-ai/dsh-api-session-controller/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'

function fixture() {
  // Exercise the shipping send method without booting unrelated remote/view services.
  const controller = Object.create(ConversationController.prototype) as ConversationController
  Object.assign(controller, { submissionModifiers: new Map(), draftAttachments: new Map() })
  const prompt = vi.fn(async () => ({ ok: true }))
  const echo = vi.fn(() => ({ requestId: 'request-test', abandon() {} }))
  const session = { sessionId: 'async-config-test', getSnapshot: () => ({ subagent: null }), beginSubmission: echo, prompt } as unknown as SessionFace
  return { controller, session, prompt, echo }
}

describe('asynchronous preparation before Host admission', () => {
  it('waits for saved settings before later modifiers, echo or prompt', async () => {
    const f = fixture()
    let finish!: (text: string) => void
    f.controller.registerSubmissionModifier(f.session.sessionId, () => new Promise<string>(resolve => { finish = resolve }))
    const later = vi.fn(text => text + ':search')
    f.controller.registerSubmissionModifier(f.session.sessionId, later)
    const sending = f.controller.sendSession(f.session, 'draft', [], 'queue')
    await Promise.resolve()
    expect(f.echo).not.toHaveBeenCalled(); expect(f.prompt).not.toHaveBeenCalled(); expect(later).not.toHaveBeenCalled()
    finish('saved:20')
    await expect(sending).resolves.toEqual({ kind: 'success' })
    expect(f.echo).toHaveBeenCalledWith(expect.objectContaining({ text: 'saved:20:search' }))
    expect(f.prompt.mock.calls[0]?.[0]).toEqual([{ type: 'text', text: 'saved:20:search' }])
  })
  it('rejects before research preparation and Host dispatch on failed save', async () => {
    const f = fixture(), later = vi.fn(text => text)
    f.controller.registerSubmissionModifier(f.session.sessionId, async () => { throw new Error('save failed') })
    f.controller.registerSubmissionModifier(f.session.sessionId, later)
    await expect(f.controller.sendSession(f.session, 'draft', [], 'queue')).rejects.toThrow('save failed')
    expect(later).not.toHaveBeenCalled(); expect(f.echo).not.toHaveBeenCalled(); expect(f.prompt).not.toHaveBeenCalled()
  })
  it('preserves synchronous attachment settlement and stops after an aborted preparation', async () => {
    const f = fixture(), settled = vi.fn(), abort = new AbortController()
    f.controller.registerSubmissionModifier(f.session.sessionId, text => ({ text, settled }))
    f.controller.registerSubmissionModifier(f.session.sessionId, async text => { abort.abort(); return text })
    await expect(f.controller.sendSession(f.session, 'draft', [], 'queue', abort.signal)).rejects.toThrow()
    expect(settled).toHaveBeenCalledWith(false); expect(f.prompt).not.toHaveBeenCalled()
  })
})
