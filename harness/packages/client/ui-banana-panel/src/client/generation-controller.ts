/** Per-Session interaction state and safe read-only model availability. */
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'
import { defaultGenerationSettings, generationRequestText, parseGenerationOptions, restoreGenerationSettings, updateGenerationSettings } from './generation-settings.ts'
import type { GenerationOptions, GenerationSettings, GenerationTextTranslator } from './generation-settings.ts'

/** A single observable owns the displayed and submitted composer choices. */
export interface GenerationSnapshot {
  settings: GenerationSettings
  options: GenerationOptions | null
  phase: 'idle' | 'loading' | 'ready' | 'error'
}
/** Only finite settings JSON is retained; profiles and credentials are never stored. */
export interface GenerationPersistence {
  read(): string | null
  write(value: string): void
}
/** Session lifetime owns this controller; no generation starts from choosing an option. */
export class GenerationController {
  readonly state: SnapshotStore<GenerationSnapshot>
  private disposed = false
  private pending: Promise<void> | undefined
  /** @param readOptions - Read-only safe model profile query. @param persistence - Optional Session-specific browser settings storage. @param translate - Current locale wording for the logged user summary. */
  constructor(private readonly readOptions: () => Promise<unknown>, private readonly persistence?: GenerationPersistence, private readonly translate?: GenerationTextTranslator) {
    let settings = defaultGenerationSettings()
    try { settings = restoreGenerationSettings(persistence?.read() ?? null) ?? settings } catch { /* Browser storage may be unavailable; interaction remains in memory. */ }
    this.state = createSnapshotStore<GenerationSnapshot>({ settings, options: null, phase: 'idle' })
  }
  /** @param patch - User choices; explicit unavailable profiles are ignored. */
  change(patch: Partial<GenerationSettings>): void {
    if (this.disposed) return
    const snapshot = this.state.getSnapshot()
    if (patch.image_profile && patch.image_profile !== 'default' && (snapshot.phase !== 'ready' || !snapshot.options?.profiles.some(profile => profile.id === patch.image_profile && profile.available))) return
    this.state.update(draft => { draft.settings = updateGenerationSettings(draft.settings, patch) })
    try { this.persistence?.write(JSON.stringify(this.state.getSnapshot().settings)) } catch { /* A blocked storage area does not reject the user's in-memory choice. */ }
  }
  /** @returns Coalesced safe-options refresh, retaining explicit choices for recovery on failure. */
  load(): Promise<void> {
    if (this.disposed || this.state.getSnapshot().phase === 'ready' && this.canSubmit()) return Promise.resolve()
    if (this.pending) return this.pending
    this.state.update(draft => { draft.phase = 'loading' })
    const pending = this.readOptions().then(raw => {
      const options = parseGenerationOptions(raw)
      if (this.disposed) return
      this.state.update(draft => {
        draft.options = options
        draft.phase = 'ready'
      })
    }).catch(() => {
      // Transport/provider details stay out of the public toolbar state.
      if (this.disposed) return
      this.state.update(draft => { draft.phase = 'error'; draft.options = null })
    }).finally(() => { if (this.pending === pending) this.pending = undefined })
    this.pending = pending
    return pending
  }
  /** @returns Whether the default or an explicitly verified available profile can be submitted. */
  canSubmit(): boolean {
    const { settings, phase, options } = this.state.getSnapshot()
    return settings.image_profile === 'default' || phase === 'ready' && options?.profiles.some(profile => profile.id === settings.image_profile && profile.available) === true
  }
  /** @param text - This submitted message. @returns Synchronously frozen settings and text; unverified explicit profiles fail before admission. */
  submission(text: string): string {
    if (!this.canSubmit()) throw new Error('GENERATION_PROFILE_UNAVAILABLE')
    return generationRequestText(text, this.state.getSnapshot().settings, this.translate)
  }
  /** Discard late read results when the owning Session or plugin is disposed. */
  dispose(): void { this.disposed = true }
}
