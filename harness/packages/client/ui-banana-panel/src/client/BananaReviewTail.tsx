/** Human decisions remain visible beneath the answer while raw Tool details fold. */
import { useState } from 'react'
import type { PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { PreviewTarget } from './preview-target.ts'
import type { ReviewCard } from './turn-review-decisions.ts'
import css from './BananaPanelRow.module.css'

/** Existing review operations; opening a card grants no workflow permission. */
export interface ReviewActions {
  openPanel(handle: string, projectId?: string, target?: PreviewTarget): void
  openTemplates(handle: string, projectId?: string): void
  writeReviewPrompt(text: string): Promise<void>
  savePersonalTemplate(projectId: string, revision: number, title: string): Promise<void>
}

type ReviewTailProps = PropsRuntime<'conversation.chat.turnTail'>
  & PropsLocale<'bananaPanel'> & ReviewActions & { matched: readonly ReviewCard[] }

function ReviewCardView({ card, t, openPanel, openTemplates, writeReviewPrompt, savePersonalTemplate }:
  Pick<ReviewTailProps, 't'> & ReviewActions & { card: ReviewCard }) {
  const { decision, handle, projectId } = card
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false), [saved, setSaved] = useState(false)
  const [templateName, setTemplateName] = useState(() => t('review.templateName'))
  if (decision.kind === 'template') return (
    <section className={css.reviewCard} aria-label={t('review.templateTitle')}>
      <strong>{t('review.templateTitle')}</strong><p>{t('review.templateHint')}</p>
      <button type="button" className={css.reviewAction} onClick={() => openTemplates(handle, projectId)}>
        {t('review.openTemplates')}<span aria-hidden="true"> ↗</span>
      </button>
    </section>
  )
  if (decision.kind === 'save') return projectId ? (
    <section className={css.reviewCard} aria-label={t('review.saveTitle')}>
      <strong>{t('review.saveTitle')}</strong><p>{t('review.saveHint')}</p>
      <form onSubmit={event => {
        event.preventDefault()
        if (saving || saved) return
        setSaving(true)
        void savePersonalTemplate(projectId, decision.revision, templateName)
          .then(() => { setSaved(true); setNotice(t('review.saved')) })
          .catch(() => setNotice(t('review.saveError'))).finally(() => setSaving(false))
      }}>
        <label>{t('review.templateLabel')} <input value={templateName} maxLength={200} required
          disabled={saving || saved} onChange={event => setTemplateName(event.target.value)} /></label>{' '}
        <button type="submit" className={css.reviewAction} disabled={saving || saved || !templateName.trim()}>
          {t(saved ? 'review.savedButton' : saving ? 'templateDecision.confirming' : 'review.saveButton')}
        </button>
      </form>{notice ? <p role="status">{notice}</p> : null}
    </section>
  ) : null
  return (
    <section className={css.reviewCard} aria-label={t('review.imagesTitle')}>
      <strong>{t('review.imagesTitle')}</strong><p>{t('review.imagesHint')}</p>
      <div className={css.reviewImages}>{decision.previews.slice(0, 4).map(page => (
        <button type="button" key={page.number} aria-label={page.label}
          onClick={() => openPanel(handle, projectId, { view: 'slides', page_number: page.number })}>
          <img src={page.url} alt={page.label} loading="lazy" /><span>{page.label}</span>
        </button>
      ))}</div>
      <div className={css.reviewActions}>
        <button type="button" className={css.reviewAction} onClick={() => openPanel(handle, projectId, { view: 'slides' })}>
          {t('review.openAll', { count: decision.previews.length })}
        </button>
        <button type="button" className={css.reviewAction} onClick={() => {
          void writeReviewPrompt(t('review.approvalPrompt') + '\n<wuyu-image-approval>'
            + JSON.stringify({ expected_revision: decision.revision, image_versions: decision.versions })
            + '</wuyu-image-approval>')
            .then(() => setNotice(t('review.prepared'))).catch(() => setNotice(t('review.error')))
        }}>{t('review.confirm')}</button>
      </div>{notice ? <p role="status">{notice}</p> : null}
    </section>
  )
}

/** Render validated decisions once outside the Tool-process disclosure. */
export function BananaReviewTail({ matched, ...props }: ReviewTailProps) {
  return <div>{matched.map(card => <ReviewCardView key={card.handle + ':' + card.seq} card={card} {...props} />)}</div>
}
