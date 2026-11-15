/** The work shell follows the same account UI language as the platform settings. */
export type UiLanguage = 'zh-CN' | 'en'
export const UI_LANGUAGE_CACHE_KEY = 'wuyu-ui-language'
export const UI_LANGUAGE_EVENT = 'wuyu-ui-language-change'
export const WORK_UI_LANGUAGE_APPLIED_EVENT = 'wuyu-work-ui-language-applied'

export interface ExternalLocaleBinding { apply(id: string): void; dispose(): void }
export interface AccountLocaleRuntime {
  getLocale(): { active: string }
  subscribe(listener: () => void): () => void
  bindExternalPreference(source: {
    initialLocale: string
    save(id: string): Promise<string | void>
    onError(error: unknown): void
  }): ExternalLocaleBinding
}
export interface AccountLanguagePorts {
  cached(): string | null
  cache(language: UiLanguage | null): void
  read(signal: AbortSignal): Promise<{ userId: string; language: UiLanguage }>
  write(language: UiLanguage, userId: string, signal: AbortSignal): Promise<UiLanguage>
  publish(language: UiLanguage): void
  listen(refresh: () => void): () => void
  notify(language: UiLanguage): void
}

export class UiLanguageError extends Error {
  notified = false
  constructor(readonly status: number, message = 'UI_LANGUAGE_UNAVAILABLE') { super(message) }
}

export function uiLanguage(value: unknown): UiLanguage | null {
  return value === 'zh' || value === 'zh-CN' ? 'zh-CN' : value === 'en' ? 'en' : null
}
const workLocale = (language: UiLanguage): 'zh' | 'en' => language === 'en' ? 'en' : 'zh'

/** Reads never write account preferences; native Language choices await the same PATCH as the platform. */
export class WorkUiLanguageBridge {
  readonly ready: Promise<void>
  private readonly binding: ExternalLocaleBinding
  private readonly unlisten: () => void
  private disposed = false
  private readGeneration = 0
  private accountGeneration = 0
  private selectionGeneration = 0
  private pendingSelections = 0
  private userId: string | undefined
  private tail: Promise<unknown> = Promise.resolve()
  private readonly reads = new Set<AbortController>()
  private readonly writes = new Set<AbortController>()

  constructor(private readonly locale: AccountLocaleRuntime, private readonly ports: AccountLanguagePorts) {
    const initial = uiLanguage(ports.cached()) ?? 'zh-CN'
    this.binding = locale.bindExternalPreference({
      initialLocale: workLocale(initial),
      save: id => this.save(id),
      onError: error => { if (!this.disposed && !(error instanceof UiLanguageError && error.notified)) ports.notify(uiLanguage(locale.getLocale().active) ?? 'zh-CN') },
    })
    this.unlisten = ports.listen(() => { void this.refresh() })
    this.ready = this.refresh()
  }

  /** Re-read the authenticated account after navigation, focus, storage, or shared-settings events. */
  async refresh(): Promise<void> {
    if (this.disposed) return
    const generation = ++this.readGeneration
    const controller = new AbortController()
    this.reads.add(controller)
    try {
      const value = await this.ports.read(controller.signal)
      if (this.disposed || generation !== this.readGeneration) return
      const changed = this.userId !== undefined && this.userId !== value.userId
      if (changed) this.invalidateAccount()
      this.userId = value.userId
      if (this.pendingSelections === 0 || changed) {
        this.binding.apply(workLocale(value.language))
        this.ports.cache(value.language)
      }
    } catch (error) {
      if (this.disposed || generation !== this.readGeneration) return
      if (error instanceof UiLanguageError && error.status === 401) {
        this.invalidateAccount()
        this.userId = undefined
        // The locale-only cache also serves the signed-out login page. Keep
        // the last acknowledged projection, while dropping all account ownership.
      }
      // Offline reads retain the cache/default projection and never persist it to an account.
    } finally {
      this.reads.delete(controller)
    }
  }

  private async save(id: string): Promise<string> {
    const language = uiLanguage(id)
    if (language === null || this.disposed) throw new UiLanguageError(400)
    this.pendingSelections++
    const selection = ++this.selectionGeneration
    let succeeded = false
    try {
      await this.ready
      if (this.userId === undefined) await this.refresh()
      const userId = this.userId
      const account = this.accountGeneration
      if (userId === undefined || this.disposed) throw new UiLanguageError(401)
      const operation = this.tail.then(async () => {
        if (this.disposed || account !== this.accountGeneration || userId !== this.userId) throw new UiLanguageError(409, 'ACCOUNT_CHANGED')
        const controller = new AbortController()
        this.writes.add(controller)
        try {
          const accepted = await this.ports.write(language, userId, controller.signal)
          if (this.disposed || account !== this.accountGeneration || userId !== this.userId) throw new UiLanguageError(409, 'ACCOUNT_CHANGED')
          // A GET issued before this successful commit cannot project stale language afterwards.
          this.readGeneration++
          if (selection === this.selectionGeneration) {
            this.ports.cache(accepted)
            this.ports.publish(accepted)
          }
          return workLocale(accepted)
        } catch (error) {
          if (error instanceof UiLanguageError && (error.status === 401 || error.status === 409)) await this.refresh()
          throw error
        } finally {
          this.writes.delete(controller)
        }
      })
      this.tail = operation.catch(() => {})
      const accepted = await operation
      succeeded = true
      return accepted
    } catch (error) {
      const failure = error instanceof UiLanguageError ? error : new UiLanguageError(0)
      if (!this.disposed && selection === this.selectionGeneration) {
        failure.notified = true
        this.ports.notify(uiLanguage(this.locale.getLocale().active) ?? 'zh-CN')
      }
      throw failure
    } finally {
      this.pendingSelections--
      if (!succeeded && !this.disposed) void this.refresh()
    }
  }

