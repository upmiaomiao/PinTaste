/** Managed Work creation: a name field and one explicit commit, without a directory browser. */
import { useEffect, useId, useRef, useState } from 'react'
import { Button, Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import type { WorkspaceId } from '@deepseek-ai/dsh-api-workspace-controller/client'
import type { Translate } from '@deepseek-ai/dsh-client-locale/client'
import { NamedWorkspaceError } from './managed-workspace.ts'
import type { NamedWorkspace } from './managed-workspace.ts'
import css from './NamedWorkspaceForm.module.css'

/** Name-only creation callbacks injected by the composed managed provider. */
export interface NamedWorkspaceFormProps {
  open: boolean
  busy: boolean
  t: Translate
  createNamedWorkspace: (name: string, requestId: string, signal: AbortSignal) => Promise<NamedWorkspace>
  onCreated: (id: WorkspaceId) => void
  onClose: () => void
}

/** Render the managed name flow; cancellation never issues another creation. */
export function NamedWorkspaceForm({ open, busy, t, createNamedWorkspace, onCreated, onClose }: NamedWorkspaceFormProps) {
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const request = useRef<{ name: string; id: string } | null>(null)
  const active = useRef<AbortController | null>(null)
  const composing = useRef(false)
  const fieldId = useId()
  const locked = busy || submitting
  useEffect(() => {
    if (open) { setName(''); setError(null); request.current = null }
    return () => { active.current?.abort(); active.current = null }
  }, [open])
  const submit = async (): Promise<void> => {
    const title = name.normalize('NFC').trim()
    if (locked || active.current !== null || title === '') return
    if (title.length > 80 || title === '.' || title === '..' || /[/\\\p{C}]/u.test(title)) {
      setError(t('managed.invalidName')); return
    }
    if (request.current?.name !== title) request.current = { name: title, id: crypto.randomUUID() }
    const operation = new AbortController(); active.current = operation
    setSubmitting(true); setError(null)
    try {
      const result = await createNamedWorkspace(title, request.current.id, operation.signal)
      if (!operation.signal.aborted) onCreated(result.workspaceId)
    } catch (reason) {
      if (!operation.signal.aborted) setError(t(reason instanceof NamedWorkspaceError && reason.status === 409
        ? 'managed.conflict' : reason instanceof NamedWorkspaceError && reason.status === 401 ? 'managed.signIn' : 'managed.failed'))
    } finally {
      if (active.current === operation) { active.current = null; setSubmitting(false) }
    }
  }
  return <Modal open={open} onClose={locked ? () => {} : onClose} closeLabel={t('browser.cancel')}
    title={t('managed.title')} footer={<>
      <Button variant="outline" disabled={locked} onClick={onClose}>{t('browser.cancel')}</Button>
      <Button variant="primary" disabled={locked || name.trim() === ''} onClick={() => { void submit() }}>{t(locked ? 'managed.creating' : 'managed.create')}</Button>
    </>}>
    <div className={css.body}>
      <label className={css.label} htmlFor={fieldId}>{t('managed.name')}</label>
      <input id={fieldId} className={css.input} value={name} maxLength={80} autoFocus disabled={locked}
        placeholder={t('managed.placeholder')} onChange={event => { setName(event.target.value); setError(null) }}
        onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }}
        onKeyDown={event => { if (event.key === 'Enter' && !composing.current) { event.preventDefault(); void submit() } }} />
      <p className={css.hint}>{t('managed.hint')}</p>
      {error !== null && <p className={css.error} role="alert">{error}</p>}
      {locked && <p className={css.hint} role="status">{t('managed.creating')}</p>}
    </div>
  </Modal>
}
