/** Read-only presentation of the conversation's acknowledged requirements. */
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import type { CurrentRequirements, RequirementField, RequirementValues } from '../presentation-config.ts'
import { defaultRequirementValues } from '../presentation-config.ts'
import css from './CurrentRequirementsSummary.module.css'

export interface CurrentRequirementsSummaryProps {
  record: CurrentRequirements | null
  status: 'loading' | 'ready' | 'dirty' | 'saving' | 'error' | 'conflict'
  compact?: boolean
  onReload?: () => void
  t: PropsLocale<'bananaPanel'>['t']
}
const primary = ['task_mode', 'page_count', 'language', 'style'] as const
const additional = ['topic', 'audience', 'goal', 'content_policy', 'creation_kind', 'search_mode', 'requirements'] as const

/** Values remain automatic until the user, source document or plan supplies them. */
export function CurrentRequirementsSummary({ record, status, compact = false, onReload, t }: CurrentRequirementsSummaryProps) {
  const values = record?.values ?? defaultRequirementValues()
  const display = (field: RequirementField): string => {
    const value = values[field]
    if (field === 'page_count') return value === null ? t('current.autoPages') : t('current.pages', { count: String(value) })
    if (field === 'task_mode') return t(value === 'create' ? 'current.create' : value === 'relayout' ? 'current.materials' : 'current.autoTask')
    if (field === 'language') return t(value === 'zh' ? 'preferences.chinese' : value === 'en' ? 'preferences.english' : 'current.autoLanguage')
    if (field === 'creation_kind') return t(value === 'deck' ? 'workTasks.deck.title' : value === 'pages' ? 'workTasks.pages.title' : value === 'asset' ? 'workTasks.figure.title' : 'current.autoKind')
    if (field === 'content_policy') return t(value === 'preserve' ? 'current.preserve' : value === 'reorganize' ? 'current.reorganize' : 'current.autoContent')
    if (field === 'search_mode') return t(value === 'web' ? 'search.web' : value === 'deep' ? 'search.deep' : 'current.searchOff')
    return typeof value === 'string' && value.trim() ? value : t('current.unspecified')
  }
  const rows = (fields: readonly (keyof RequirementValues)[]) => <dl className={css.fields}>{fields.map(field => <div key={field}>
    <dt>{t(`current.field.${field}`)}</dt><dd><span>{display(field)}</span><small>{t(`current.source.${record?.sources[field] ?? 'default'}`)}</small></dd>
  </div>)}</dl>
  const stateKey = status === 'error' || status === 'conflict' ? 'current.readFailed' : status === 'loading' ? 'preferences.loading' : status === 'saving' ? 'preferences.saving' : record?.projection_status === 'conflict' ? 'current.projectionConflict' : record?.projection_status === 'pending' ? 'current.projectionPending' : record?.projection_status === 'applied' ? 'current.applied' : 'current.recorded'
  return <section className={css.root} data-current-requirements aria-label={t('current.title')}>
    <header><strong>{t('current.title')}</strong><span role="status" aria-live="polite">{t(stateKey)}</span></header>
    <p className={css.hint}>{t('current.chatHint')}</p>
    {rows(primary)}
    {compact ? <details className={css.more}><summary>{t('current.more')}</summary>{rows(additional)}</details> : rows(additional)}
    {(status === 'error' || status === 'conflict') && onReload ? <button type="button" className={css.reload} onClick={onReload}>{t('preferences.reload')}</button> : null}
  </section>
}
