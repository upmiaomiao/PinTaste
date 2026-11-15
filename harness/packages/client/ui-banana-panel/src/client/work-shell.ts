/** Work-only navigation state and the same-origin shared settings protocol. */
import { defineStore } from '@deepseek-ai/dsh-client-store'

export type WorkSettingsSection = 'general' | 'account' | 'work' | 'linked' | 'data' | 'about' | 'admin'
export type WorkSettingsMessage = { type: 'close' | 'user-updated' | 'auth-required' } | { type: 'open-work'; path: '/work' | '/work/admin' | '/work?templates=review' }
export interface WorkIdentity { name: string; organization: string }

/** @returns one root-scoped viewing-state store shared by work navigation entries. */
export function createWorkShellStore() {
  return defineStore({
    init: () => ({ section: null as WorkSettingsSection | null, historyOpen: false, identityRevision: 0 }),
    actions: {
      openSettings: (draft, section: WorkSettingsSection) => { draft.section = section; draft.historyOpen = false },
      closeSettings: draft => { draft.section = null },
      showHistory: draft => { draft.historyOpen = true; draft.section = null },
      closeHistory: draft => { draft.historyOpen = false },
      refreshIdentity: draft => { draft.identityRevision++ },
    },
  })
}

/** @param section - The selected shared settings section. @returns same-origin iframe entry without QA navigation. */
export function workSettingsUrl(section: WorkSettingsSection): string {
  return '/?settings=' + section + '&embed=work'
}

/** @param value - Untrusted iframe message. @returns only the shared, non-privileged navigation protocol. */
export function workSettingsMessage(value: unknown): WorkSettingsMessage | null {
  if (!value || typeof value !== 'object' || !('type' in value)) return null
  if (value.type === 'wuyu.settings.close') return { type: 'close' }
  if (value.type === 'wuyu.settings.user-updated') return { type: 'user-updated' }
  if (value.type === 'wuyu.settings.auth-required') return { type: 'auth-required' }
  if (value.type === 'wuyu.settings.open-work' && 'path' in value) {
    const path = value.path
    if (path === '/work' || path === '/work/admin' || path === '/work?templates=review') return { type: 'open-work', path }
  }
  return null
}

/** @param signal - Cancels this mounted account view. @returns the central platform's safe display identity only. */
export async function readWorkIdentity(signal?: AbortSignal): Promise<WorkIdentity> {
  const response = await fetch('/api/me', { credentials: 'same-origin', redirect: 'error', cache: 'no-store', ...(signal ? { signal } : {}) })
  if (!response.ok) throw new Error('WORK_IDENTITY_UNAVAILABLE')
  const value: unknown = await response.json()
  if (!value || typeof value !== 'object' || !('name' in value) || typeof value.name !== 'string'
    || !('organization' in value) || typeof value.organization !== 'string') throw new Error('WORK_IDENTITY_INVALID')
  return { name: value.name, organization: value.organization }
}
