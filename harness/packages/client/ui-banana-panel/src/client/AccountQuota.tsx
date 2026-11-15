/** Account balances and recent usage, independently visible on an empty work page. */
import { useEffect, useRef, useState } from 'react'
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { AccountView, QuotaEntry } from './account-quota.ts'
import { AccountReadError, RedeemError } from './account-quota.ts'
import css from './AccountQuota.module.css'

export interface AccountQuotaInjected {
  readAccount(signal?: AbortSignal): Promise<AccountView>
  readQuotaLedger(signal?: AbortSignal): Promise<QuotaEntry[]>
  redeemQuotaCode(code: string): Promise<{ points: number }>
}
type Props = Pick<PropsRuntime<'conversation.work.account'>, 'useSessions'> & PropsLocale<'bananaPanel'> & InjectFace<AccountQuotaInjected>

/** Presents verified account totals without exposing identity tokens. */
export function AccountQuota({ readAccount, readQuotaLedger, redeemQuotaCode, useSessions, t }: Props) {
  const running = useSessions(state => state.current === undefined ? false : state.byId[state.current]?.running === true)
  const [account, setAccount] = useState<AccountView | null>(null)
  const [entries, setEntries] = useState<QuotaEntry[] | null>(null)
  const [error, setError] = useState(0), [ledgerError, setLedgerError] = useState(false)
  const [open, setOpen] = useState(false), [revision, setRevision] = useState(0), [loading, setLoading] = useState(true)
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null)
  const [code, setCode] = useState(''), [redeeming, setRedeeming] = useState(false), [redeemNote, setRedeemNote] = useState(''), [redeemFailed, setRedeemFailed] = useState(false)
  const redeem = () => {
    if (redeeming || !code.trim()) return
    setRedeeming(true); setRedeemNote(''); setRedeemFailed(false)
    void redeemQuotaCode(code.trim()).then(result => {
      setRedeemNote(t('quota.redeemDone', { points: result.points })); setCode(''); setRevision(value => value + 1)
    }, failure => {
      setRedeemFailed(true)
      setRedeemNote(failure instanceof RedeemError && failure.message ? failure.message : t('quota.redeemFailed'))
    }).finally(() => setRedeeming(false))
  }
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(0)
    void readAccount(controller.signal).then(value => { if (!controller.signal.aborted) setAccount(value) }, failure => {
      if (!controller.signal.aborted) { setAccount(null); setError(failure instanceof AccountReadError ? failure.status : 503) }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [readAccount, running, revision])
  useEffect(() => {
    const refresh = () => { if (document.visibilityState !== 'hidden') setRevision(value => value + 1) }
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [])
  useEffect(() => {
    if (open) root.current?.querySelector<HTMLButtonElement>('[role="dialog"] button')?.focus()
  }, [open])
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setEntries(null); setLedgerError(false)
    void readQuotaLedger(controller.signal).then(value => { if (!controller.signal.aborted) setEntries(value) }, () => {
      if (!controller.signal.aborted) setLedgerError(true)
    })
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => { controller.abort(); document.removeEventListener('pointerdown', outside) }
  }, [open, readQuotaLedger, revision])
  const view = account?.quota
  const eventLabel = (event: string) => t(event === 'signup' ? 'quota.signupEvent' : event === 'migration' ? 'quota.migrationEvent' : event === 'grant' ? 'quota.grantEvent' : event === 'reserve' ? 'quota.reserveEvent' : event === 'settle' ? 'quota.settleEvent' : event === 'refund' ? 'quota.refundEvent' : event === 'redeem' ? 'quota.redeemEvent' : 'quota.adjustmentEvent')
  const entryAmount = (entry: QuotaEntry) => t('quota.entryAmount', { points: entry.event === 'settle' ? entry.charged_points : entry.points > 0 ? '+' + entry.points : entry.points, pages: entry.event === 'settle' ? entry.charged_pages : entry.pages > 0 ? '+' + entry.pages : entry.pages })
  return <div className={css.root} ref={root} onKeyDown={event => { if (event.key === 'Escape' && open) { setOpen(false); trigger.current?.focus(); event.preventDefault() } }}>
    <button className={css.trigger} ref={trigger} type="button" aria-label={t('quota.account')} aria-haspopup="dialog" aria-expanded={open} aria-controls="wuyu-account-quota" aria-busy={loading} onClick={() => { setOpen(value => !value); if (!open) setRevision(value => value + 1) }}>
      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden><circle cx="10" cy="6.5" r="3" stroke="currentColor" strokeWidth="1.4"/><path d="M3.5 17a6.5 6.5 0 0 1 13 0" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/></svg>
      <span>{view ? view.unlimited ? t('quota.unlimited') : t('quota.summary', { points: view.available.points, used: view.daily.points.spent, limit: view.daily.points.granted }) : t('quota.account')}</span>
    </button>
    {open ? <section className={css.panel} role="dialog" id="wuyu-account-quota" aria-label={t('quota.account')} aria-busy={loading}>
      <div className={css.heading}><strong>{account?.username ?? t('quota.account')}</strong><button type="button" aria-label={t('quota.close')} onClick={() => { setOpen(false); trigger.current?.focus() }}>×</button></div>
      {error ? <p className={css.error} role="alert">{t(error === 401 ? 'quota.signInRequired' : 'quota.unavailable')}</p> : null}
      {loading && !view ? <p className={css.status} role="status">{t('quota.loading')}</p> : null}
      {view ? <>
        <div className={css.balances}><div><span>{t('quota.points')}</span><strong>{view.unlimited ? t('quota.unlimited') : view.available.points}</strong></div><div><span>{t('quota.pages')}</span><strong>{view.unlimited ? t('quota.unlimited') : view.daily.points.available}</strong></div></div>
        <dl className={css.details}><div><dt>{t('quota.today')}</dt><dd>{t('quota.used', { used: view.daily.points.spent, limit: view.unlimited ? t('quota.unlimited') : view.daily.points.granted })}</dd></div><div><dt>{t('quota.bonus')}</dt><dd>{view.unlimited ? t('quota.unlimited') : t('quota.bonusValue', { pages: view.bonus.pages.available, points: view.bonus.points.available })}</dd></div><div><dt>{t('quota.reserved')}</dt><dd>{t('quota.reservedValue', { pages: view.reserved.pages, points: view.reserved.points })}</dd></div></dl>
        {view.control.spending_frozen ? <p role="status">{t('quota.frozen')}</p> : null}<p className={css.note}>{t('quota.reset')}</p><p className={css.note}>{t('quota.rates', { image: view.tariffs.image.points, trace: view.tariffs.trace.points, input: view.tariffs.text.input_tokens_per_point, output: view.tariffs.text.output_tokens_per_point, search: view.tariffs.search.points, fetch: view.tariffs.fetch.points })}</p>
        <form className={css.redeem} onSubmit={event => { event.preventDefault(); redeem() }}>
          <input aria-label={t('quota.redeemLabel')} placeholder={t('quota.redeemPlaceholder')} value={code} maxLength={40} disabled={redeeming} onChange={event => setCode(event.target.value)} />
          <button type="submit" disabled={redeeming || !code.trim()}>{t(redeeming ? 'quota.redeeming' : 'quota.redeem')}</button>
        </form>
        {redeemNote ? <p className={redeemFailed ? css.error : css.note} role={redeemFailed ? 'alert' : 'status'}>{redeemNote}</p> : null}
        <h3>{t('quota.ledger')}</h3>
        {ledgerError ? <p role="status">{t('quota.ledgerUnavailable')}</p> : entries === null ? <p role="status">{t('quota.loading')}</p> : entries.length === 0 ? <p className={css.note}>{t('quota.noEntries')}</p> : <ul className={css.ledger}>{entries.map(entry => <li key={entry.id}><span>{eventLabel(entry.event)} · {t(entry.kind === 'image' ? 'quota.image' : entry.kind === 'trace' ? 'quota.trace' : entry.kind === 'text' ? 'quota.text' : entry.kind === 'voice' ? 'quota.voice' : entry.kind ? 'quota.web' : 'quota.adjustment')}<small>{entry.day}</small></span><span>{entryAmount(entry)}</span></li>)}</ul>}
      </> : null}
      <div className={css.actions}><button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>{t('quota.refresh')}</button>{error === 401 ? <a href="/login">{t('quota.signIn')}</a> : null}</div>
    </section> : null}
  </div>
}
