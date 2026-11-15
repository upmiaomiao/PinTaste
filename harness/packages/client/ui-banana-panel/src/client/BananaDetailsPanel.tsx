/**
 * Right details column: hosts the official Banana Slides project panel for the
 * current Session's project. Reads the shared panel store (written by the
 * transcript's tool.call.toolview row) and renders the iframe host, or an
 * empty hint until a project is set. The header mirrors the DSH details-column
 * chrome (title + close) so the panel reads as a native right-side surface.
 */
import { useEffect, useRef } from 'react'
import type { InjectFace, PropsLocale, PropsStore, PropsRenderSlots } from '@deepseek-ai/dsh-client-ui-slots'
import { CurrentRequirementsSummary } from './CurrentRequirementsSummary.tsx'
import type { PreferencesInjected } from './PresentationPreferences.tsx'
import { BananaPanelHost } from './BananaPanelHost.tsx'
import type { createBananaPanelStore } from './store.ts'
import css from './BananaPanelRow.module.css'
type BananaDetailsPanelProps = PropsStore<ReturnType<typeof createBananaPanelStore>>
  & PropsLocale<'bananaPanel'>
  & PropsRenderSlots<'wuyu.work.templates'>
  & InjectFace<PreferencesInjected>
  & { closeDetails: () => void; writeToConversation:(text:string)=>Promise<void>; templateRequest:(handle:string,request:unknown)=>Promise<unknown> }
/** Right-column deck panel; renders an empty hint until a project is set. */
export function BananaDetailsPanel({ useStore, actions, t, closeDetails, writeToConversation, templateRequest, renderSlot, usePreferences, reloadPreferences, refreshPreferences }: BananaDetailsPanelProps) {
  const projectHandle = useStore(s => s.projectHandle)
  const requirements = usePreferences(s => s)
  const binding = requirements.currentRequirements?.binding
  useEffect(() => {
    if (projectHandle === null && binding) actions.setProject(binding.project_handle, binding.project_id)
  }, [projectHandle, binding?.project_handle, binding?.project_id, actions])
  const root = useRef<HTMLDivElement>(null), refresh = useRef(refreshPreferences)
  refresh.current = refreshPreferences
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible' && root.current?.getClientRects().length) void refresh.current().catch(() => { /* A background read preserves the last acknowledged view. */ })
    }, 4000)
    return () => clearInterval(timer)
  }, [])
  const previewTarget=useStore(s=>s.previewTarget),previewRequest=useStore(s=>s.previewRequest)
  const adminReview=new URL(location.href).searchParams.get('templates')==='review'
  return (
    <div ref={root} className={css.detailsRoot}>
      <div className={css.header}>
        <div className={css.title}>{t('details.title')}</div>
        <div className={css.headerRight}>
          <button type="button" className={css.close} aria-label={t('details.close')} onClick={closeDetails}>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
              <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>
      {!adminReview ? <div className={css.requirementsDock}><CurrentRequirementsSummary record={requirements.currentRequirements} status={requirements.status} compact={projectHandle !== null} onReload={() => { void reloadPreferences() }} t={t} /></div> : null}
      <div className={css.body}>
        {adminReview?renderSlot('wuyu.work.templates',{mode:'review'}):projectHandle === null
          ? <div className={css.detailsEmpty}>{t('details.empty')}</div>
          : <BananaPanelHost currentRequirements={requirements.currentRequirements} projectHandle={projectHandle} target={previewTarget} targetRevision={previewRequest} writeToConversation={writeToConversation} templateRequest={templateRequest} t={t} />}
      </div>
    </div>
  )
}
