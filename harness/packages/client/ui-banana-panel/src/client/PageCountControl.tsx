/** PPT page count beside the composer; changing it never sends a message. */
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { isGenerationWorkSession } from './generation-settings.ts'
import { selectedPageCount, type PageCountSnapshot } from './page-count.ts'
import css from './PageCountControl.module.css'

interface PageCountInjected {
  hooks: { pageCount: ObservableSnapshot<PageCountSnapshot> }
  choosePageCount(fixed: boolean): void
  editPageCount(value: string): void
}
type Props = PropsRuntime<'conversation.input.left'> & PropsLocale<'bananaPanel'> & InjectFace<PageCountInjected>

/** Render a Session-owned choice with the same admission rules as its submission modifier. */
export function PageCountControl({ sessionId, useSessions, useInput, usePageCount, choosePageCount, editPageCount, t }: Props) {
  const work = useSessions(state => isGenerationWorkSession(state.byId[sessionId]))
  const busy = useInput(state => state.phase !== 'plain')
  const selection = usePageCount(state => state)
  let invalid = false
  try { selectedPageCount(selection) } catch { invalid = true }
  if (!work) return null
  const hintId = 'wuyu-page-count-hint-' + sessionId
  return <div className={css.root} role="group" aria-label={t('pageCount.label')} data-wuyu-page-count>
    <label className={css.chip} title={t('pageCount.hint')}>
      <span>{t('pageCount.label')}</span>
      <select aria-label={t('pageCount.mode')} disabled={busy} value={selection.fixed ? 'fixed' : 'auto'} onChange={event => choosePageCount(event.target.value === 'fixed')}>
        <option value="auto">{t('pageCount.auto')}</option>
        <option value="fixed">{t('pageCount.fixed')}</option>
      </select>
    </label>
    {selection.fixed ? <label className={css.number} title={t('pageCount.hint')}>
      <input type="number" min="1" max="80" step="1" inputMode="numeric" disabled={busy}
        aria-label={t('pageCount.number')} aria-invalid={invalid} aria-describedby={hintId}
        value={selection.draft} onChange={event => editPageCount(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation() } }} />
      <span>{t('pageCount.unit')}</span>
    </label> : null}
    <span id={hintId} className={css.hint} role={invalid ? 'status' : undefined}>{t(invalid ? 'pageCount.invalid' : 'pageCount.hint')}</span>
  </div>
}
