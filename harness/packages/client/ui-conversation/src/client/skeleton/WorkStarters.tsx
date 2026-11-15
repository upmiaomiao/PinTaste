import { useId } from 'react'
import type { ConversationSlotProps } from '../contract/slots.ts'
import css from './WorkStarters.module.css'

const suggestions = [
  { kind: 'academic', actions: ['academic', 'defense'], icon: 'M5 3h14v17H5z M8 7h8M8 11h8M8 15h5' },
  { kind: 'business', actions: ['business', 'award'], icon: 'M4 21V5h11v16M15 11h5v10M8 9h3M8 13h3M8 17h3M2 21h20' },
  { kind: 'focused', actions: ['focused', 'asset'], icon: 'M3 5h15v12H3z M7 21h14V9 M7 9h7M7 13h4' },
] as const

/** Suggestions write an editable draft; they never start generation or approve a page. */
export function WorkStarters({ t, onChoose, disabled }: {
  t: ConversationSlotProps['t']
  onChoose: (text: string) => void
  disabled: boolean
}) {
  const id = useId()
  return (
    <section className={css.root} aria-labelledby={id}>
      <div className={css.heading}><h2 id={id}>{t('work.starters.title')}</h2><p>{t('work.starters.editHint')}</p></div>
      <div className={css.cards}>
        {suggestions.map(({ kind, actions, icon }) => (
          <article key={kind} aria-labelledby={id + '-' + kind}>
            <div className={css.cardHeading}>
              <span className={css.icon} aria-hidden="true"><svg viewBox="0 0 24 24" width="22" height="22" fill="none"><path d={icon} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg></span>
              <div><h3 id={id + '-' + kind}>{t(`work.${kind}.title`)}</h3><p>{t(`work.${kind}.description`)}</p></div>
            </div>
            <div className={css.actions}>{actions.map(action => <button key={action} type="button" disabled={disabled} onClick={() => onChoose(t(`work.${action}.prompt`))}>
              <span><strong>{t(`work.${action}.action`)}</strong><small>{t(`work.${action}.detail`)}</small></span>
              <span className={css.arrow} aria-hidden="true">↗</span>
            </button>)}</div>
          </article>
        ))}
      </div>
      <p className={css.hint}><svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden><path d="M7 12a5 5 0 1 1 6 0v2H7v-2Zm1 5h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>{t('work.reviewHint')}</p>
    </section>
  )
}
