/** Restore a linked work record only after the current account lists it. */
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'

interface SessionLinkTarget {
  list: ObservableSnapshot<{ ids: readonly SessionId[] }>
  open(id: SessionId): void
}

/** @param sessions - The authenticated account's session catalog. @param address - Requested product URL. @param replace - Removes only the consumed link selector. @returns Subscription disposer. */
export function bindWorkSessionLink(sessions: SessionLinkTarget, address: URL, replace: (url: string) => void): () => void {
  const requested = address.searchParams.get('session')
  if (!requested || !/^session-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(requested)) return () => {}
  let consumed = false
  const restore = () => {
    if (consumed) return
    const ownId = sessions.list.getSnapshot().ids.find(id => id === requested)
    if (!ownId) return
    consumed = true
    sessions.open(ownId)
    address.searchParams.delete('session')
    replace(address.pathname + address.search + address.hash)
  }
  const dispose = sessions.list.subscribe(restore)
  restore()
  return dispose
}
