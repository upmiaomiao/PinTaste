import { useEffect, useRef, useState } from 'react'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import { recoverWithExistingMaterials } from './research-recovery.ts'
import css from './BananaPanelRow.module.css'

/** User-owned scope recovery; it never submits a chat prompt or starts generation. */
export function ResearchRecovery({projectId, t}: {projectId:string} & PropsLocale<'bananaPanel'>) {
  const operation = useRef<AbortController | null>(null)
  const [state, setState] = useState<'idle' | 'busy' | 'ready' | 'changed' | 'error'>('idle')
  useEffect(() => () => operation.current?.abort(), [])
  const recover = async () => {
    if (operation.current || state === 'ready' || state === 'changed') return
    const current = new AbortController()
    operation.current = current
    setState('busy')
    try {
      const result = await recoverWithExistingMaterials(projectId, AbortSignal.any([current.signal, AbortSignal.timeout(20000)]))
      if (!current.signal.aborted) setState(result === 'state_changed' ? 'changed' : 'ready')
    } catch {
      if (!current.signal.aborted) setState('error')
    } finally {
      if (operation.current === current) operation.current = null
    }
  }
  return <div className={css.reviewCard} data-wuyu-research-recovery>
    <strong>{t('recovery.research.title')}</strong>
    <p>{t('recovery.research.hint')}</p>
    <p>{t('recovery.research.supplementHint')}</p>
    <button type="button" className={css.reviewAction} disabled={state === 'busy' || state === 'ready' || state === 'changed'} onClick={() => void recover()}>
      {t(state === 'busy' ? 'recovery.research.saving' : 'recovery.research.useMaterials')}
    </button>
    {state === 'ready' ? <p role="status">{t('recovery.research.ready')}</p> : null}
    {state === 'changed' ? <p role="status">{t('recovery.research.changed')}</p> : null}
    {state === 'error' ? <p role="alert">{t('recovery.research.error')}</p> : null}
  </div>
}
