/** Vertical, neutral-only template choices; picking a card never starts generation. */
import { useEffect, useRef, useState } from 'react'
import type { InjectFace, PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { createBananaPanelStore } from './store.ts'
import { isGenerationWorkSession } from './generation-settings.ts'
import { templateAxes, type NeutralTemplateCard, type TemplateFilters, type TemplateLibrarySnapshot, type TemplateTags } from './template-library.ts'
import css from './TemplateLibraryChooser.module.css'
import {TEMPLATE_PAGE_SIZE,TemplateBrowserPagination,templatePage,useTemplateBrowser,useTemplateScroll} from './template-browser.tsx'

/** Session-scoped callbacks are registered by the client entry point. */
export interface TemplateLibraryChooserInjected {
  hooks: { templateLibrary: ObservableSnapshot<TemplateLibrarySnapshot> }
  loadTemplateLibrary(filters?: TemplateFilters, tab?: TemplateLibrarySnapshot['tab']): Promise<void>
  inspectTemplate(id: string): Promise<void>
  chooseTemplate(card: NeutralTemplateCard | null): void
  loadTemplateMaterials(handle: string | null): Promise<void>
  submitTemplate(materialId: string): Promise<void>
  tagTemplate(id: string): Promise<void>
  reviewTemplate(id: string, decision: 'approve' | 'reject', reason: string, tags?: TemplateTags): Promise<void>
}
type Props = PropsRuntime<'wuyu.work.templates'> & PropsLocale<'bananaPanel'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & InjectFace<TemplateLibraryChooserInjected>

/** A chooser and the private-to-reviewed sharing flow, styled with platform tokens. */
export function TemplateLibraryChooser({ sessionId, useSessions, useStore, useTemplateLibrary, loadTemplateLibrary, inspectTemplate, chooseTemplate, loadTemplateMaterials, submitTemplate, tagTemplate, reviewTemplate, t }: Props) {
  const work = useSessions(s => isGenerationWorkSession(s.byId[sessionId]))
  const handle = useStore(s => s.projectHandle)
  const snapshot = useTemplateLibrary(s => s)
  const [open, setOpen] = useState(false), [query, setQuery] = useState(''), [reason, setReason] = useState(''), [correcting, setCorrecting] = useState(false), [correctedTags, setCorrectedTags] = useState<TemplateTags | null>(null)
  const scroll = useTemplateScroll<HTMLDivElement>(handle??'')
  const root = scroll.root, trigger = useRef<HTMLButtonElement>(null), dialog = useRef<HTMLElement>(null)
  const initialTab = useRef<TemplateLibrarySnapshot['tab'] | undefined>(undefined), linked = useRef(false)
  const detail = snapshot.detail
  useEffect(() => {
    if (!open) return
    void loadTemplateLibrary({...snapshot.filters,offset:templatePage(snapshot.total,snapshot.filters.offset).offset,limit:TEMPLATE_PAGE_SIZE}, initialTab.current)
    initialTab.current = undefined
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const keyboard = (event: KeyboardEvent) => {
      if (event.isComposing) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        setOpen(false)
        trigger.current?.focus()
        return
      }
      if (event.key !== 'Tab' || !dialog.current) return
      const controls = [...dialog.current.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex]')].filter(element => element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length > 0)
      const first = controls[0], last = controls.at(-1)
      const current = document.activeElement
      if (!first || !controls.includes(current as HTMLElement) || event.shiftKey && current === first || !event.shiftKey && current === last) {
        event.preventDefault()
        event.stopPropagation()
        ;(event.shiftKey ? last ?? dialog.current : first ?? dialog.current).focus()
      }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', keyboard, true)
    root.current?.querySelector<HTMLInputElement>('input[type=search]')?.focus()
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', keyboard, true)
    }
  }, [open, loadTemplateLibrary])
  useEffect(() => {
    if (linked.current || !work) return
    linked.current = true
    if (new URL(location.href).searchParams.get('templates') === 'review') {
      initialTab.current = 'review'
      setOpen(true)
    }
  }, [work])
  useEffect(() => { setReason(''); setCorrecting(false); setCorrectedTags(null) }, [detail?.id])
  useEffect(() => { if (open && snapshot.tab !== 'library') void loadTemplateMaterials(handle) }, [open, snapshot.tab, handle, loadTemplateMaterials])
  const browser = useTemplateBrowser(snapshot,open&&work,loadTemplateLibrary)
  if (!work) return null
  const close = () => { setOpen(false); trigger.current?.focus() }
  const statusLabel = (card: NeutralTemplateCard) => t(card.review_status === 'approved' ? 'templateLibrary.approved' : card.review_status === 'rejected' ? 'templateLibrary.rejected' : 'templateLibrary.pending')
  const useTemplate = () => { if (detail?.review_status === 'approved') { chooseTemplate(detail); close() } }
  const setFilter = (key: keyof TemplateFilters, value: string) => { scroll.capture();void loadTemplateLibrary({ ...snapshot.filters, [key]: value, offset: 0, limit: TEMPLATE_PAGE_SIZE }) }
  return <div className={css.root} ref={root} onScrollCapture={scroll.onScrollCapture}>
    <button type="button" ref={trigger} className={css.trigger} aria-label={t('templateLibrary.label')} aria-expanded={open} aria-haspopup="dialog" data-selected={snapshot.selected ? true : undefined} onClick={() => setOpen(value => !value)}>
      <svg width="17" height="17" viewBox="0 0 20 20" fill="none" aria-hidden><rect x="2.5" y="3" width="15" height="14" rx="2" stroke="currentColor" strokeWidth="1.4"/><path d="M3 7h14M8 7v10" stroke="currentColor" strokeWidth="1.4"/></svg>
      <span>{snapshot.selected?.title ?? t('templateLibrary.label')}</span><span aria-hidden>⌄</span>
    </button>
    {open ? <section ref={dialog} className={css.dialog} role="dialog" aria-modal="true" tabIndex={-1} aria-label={t('templateLibrary.title')} aria-busy={snapshot.busy}>
      <header className={css.header}><div><strong>{t('templateLibrary.title')}</strong><p>{t('templateLibrary.choiceHint')}</p></div><button type="button" className={css.iconButton} aria-label={t('templateLibrary.close')} onClick={close}>×</button></header>
      <nav className={css.tabs} aria-label={t('templateLibrary.title')}>
        {(['library', 'mine', ...(snapshot.admin ? ['review'] : [])] as TemplateLibrarySnapshot['tab'][]).map(tab => <button type="button" key={tab} aria-pressed={snapshot.tab === tab} onClick={() => { scroll.capture();void loadTemplateLibrary({offset:0,limit:TEMPLATE_PAGE_SIZE}, tab) }}>{t(tab === 'library' ? 'templateLibrary.library' : tab === 'mine' ? 'templateLibrary.mine' : 'templateLibrary.review')}</button>)}
      </nav>
      <div className={css.filters}>
        <form onSubmit={event => { event.preventDefault(); setFilter('search', query) }}><input type="search" aria-label={t('templateLibrary.search')} placeholder={t('templateLibrary.search')} maxLength={200} value={query} onChange={event => setQuery(event.target.value)}/><button type="submit" aria-label={t('templateLibrary.search')}>⌕</button></form>
        {templateAxes.map(axis => <label key={axis}>{t(`templateLibrary.${axis}`)}<select value={snapshot.filters[axis] ?? ''} onChange={event => setFilter(axis, event.target.value)}><option value="">{t('templateLibrary.all')}</option>{snapshot.facets[axis].map(value => <option key={value} value={value}>{value}</option>)}</select></label>)}
      </div>
      {snapshot.tab !== 'library' ? <div className={css.uploads}><p>{t('templateLibrary.uploadHint')}</p><button type="button" disabled={snapshot.busy || !handle} onClick={() => { void loadTemplateMaterials(handle) }}>{t('templateLibrary.refreshMaterials')}</button>{snapshot.materials.map(material => <div key={material.id} className={css.material}><span title={material.filename}>{material.filename}</span><button type="button" disabled={snapshot.busy || !material.ready} onClick={() => { void submitTemplate(material.id) }}>{t(material.ready ? 'templateLibrary.submit' : 'templateLibrary.parsing')}</button></div>)}</div> : null}
      {snapshot.actionError || snapshot.phase === 'error' ? <p className={css.error} role="alert">{t('templateLibrary.unavailable')} <button type="button" onClick={() => { scroll.capture();void loadTemplateLibrary({...snapshot.filters,limit:TEMPLATE_PAGE_SIZE}) }}>{t('templateLibrary.retry')}</button></p> : null}
      <div className={css.columns}>
        <div id={browser.optionsId} className={css.options} aria-busy={snapshot.phase==='loading'}>
          {snapshot.tab === 'library' ? <button type="button" className={css.automatic} aria-pressed={snapshot.selected === null} onClick={() => { chooseTemplate(null); close() }}>{t('templateLibrary.automatic')}</button> : null}
          {snapshot.phase === 'loading' ? <p role="status">{t('templateLibrary.loading')}</p> : browser.cards.length === 0 ? <p className={css.note}>{t('templateLibrary.empty')}</p> : null}
          {browser.cards.map(card => <button type="button" data-template-card={card.id} key={card.id} className={css.card} aria-pressed={detail?.id === card.id} onClick={() => { scroll.capture();void inspectTemplate(card.id) }}>
            {card.cover_url ? <img src={card.cover_url} alt="" loading="lazy"/> : null}<span className={css.cardText}><strong>{card.title}</strong><span>{card.tags.purpose.join(' / ')}</span><small>{t('templateLibrary.referenceCount', { count: card.body_reference_count })}{!card.reference_sufficient ? ' · ' + t('templateLibrary.short') : ''}</small><span className={css.badges}>{card.tags.visual_style.slice(0, 2).map(tag => <i key={tag}>{tag}</i>)}{card.review_status !== 'approved' ? <i>{statusLabel(card)}</i> : null}{snapshot.selected?.id === card.id ? <i>{t('templateLibrary.selected')}</i> : null}</span></span>
          </button>)}
          <TemplateBrowserPagination page={browser.page} disabled={snapshot.busy||snapshot.phase==='loading'} controls={browser.optionsId} label={t('templateBrowser.pagination')} status={t('templateBrowser.page',{page:browser.page.page,pages:browser.page.pages,count:browser.page.total})} previous={t('templateDecision.previous')} next={t('templateDecision.next')} onPage={offset=>{scroll.capture();void loadTemplateLibrary({...snapshot.filters,offset,limit:TEMPLATE_PAGE_SIZE})}}/>
        </div>
        <div className={css.preview}>
          {detail ? <><div className={css.previewHeader}><div><h3>{detail.title}</h3><span>{t('templateLibrary.referenceCount', { count: detail.body_reference_count })} · {statusLabel(detail)}</span></div>{detail.review_status === 'approved' ? <button type="button" className={css.primary} disabled={snapshot.busy} onClick={useTemplate}>{t('templateLibrary.choose')}</button> : null}</div><p className={css.note}>{detail.preview_disclaimer}</p>{detail.warnings.map(warning => <p className={css.warning} key={warning}>{warning}</p>)}<div className={css.thumbnails}>{detail.pages?.map(page => <figure key={page.id}><img src={page.image_url} alt={page.title} loading="lazy"/><figcaption>{page.title}</figcaption></figure>)}</div>
            {detail.review_reason ? <p className={css.note}>{detail.review_reason}</p> : null}
            {snapshot.admin && detail.review_status === 'pending' ? <div className={css.review}><p>{t('templateLibrary.tagHint')}</p><div className={css.actions}><button type="button" disabled={snapshot.busy} onClick={() => { void tagTemplate(detail.id) }}>{t('templateLibrary.tag')}</button><button type="button" disabled={snapshot.busy} onClick={() => { setCorrecting(value => !value); setCorrectedTags(structuredClone(detail.tags)) }}>{t('templateLibrary.correctTags')}</button></div>{correcting && correctedTags ? <div className={css.tagEditor}>{templateAxes.map(axis => <fieldset key={axis}><legend>{t(`templateLibrary.${axis}`)}</legend>{snapshot.facets[axis].map(tag => <label key={tag}><input type="checkbox" checked={correctedTags[axis].includes(tag)} onChange={event => setCorrectedTags(previous => previous ? { ...previous, [axis]: event.target.checked ? [...previous[axis], tag] : previous[axis].filter(value => value !== tag) } : previous)}/>{tag}</label>)}</fieldset>)}</div> : null}<label className={css.reason}>{t('templateLibrary.reason')}<textarea maxLength={500} value={reason} onChange={event => setReason(event.target.value)}/></label>{detail.tagging_status !== 'completed' && !correcting ? <p className={css.note}>{t('templateLibrary.tagRequired')}</p> : null}<div className={css.actions}><button type="button" className={css.primary} disabled={snapshot.busy || detail.tagging_status !== 'completed' && !correcting || correcting && !correctedTags?.purpose.length} onClick={() => { void reviewTemplate(detail.id, 'approve', reason, correcting ? correctedTags ?? undefined : undefined) }}>{t('templateLibrary.approve')}</button><button type="button" disabled={snapshot.busy} onClick={() => { void reviewTemplate(detail.id, 'reject', reason) }}>{t('templateLibrary.reject')}</button></div></div> : null}
          </> : <div className={css.emptyPreview}><svg width="52" height="52" viewBox="0 0 52 52" fill="none" aria-hidden><rect x="5" y="8" width="42" height="34" rx="5" stroke="currentColor" strokeWidth="1.8"/><path d="M6 18h40M20 18v23" stroke="currentColor" strokeWidth="1.8"/></svg><p>{t('templateLibrary.inspect')}</p><small>{t('templateLibrary.neutral')}</small></div>}
        </div>
      </div>
    </section> : null}
  </div>
}
