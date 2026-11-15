/** A settled local requirement tool refreshes the authoritative readback without copying its result into another store. */
import { useEffect } from 'react'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import css from './BananaPanelRow.module.css'
import { requirementsToolFailureKey } from './requirements-tool-feedback.ts'
type Props = ToolCallViewProps & PropsLocale<'bananaPanel'> & {
  requirementsOperation: 'read' | 'update'
  refreshRequirements(): Promise<void>
  openRequirements(): void
}
/** Refresh only successful local-tool completions; a failed operation retains its ordinary error presentation. */
export function CurrentRequirementsToolRow({ block, requirementsOperation, refreshRequirements, openRequirements, t }: Props) {
  const settled = 'kind' in block
  useEffect(() => {
    if (settled && !block.isError) void refreshRequirements().catch(() => { /* The saved requirements remain available through an explicit reload. */ })
  }, [settled, settled && block.isError])
  if (!settled) return null
  if (block.isError) return <div className={css.card}><div className={css.error}><span>{t(requirementsToolFailureKey(block, requirementsOperation))}</span><button type="button" className={css.previewButton} onClick={openRequirements}>{t('current.open')}</button></div></div>
  return <div className={css.card}><div className={css.rowOpened}><span>{t('current.updated')}</span><button type="button" className={css.previewButton} onClick={openRequirements}>{t('current.open')}</button></div></div>
}
