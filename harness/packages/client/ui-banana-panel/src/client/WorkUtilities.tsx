/** Work navigation shares account settings with the primary Wuyu platform. */
import { useState } from 'react'
import { Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import type { InjectFace, PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type { createWorkShellStore } from './work-shell.ts'
import { WorkSettingsDialog } from './WorkSettingsDialog.tsx'
import { WorkAccount, type WorkAccountInjected } from './WorkAccount.tsx'
import { AccountQuota, type AccountQuotaInjected } from './AccountQuota.tsx'
import css from './WorkUtilities.module.css'

/** The details panel remains a host-owned surface. */
export interface WorkUtilitiesInjected extends WorkAccountInjected, AccountQuotaInjected { openWorkDetails(): void; navigateWork(path: string): void; openWorkSession(id: SessionId): void }
type ShellStore = PropsStore<ReturnType<typeof createWorkShellStore>>
type Props = PropsRuntime<'conversation.work.account'> & PropsLocale<'bananaPanel'> & ShellStore & InjectFace<WorkUtilitiesInjected>

/** Keep work progress, settings and history inside the work application. */
export function WorkUtilities({ useSessions, useStore, actions, openWorkDetails, navigateWork, openWorkSession, readIdentity, readAccount, readQuotaLedger, redeemQuotaCode, t }: Props) {
  const current = useSessions(state => state.current === undefined ? undefined : state.byId[state.current])
  const section = useStore(state => state.section), historyOpen = useStore(state => state.historyOpen)
  const sessions = useSessions(state => state.ids.map(id => state.byId[id]).filter((row): row is NonNullable<typeof row> => row !== undefined && !row.blank && row.origin !== 'subagent'
    && (row.projectionValues?.agentPreset === undefined || row.projectionValues.agentPreset === 'wuyu-work')))
  const [query, setQuery] = useState('')
  return <div className={css.utilities}>
    <AccountQuota readAccount={readAccount} readQuotaLedger={readQuotaLedger} redeemQuotaCode={redeemQuotaCode} useSessions={useSessions} t={t} />
    <button type="button" className={css.progress} onClick={openWorkDetails} aria-label={t('work.progressDetails')}>
      <span className={css.dot} data-running={current?.running === true} aria-hidden />
      <span>{t('work.progress')}</span><small>{t(current?.running === true ? 'work.running' : 'work.details')}</small>
    </button>
    <button type="button" className={css.settings} onClick={() => actions.openSettings('general')} aria-label={t('work.platformSettings')} title={t('work.platformSettings')}>
      <svg viewBox="0 0 24 24" width="19" height="19" fill="none" aria-hidden><path d="m10 3-.7 2.2-2 .9L5 5.5 3 9l1.6 1.7v2.6L3 15l2 3.5 2.3-.6 2 .9L10 21h4l.7-2.2 2-.9 2.3.6 2-3.5-1.6-1.7v-2.6L21 9l-2-3.5-2.3.6-2-.9L14 3Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.5"/></svg>
    </button>
    <WorkAccount readIdentity={readIdentity} useStore={useStore} actions={actions} t={t} />
    {section ? <WorkSettingsDialog key={section} section={section} t={t} onClose={actions.closeSettings} onUserUpdated={actions.refreshIdentity} onNavigate={navigateWork} /> : null}
    <Modal open={historyOpen} title={t('work.allHistory')} closeLabel={t('work.closeHistory')} onClose={actions.closeHistory} className={css.historyDialog ?? ''}>
      <input className={css.historySearch} aria-label={t('work.searchHistory')} placeholder={t('work.searchHistory')} value={query} onChange={event => setQuery(event.target.value)} />
      <div className={css.historyList}>{sessions.filter(row => row.displayTitle.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(row => <button type="button" key={row.id} onClick={() => { openWorkSession(row.id); actions.closeHistory() }}><strong>{row.displayTitle}</strong><small>{new Date(row.updatedAt).toLocaleDateString()}</small></button>)}</div>
      {sessions.length === 0 ? <p>{t('work.noHistory')}</p> : null}
    </Modal>
  </div>
}

/** Open shared settings without leaving the work application. */
export function PlatformSettingsLink({ wide, actions, t }: PropsRuntime<'sidebar.settings'> & PropsLocale<'bananaPanel'> & ShellStore) {
  return <button type="button" className={css.sidebarSettings} onClick={() => actions.openSettings('general')} aria-label={t('work.platformSettings')} title={t('work.platformSettings')}>
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden><path d="m10 3-.7 2.2-2 .9L5 5.5 3 9l1.6 1.7v2.6L3 15l2 3.5 2.3-.6 2 .9L10 21h4l.7-2.2 2-.9 2.3.6 2-3.5-1.6-1.7v-2.6L21 9l-2-3.5-2.3.6-2-.9L14 3Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.5"/></svg>
    {wide ? <span>{t('work.settings')}</span> : null}
  </button>
}

/** Only the current work Host catalog supplies this history entry. */
export function PlatformHistoryLink({ wide, actions, t }: PropsRuntime<'sidebar.footer.action'> & PropsLocale<'bananaPanel'> & ShellStore) {
  return <button type="button" className={css.sidebarSettings} onClick={actions.showHistory} aria-label={t('work.allHistory')} title={t('work.allHistory')}>
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden><path d="M4 5h16v12H9l-5 4V5Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/><path d="M8 9h8M8 13h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg>
    {wide ? <span>{t('work.allHistory')}</span> : null}
  </button>
}
