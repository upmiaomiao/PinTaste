/** Shared settings remain inside the work page and cannot navigate its parent arbitrarily. */
import { useEffect, useRef, useState } from 'react'
import { Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import { workSettingsMessage, workSettingsUrl, type WorkSettingsSection } from './work-shell.ts'
import css from './WorkUtilities.module.css'

interface Props extends PropsLocale<'bananaPanel'> {
  section: WorkSettingsSection
  onClose(): void
  onUserUpdated(): void
  onNavigate(path: '/work' | '/work/admin' | '/work?templates=review' | '/work-auth/sso?next=/work'): void
}

/** Render the same-origin shared settings entry without replacing the work URL. */
export function WorkSettingsDialog({ section, onClose, onUserUpdated, onNavigate, t }: Props) {
  const frame = useRef<HTMLIFrameElement>(null), close = useRef<HTMLButtonElement>(null)
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState(false)
  useEffect(() => {
    const previous = document.activeElement
    close.current?.focus()
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus() }
  }, [])
  useEffect(() => {
    const receive = (event: MessageEvent<unknown>) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow) return
      const message = workSettingsMessage(event.data)
      if (!message) return
      if (message.type === 'user-updated') onUserUpdated()
      else if (message.type === 'auth-required') { onClose(); onNavigate('/work-auth/sso?next=/work') }
      else if (message.type === 'open-work') { onClose(); onNavigate(message.path) }
      else onClose()
    }
    window.addEventListener('message', receive)
    return () => window.removeEventListener('message', receive)
  }, [onClose, onNavigate, onUserUpdated])
  const sizeLabel = t(expanded ? 'work.restoreSettings' : 'work.maximizeSettings')
  return <Modal open headless title={t('work.settings')} onClose={onClose} className={[css.settingsDialog, expanded ? css.settingsDialogExpanded : undefined].filter(Boolean).join(' ')}>
    <div className={css.dialogHeading}><strong>{t('work.settings')}</strong><div className={css.dialogActions}>
      <button type="button" aria-label={sizeLabel} title={sizeLabel} aria-pressed={expanded} onClick={() => setExpanded(value => !value)}>
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden>{expanded
          ? <><path d="M8 8V5h11v11h-3" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"/><rect x="5" y="8" width="11" height="11" rx="1" stroke="currentColor" strokeWidth="1.6"/></>
          : <rect x="5" y="5" width="14" height="14" rx="1" stroke="currentColor" strokeWidth="1.6"/>}</svg>
      </button>
      <button ref={close} type="button" aria-label={t('work.closeSettings')} title={t('work.closeSettings')} onClick={onClose}><svg viewBox="0 0 24 24" width="19" height="19" fill="none" aria-hidden><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg></button>
    </div></div>
    {!loaded ? <span className={css.loading} role="status">{t('work.settingsLoading')}</span> : null}
    <iframe ref={frame} className={css.settingsFrame} title={t('work.platformSettings')} src={workSettingsUrl(section)} onLoad={() => setLoaded(true)} />
  </Modal>
}
