/** Wuyu work composer choices; all actions only update the next submission. */
import { useEffect, useRef, useState } from 'react'
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { GenerationSnapshot } from './generation-controller.ts'
import type { GenerationSettings } from './generation-settings.ts'
import { isGenerationWorkSession } from './generation-settings.ts'
import css from './GenerationControls.module.css'

/** Callbacks and the Session-owned source are bound by the plugin registration. */
export interface GenerationControlsInjected {
  hooks: { generation: ObservableSnapshot<GenerationSnapshot> }
  changeGeneration(patch: Partial<GenerationSettings>): void
  loadGenerationOptions(): Promise<void>
}
type Props = PropsRuntime<'conversation.input.left'> & PropsLocale<'bananaPanel'> & InjectFace<GenerationControlsInjected>

/** Configures complete decks, selected pages and individual visual assets. */
export function GenerationControls({ sessionId, useSessions, useGeneration, changeGeneration, loadGenerationOptions, t }: Props) {
  const work = useSessions(s => isGenerationWorkSession(s.byId[sessionId]))
  const { settings, options, phase } = useGeneration(s => s)
  const [open, setOpen] = useState(false)
  const [pageCountDraft, setPageCountDraft] = useState(() => settings.page_count === null ? '' : String(settings.page_count))
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null)
  const commitPageCount = () => {
    if (settings.creation_kind === 'asset' || settings.page_count === null) { setPageCountDraft(settings.page_count === null ? '' : String(settings.page_count)); return }
    const count = pageCountDraft.trim() === '' ? Number.NaN : Number(pageCountDraft)
    if (!Number.isSafeInteger(count)) { setPageCountDraft(String(settings.page_count)); return }
    const bounded = Math.min(settings.creation_kind === 'pages' ? 8 : 80, Math.max(settings.creation_kind === 'pages' ? 1 : 2, count))
    setPageCountDraft(String(bounded))
    if (bounded !== settings.page_count) changeGeneration({ page_count: bounded })
  }
  const commitPageCountRef = useRef(commitPageCount)
  commitPageCountRef.current = commitPageCount
  useEffect(() => { setPageCountDraft(settings.page_count === null ? '' : String(settings.page_count)) }, [settings.creation_kind, settings.page_count])
  useEffect(() => { if (work) void loadGenerationOptions() }, [work, loadGenerationOptions])
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLElement>('[role=dialog] input:not(:disabled), [role=dialog] select')?.focus()
    const closeOutside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) { commitPageCountRef.current(); setOpen(false) } }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [open])
  if (!work) return null
  const profiles = options?.profiles ?? (['gpt', 'gemini'] as const).map(id => ({ id, label: t(id === 'gpt' ? 'generation.gpt' : 'generation.gemini'), available: false, model: '' }))
  const unverifiedProfile = settings.image_profile !== 'default' && (phase !== 'ready' || !profiles.some(profile => profile.id === settings.image_profile && profile.available))
  const modeLabel = settings.creation_kind === 'deck' ? t('generation.deck') : settings.creation_kind === 'pages' ? t('generation.pages') : t('generation.asset')
  return <div ref={root} className={css.controls} data-wuyu-generation-controls onKeyDown={event => { if (event.key === 'Escape' && open) { setOpen(false); trigger.current?.focus(); event.preventDefault() } }}>
    <label className={css.chip} data-selected>
      <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden><rect x="2.5" y="3" width="15" height="11" rx="2" stroke="currentColor" strokeWidth="1.4"/><path d="M7 17h6M10 14v3M6 7h8M6 10h5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/></svg>
      <select aria-label={t('generation.kind')} title={modeLabel} value={settings.creation_kind} onChange={event => changeGeneration({ creation_kind: event.target.value as GenerationSettings['creation_kind'] })}>
        <option value="deck">{t('generation.deck')}</option><option value="pages">{t('generation.pages')}</option><option value="asset">{t('generation.asset')}</option>
      </select><span className={css.chevron} aria-hidden>⌄</span>
    </label>
    <label className={css.chip} aria-busy={phase === 'loading'}>
      <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden><path d="m10 2 1.9 5.6L18 10l-6.1 2.4L10 18l-1.9-5.6L2 10l6.1-2.4L10 2Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/></svg>
      <select aria-label={t('generation.model')} value={settings.image_profile} onChange={event => changeGeneration({ image_profile: event.target.value as GenerationSettings['image_profile'] })}>
        <option value="default">{t('generation.modelDefault')}</option>
        {profiles.map(profile => <option key={profile.id} value={profile.id} disabled={phase !== 'ready' || !profile.available}>{profile.label}{phase !== 'ready' || !profile.available ? ' · ' + t('generation.unavailable') : ''}</option>)}
      </select><span className={css.chevron} aria-hidden>⌄</span>
    </label>
    <label className={css.chip}>
      <select aria-label={t('generation.output')} value={settings.output_preference} onChange={event => changeGeneration({ output_preference: event.target.value as GenerationSettings['output_preference'] })}>
        <option value="editable">{t('generation.editable')}</option><option value="image">{t('generation.image')}</option>
      </select><span className={css.chevron} aria-hidden>⌄</span>
    </label>
    <button ref={trigger} type="button" className={css.more} aria-label={t('generation.more')} aria-haspopup="dialog" aria-controls={'wuyu-generation-' + sessionId} aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden><path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/><path d="M7 3v4M13 8v4M8 13v4" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></svg>
      <span>{t('generation.settings')}</span>
    </button>
    {phase === 'error' || phase === 'ready' && unverifiedProfile ? <button type="button" className={css.retry} onClick={() => void loadGenerationOptions()}>{t('generation.retryModels')}</button> : null}
    {unverifiedProfile ? <span className={css.profileNotice} role="status">{t('generation.profileUnavailable')}</span> : null}
    {phase === 'loading' ? <span className={css.srOnly} role="status">{t('generation.loadingModels')}</span> : null}
    {open ? <div id={'wuyu-generation-' + sessionId} role="dialog" aria-label={t('generation.dialog')} className={css.popover} onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { commitPageCount(); event.preventDefault(); event.stopPropagation() } }} onBlur={event => { if (event.relatedTarget && !root.current?.contains(event.relatedTarget)) setOpen(false) }}>
      <header><strong>{t('generation.dialog')}</strong><span>{t('generation.aspect')}</span></header>
      {settings.creation_kind !== 'asset' ? <div className={css.field}>
        <label htmlFor={'wuyu-pages-' + sessionId}>{t('generation.pageCount')}</label>
        {settings.creation_kind === 'deck' ? <label className={css.check}><input type="checkbox" checked={settings.page_count === null} onChange={event => changeGeneration({ page_count: event.target.checked ? null : 12 })} />{t('generation.autoCount')}</label> : null}
        <input id={'wuyu-pages-' + sessionId} type="number" min={settings.creation_kind === 'pages' ? 1 : 2} max={settings.creation_kind === 'pages' ? 8 : 80} step="1" disabled={settings.page_count === null} value={pageCountDraft} onChange={event => setPageCountDraft(event.target.value)} onBlur={commitPageCount} />
      </div> : <label className={css.field}>{t('generation.assetKind')}<select value={settings.asset_kind} onChange={event => changeGeneration({ asset_kind: event.target.value as GenerationSettings['asset_kind'] })}><option value="diagram">{t('generation.diagram')}</option><option value="illustration">{t('generation.illustration')}</option><option value="icon">{t('generation.icon')}</option></select></label>}
      {settings.creation_kind === 'deck' ? <label className={css.field}>{t('generation.review')}<select value={settings.review_policy} onChange={event => changeGeneration({ review_policy: event.target.value as GenerationSettings['review_policy'] })}><option value="template_only">{t('generation.reviewTemplate')}</option><option value="template_and_final">{t('generation.reviewFinal')}</option><option value="guided">{t('generation.reviewGuided')}</option></select></label> : null}
      <p>{t(settings.creation_kind === 'deck' ? 'generation.deckHint' : 'generation.focusedHint')}</p>
      <p>{t(settings.output_preference === 'editable' ? 'generation.editableHint' : 'generation.imageHint')}</p>
    </div> : null}
  </div>
}
