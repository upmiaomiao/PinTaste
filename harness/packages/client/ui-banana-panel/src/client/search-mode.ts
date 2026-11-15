/**
 * Web / deep search selection belongs to the current composer Session.
 *
 * Sending with a switch on appends a marker line the assistant reads ("【资料方式：深度探索】") and
 * records the choice on the server for this user (the click is the consent). When the assistant then
 * opens research for the project, the server starts that search at once; nothing in the
 * conversation asks again. Sending retains the selection; clicking it again turns it off.
 * Each explicit submission still records its own choice and marker. The selection does not
 * survive a full reload or authorize a search without a user request.
 */
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'

export type SearchMode = 'off' | 'web' | 'deep'
export type ArmedSearch = 'web' | 'deep'

export const SEARCH_MARKERS: Record<ArmedSearch, string> = {
  web: '【资料方式：联网搜索】',
  deep: '【资料方式：深度探索】',
}

export interface SearchModeSnapshot { mode: SearchMode }

export class SearchModeController {
  readonly state: SnapshotStore<SearchModeSnapshot> = createSnapshotStore<SearchModeSnapshot>({ mode: 'off' })
  private disposed = false

  /** @param arm - records the choice on the server; failures are retried once and then reported. */
  constructor(private readonly arm: (mode: ArmedSearch) => Promise<void> = async () => {},
              private readonly report: (error: unknown) => void = () => {}) {}

  /** Clicking the active switch again turns it off; the two switches exclude each other. */
  toggle(mode: ArmedSearch): void {
    this.state.set({ mode: this.state.getSnapshot().mode === mode ? 'off' : mode })
  }

  /** Stop preparation owned by a retired Session; it must not retry or notify a new view. */
  dispose(): void { this.disposed = true }

  /** Permission preparation must complete before the SDK can admit this explicit submission. */
  async submission(text: string): Promise<string> {
    if (this.disposed) throw new Error('RESEARCH_INTENT_RETIRED')
    const mode = this.state.getSnapshot().mode
    if (mode === 'off') return text
    try {
      try { await this.arm(mode) }
      catch (error) {
        if (this.disposed) throw error
        await this.arm(mode)
      }
    } catch (error) {
      if (!this.disposed) this.report(error)
      throw error
    }
    if (this.disposed) throw new Error('RESEARCH_INTENT_RETIRED')
    // The envelope is hidden in the user's bubble (ui-chat visibleWorkText) but read by the assistant.
    return (text.trimEnd() ? text.trimEnd() + '\n\n' : '') + '<wuyu-search-mode>' + SEARCH_MARKERS[mode] + '</wuyu-search-mode>'
  }
}

/** Same-origin, cookie-authenticated; the interaction header marks a real user gesture. */
export async function armResearchOnServer(mode: ArmedSearch, request: typeof fetch = fetch): Promise<void> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('RESEARCH_INTENT_TIMEOUT')), 10000)
  try {
    const response = await request.call(globalThis, '/agent-api/api/projects/research-intent', {
      method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'X-Wuyu-Interaction': 'research-confirmation' },
      body: JSON.stringify({ mode }),
    })
    if (!response.ok) throw new Error('RESEARCH_INTENT_' + response.status)
  } finally { clearTimeout(timer) }
}
