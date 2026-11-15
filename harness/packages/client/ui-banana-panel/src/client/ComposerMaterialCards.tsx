/** Compact file cards describe the staged batch without starting model work. */
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { MaterialUploadsInjected } from './ComposerMaterials.tsx'
import css from './ComposerMaterialCards.module.css'
import { MaterialFileIcon } from './MaterialFileIcon.tsx'

type Props = PropsRuntime<'conversation.input.materials'> & PropsLocale<'bananaPanel'> & InjectFace<MaterialUploadsInjected>

/** Confirmation remains explicit even when files are added in several gestures. */
export function ComposerMaterialCards({ useMaterialUploads, confirmUploads, removeUpload, t }: Props) {
  const state = useMaterialUploads(value => value)
  const visible = state.rows.filter(row => !row.sent && row.phase !== 'attached')
  if (!visible.length) return state.rows.some(row => !row.sent) ? <p className={css.remaining} role="status">{t('upload.attached')}</p> : null
  const pending = state.rows.filter(row => row.phase === 'staged').length
  const invalid = state.rows.some(row => !row.submitted && row.phase === 'failed')
  const phases = { staged:'upload.staged', queued:'upload.queued', uploading:'upload.sending', registering:'upload.registering', uploaded:'upload.uploaded', attached:'upload.attached', failed:'upload.failed', paused:'upload.paused' } as const
  return <section className={css.materials} aria-label={t('upload.selectedCards')}>
    <div className={css.cards}>{visible.slice(0, 3).map(row => <div className={css.card} key={row.id}>
      <MaterialFileIcon filename={row.filename}/>
      <div><strong title={row.filename}>{row.filename}</strong><small>{row.size < 1048576 ? t('upload.sizeKb', { size:Math.ceil(row.size / 1024) }) : t('upload.sizeMb', { size:(row.size / 1048576).toFixed(1) })} · {t(row.sending ? 'upload.submitting' : row.sent ? 'upload.sent' : phases[row.phase])}{row.phase === 'uploading' ? ' ' + row.percent + '%' : ''}</small></div>
      {!row.submitted ? <button type="button" aria-label={t('upload.removeFile', { filename:row.filename })} onClick={() => removeUpload(row.id)}>×</button> : null}
    </div>)}</div>
    {visible.length > 3 ? <p className={css.remaining}>{t('upload.moreInList', { count:visible.length - 3 })}</p> : null}
    {pending ? <div className={css.confirmation}><span>{t('upload.localUntilConfirmed')}</span><button type="button" disabled={state.busy || state.batchActive || state.intaking || invalid} onClick={() => void confirmUploads()}>{t('upload.confirmCards', { count:pending })}</button></div> : null}
  </section>
}
