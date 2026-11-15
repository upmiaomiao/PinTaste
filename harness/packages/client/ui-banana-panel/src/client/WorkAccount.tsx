/** Central display identity is separate from this work Host's conversation catalog. */
import { useEffect, useState } from 'react'
import type { InjectFace, PropsLocale, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { createWorkShellStore, WorkIdentity } from './work-shell.ts'
import css from './WorkUtilities.module.css'

export interface WorkAccountInjected { readIdentity(signal?: AbortSignal): Promise<WorkIdentity> }
type Props = PropsLocale<'bananaPanel'> & PropsStore<ReturnType<typeof createWorkShellStore>> & InjectFace<WorkAccountInjected>

/** Display the signed-in account using the same safe central profile as Q&A. */
export function WorkAccount({ readIdentity, useStore, actions, t }: Props) {
  const revision = useStore(state => state.identityRevision)
  const [identity, setIdentity] = useState<WorkIdentity | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    void readIdentity(controller.signal).then(value => { if (!controller.signal.aborted) setIdentity(value) }, () => {
      if (!controller.signal.aborted) setIdentity(null)
    })
    return () => controller.abort()
  }, [readIdentity, revision])
  const name = identity?.name || t('work.account')
  return <button type="button" className={css.account} data-wuyu-work-account="" aria-label={t('work.accountSettings', { name })} title={identity?.organization ? name + ' · ' + identity.organization : name} onClick={() => actions.openSettings('account')}>
    <span className={css.avatar} aria-hidden>
      <svg viewBox="0 0 24 24" width="21" height="21" fill="none"><circle cx="12" cy="8" r="3.5" stroke="currentColor" strokeWidth="1.6"/><path d="M5 20v-2a7 7 0 0 1 14 0v2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg>
    </span>
    <span className={css.accountText}><strong>{name}</strong>{identity?.organization ? <small>{identity.organization}</small> : null}</span>
  </button>
}