  private invalidateAccount(): void {
    this.accountGeneration++
    for (const request of this.writes) request.abort()
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.unlisten()
    for (const request of this.reads) request.abort()
    for (const request of this.writes) request.abort()
    this.binding.dispose()
  }
}

function notifyLanguageFailure(language: UiLanguage): void {
  const previous = document.querySelector('[data-wuyu-ui-language-error]')
  previous?.remove()
  const message = document.createElement('div')
  message.dataset.wuyuUiLanguageError = ''
  message.setAttribute('role', 'alert')
  message.textContent = language === 'en' ? 'Language was not saved. Please try again.' : '语言未保存，请重试。'
  Object.assign(message.style, { position: 'fixed', bottom: '20px', right: '20px', zIndex: '10000', maxWidth: 'calc(100vw - 40px)', padding: '12px 16px', borderRadius: '10px', background: '#263445', color: '#fff', fontSize: '14px' })
  document.body.appendChild(message)
  window.setTimeout(() => { message.remove() }, 6000)
}

/** Browser-only adapter. Requests carry account ownership, never a token or API credential. */
export function bindWorkUiLanguage(locale: AccountLocaleRuntime): () => void {
  const request = async (path: string, signal: AbortSignal, init: RequestInit = {}): Promise<unknown> => {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...init, signal })
    if (!response.ok) throw new UiLanguageError(response.status)
    return response.json()
  }
  const languageFrom = (value: unknown): UiLanguage => {
    const language = value && typeof value === 'object' && 'ui_language' in value ? uiLanguage(value.ui_language) : null
    if (language === null) throw new UiLanguageError(502)
    return language
  }
  const controller = new WorkUiLanguageBridge(locale, {
    cached: () => { try { return localStorage.getItem(UI_LANGUAGE_CACHE_KEY) } catch { return null } },
    cache: language => { try { if (language === null) localStorage.removeItem(UI_LANGUAGE_CACHE_KEY); else localStorage.setItem(UI_LANGUAGE_CACHE_KEY, language) } catch { /* Account persistence still works when browser storage is disabled. */ } },
    read: async signal => {
      const [identity, preferences] = await Promise.all([request('/api/me', signal), request('/api/me/preferences', signal)])
      const userId = identity && typeof identity === 'object' && 'id' in identity ? identity.id : undefined
      if (typeof userId !== 'string' || !/^[A-Za-z0-9:_-]{1,128}$/.test(userId)) throw new UiLanguageError(502)
      return { userId, language: languageFrom(preferences) }
    },
    write: async (language, userId, signal) => languageFrom(await request('/api/me/preferences', signal, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-Wuyu-Preferences-User-ID': userId }, body: JSON.stringify({ ui_language: language }),
    })),
    publish: language => { window.dispatchEvent(new CustomEvent(UI_LANGUAGE_EVENT, { detail: { language } })) },
    listen: refresh => {
      const storage = (event: StorageEvent) => { if (event.key === UI_LANGUAGE_CACHE_KEY || event.key === null) refresh() }
      const visible = () => { if (document.visibilityState === 'visible') refresh() }
      const message = (event: MessageEvent) => {
        if (event.origin === location.origin && event.data && ['wuyu.settings.user-updated', 'wuyu.settings.auth-required'].includes(event.data.type)) refresh()
      }
      window.addEventListener('storage', storage)
      window.addEventListener(UI_LANGUAGE_EVENT, refresh)
      window.addEventListener('focus', refresh)
      window.addEventListener('message', message)
      document.addEventListener('visibilitychange', visible)
      return () => {
        window.removeEventListener('storage', storage)
        window.removeEventListener(UI_LANGUAGE_EVENT, refresh)
        window.removeEventListener('focus', refresh)
        window.removeEventListener('message', message)
        document.removeEventListener('visibilitychange', visible)
      }
    },
    notify: notifyLanguageFailure,
  })
  const projected = () => {
    const language = uiLanguage(locale.getLocale().active) ?? 'zh-CN'
    window.dispatchEvent(new CustomEvent(WORK_UI_LANGUAGE_APPLIED_EVENT, { detail: { language } }))
  }
  const unsubscribe = locale.subscribe(projected)
  projected()
  return () => { unsubscribe(); controller.dispose() }
}
