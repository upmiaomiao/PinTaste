/**
 * Banana Slides deck row, browser half: extracts the opaque project handle
 * from a settled banana MCP call, then hands it to the right details column
 * through the inject-bound `openPanel` callback (which writes the shared
 * store and opens the details panel). The row itself stays a compact status
 * line; the official project panel lives in the right column.
 */
import { useEffect } from 'react'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import { projectContext } from './preview-target.ts'
import { bananaToolRow, isQuietProgressRead } from './tool-row.ts'
import type { PreviewTarget } from './preview-target.ts'
import css from './BananaPanelRow.module.css'
import { ResearchRecovery } from './ResearchRecovery.tsx'
import { hasResearchRecovery } from './research-recovery.ts'
type BananaPanelProps = ToolCallViewProps & PropsLocale<'bananaPanel'> & {
  /** Write the project handle to the shared store and open the details column. */
  rememberProject:(handle:string,projectId?:string,taskMode?:'create'|'relayout')=>void
  openPanel: (handle: string, projectId?:string, target?:PreviewTarget) => void
}
/** Inline row that routes one deck project into the right details panel. */
export function BananaPanelRow(props: BananaPanelProps) {
  const { block, toolName, t, openPanel, rememberProject } = props
  const row = bananaToolRow(toolName, block)
  const settled='kind' in block
  const context=settled?projectContext(block.content.filter(x=>x.type==='text').map(x=>x.text),block.call?.argsRaw):null
  const handle=context?.handle||null
  useEffect(()=>{if(context){rememberProject(context.handle,context.projectId,context.taskMode);if(settled&&!block.isError&&toolName==='mcp__banana__open_presentation_panel')openPanel(context.handle,context.projectId,context.target)}},[handle,toolName,settled&&block.isError,context?.target?.view,context?.target?.page_number])
  const recovery = settled && context?.projectId && hasResearchRecovery(toolName, block.content.filter(x=>x.type==='text').map(x=>x.text), context.projectId, context.handle)
  if (isQuietProgressRead(toolName, row) && !recovery) return null
  return (
    <div className={css.card}>
      <div className={row.state === 'failed' ? css.error : css.rowOpened}>
        <span>{t(row.titleKey as Parameters<typeof t>[0])}</span>
        {context ? <button type="button" className={css.previewButton} onClick={()=>{rememberProject(context.handle,context.projectId,context.taskMode);openPanel(context.handle,context.projectId,context.target)}}>{t('row.opened')}</button> : null}
      </div>
      {recovery && context?.projectId ? <ResearchRecovery key={context.projectId} projectId={context.projectId} t={t} /> : null}
      {row.state === 'failed' ? <small className={css.recordNote}>{t('row.historyNote')}</small> : null}
    </div>
  )
}
