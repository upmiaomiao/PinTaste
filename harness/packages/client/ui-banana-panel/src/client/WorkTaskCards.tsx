/** Recommended work prompts only edit the unsent draft. Requirements remain conversation-owned. */
import { useRef } from 'react'
import type { PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { isGenerationWorkSession } from './generation-settings.ts'
import css from './WorkTaskCards.module.css'

type Props = PropsRuntime<'conversation.hero.suggestions'> & PropsLocale<'bananaPanel'>
const TASKS = ['deck', 'pages', 'figure'] as const
const ICON_PATHS = {
  deck: 'M3 4h18v12H3ZM8 20h8M12 16v4M7 8h10M7 12h6',
  pages: 'M7 3h12v14H7ZM4 7v14h12M10 8h6M10 12h4',
  figure: 'M2 9h5v6H2Zm8 0h5v6h-5Zm8 0h5v6h-5ZM7 12h3m-2-2 2 2-2 2M15 12h3m-2-2 2 2-2 2',
} as const

/**
 * Render optional tasks without submitting or changing attachments.
 * @param props - optional Session input, availability and localized task copy.
 * @returns the three starting buttons for the empty work composer, or null for other presets.
 */
export function WorkTaskCards({ sessionId, useSessions, useInput, inputActions, disabled, onRequestWorkspace, t }: Props) {
  const work = useSessions(state => sessionId === undefined || isGenerationWorkSession(state.byId[sessionId]))
  const input = useInput(state => state)
  const root = useRef<HTMLElement>(null)
  if (!work) return null
  const editing = input !== undefined && input.phase !== 'plain'
  const available = !disabled && inputActions !== undefined && input !== undefined && !editing
  const choose = (prompt: string) => {
    if (!available) { if (!editing) onRequestWorkspace?.(); return }
    const draft = input.draft
    inputActions.setDraft(draft ? draft + '\n\n' + prompt : prompt)
    root.current?.closest('[data-composer-seat]')?.querySelector<HTMLElement>(
      '[role="textbox"][contenteditable="true"]:not([aria-disabled="true"]),textarea:not(:disabled)',
    )?.focus({ preventScroll: true })
  }
  return <section ref={root} className={css.root} aria-label={t('workTasks.label')}>
    <p className={css.hint}>{t('current.startHint')}</p>
    <div className={css.grid}>{TASKS.map((task, index) => <button type="button" key={task}
      className={css.card} disabled={!available && (editing || onRequestWorkspace === undefined)}
      onClick={() => choose(t(`workTasks.${task}.prompt`))}>
      <span className={css.icon} data-tone={index} aria-hidden="true"><svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d={ICON_PATHS[task]} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
      <strong>{t(`workTasks.${task}.title`)}</strong><span className={css.description}>{t(`workTasks.${task}.description`)}</span>
    </button>)}</div>
  </section>
}
