/** Optional composer access to the conversation's current requirements. */
import { useId, useRef } from 'react'
import type { InjectFace, PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { isGenerationWorkSession } from './generation-settings.ts'
import type { createBananaPanelStore } from './store.ts'
import type { PreferencesSnapshot } from './presentation-preferences.ts'
import { CurrentRequirementsSummary } from './CurrentRequirementsSummary.tsx'
import { ResearchReuse } from './ResearchReuse.tsx'
import css from './PresentationPreferences.module.css'

export interface PreferencesInjected {
  hooks: { preferences: ObservableSnapshot<PreferencesSnapshot> }
  editPreferences(patch: Partial<Pick<PreferencesSnapshot, 'taskMode' | 'creationKind' | 'fixed' | 'countDraft' | 'output' | 'language'>>): void
  chooseTask(taskMode: PreferencesSnapshot['taskMode'], creationKind?: PreferencesSnapshot['creationKind']): Promise<void>
  savePreferences(): Promise<void>
  reloadPreferences(): Promise<void>
  refreshPreferences(): Promise<void>
}
type Props = PropsRuntime<'conversation.input.left'> & PropsLocale<'bananaPanel'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & InjectFace<PreferencesInjected>

/** Viewing requirements never selects a mode or starts production. */
export function PresentationPreferences({ sessionId, useSessions, usePreferences, reloadPreferences, refreshPreferences, t }: Props) {
  const popoverId = useId(), panel = useRef<HTMLDivElement>(null)
  const work = useSessions(s => isGenerationWorkSession(s.byId[sessionId]))
  const state = usePreferences(s => s)
  if (!work) return null
  return <div className={css.root} data-wuyu-presentation-preferences>
    <button type="button" className={css.chip} {...{ popovertarget: popoverId }} aria-haspopup="dialog" title={t('current.title')} onClick={event => {
      if (!panel.current) return
      void refreshPreferences().catch(() => { /* A readback failure preserves the displayed record. */ })
      const bounds = event.currentTarget.getBoundingClientRect(), width = Math.min(360, window.innerWidth - 24)
      panel.current.style.left = `${Math.max(12, Math.min(bounds.left, window.innerWidth - width - 12))}px`
      panel.current.style.bottom = `${Math.max(12, window.innerHeight - bounds.top + 8)}px`
      panel.current.style.maxHeight = `${Math.max(160, bounds.top - 20)}px`
    }}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 4h14v16H5ZM8 8h8M8 12h8M8 16h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>{t('current.title')}</button>
    <div id={popoverId} ref={panel} {...{ popover: 'auto' }} role="dialog" aria-label={t('current.title')} className={css.panel}>
      <header className={css.closeBar}><button type="button" {...{ popovertarget: popoverId, popovertargetaction: 'hide' }} aria-label={t('preferences.close')}>×</button></header>
      <CurrentRequirementsSummary record={state.currentRequirements} status={state.status} onReload={() => { void reloadPreferences() }} t={t} />
      {state.currentRequirements?.binding ? <ResearchReuse key={state.currentRequirements.binding.project_id} projectId={state.currentRequirements.binding.project_id} requirementsRevision={state.revision} t={t} /> : null}
    </div>
  </div>
}
