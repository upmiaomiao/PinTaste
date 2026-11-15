import { ResearchConversationClient } from './research-conversation.ts'
import { toolJsonObjects } from './tool-json.ts'

const WORKFLOW_READS = new Set([
  'mcp__banana__create_presentation', 'mcp__banana__get_presentation_workflow',
  'mcp__banana__presentation_workflow_action', 'mcp__banana__wait_presentation_workflow',
])
const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null

/** Only an owned workflow result may advertise this local, non-generating recovery. */
export function hasResearchRecovery(toolName: string, texts: readonly string[], projectId: string, handle: string): boolean {
  if (!WORKFLOW_READS.has(toolName)) return false
  return toolJsonObjects(texts).some(value => {
    if (value.project_id !== projectId || value.project_handle !== handle) return false
    const recovery = record(record(value.conversation)?.recovery)
    return recovery?.kind === 'research' && recovery.use_existing_materials === true && recovery.keep_project === true
  })
}

export type ResearchRecoveryResult = 'recovered' | 'already_ready' | 'state_changed'
type RecoveryClient = Pick<ResearchConversationClient, 'read' | 'write'>

/** A real click re-reads the current state and changes only the research scope. */
export async function recoverWithExistingMaterials(projectId: string, signal?: AbortSignal, client: RecoveryClient = new ResearchConversationClient(), request: typeof fetch = fetch): Promise<ResearchRecoveryResult> {
  const snapshot = await client.read(projectId, signal)
  signal?.throwIfAborted()
  const state = snapshot.research
  if (state.materials_only_current === true) return 'already_ready'
  // Only the backend owner can offer this evidence-preserving scope contract.
  // Old projections remain safely read-only; complete/unreviewed reports use
  // their existing review/reuse contracts instead.
  if (!['failed', 'paused'].includes(state.status) || state.consent_current !== true
    || state.can_use_existing_materials !== true || typeof state.report_id !== 'string' || !state.report_id) return 'state_changed'
  // The bounded research projection omits jobs. Read the same revision's work
  // state before changing scope so a historical card cannot cancel newer work.
  const response = await request('/agent-api/api/projects/' + encodeURIComponent(projectId) + '/workflow', {
    credentials:'same-origin', redirect:'error', cache:'no-store', ...(signal ? {signal} : {}),
  })
  if (!response.ok) throw new Error('RESEARCH_RECOVERY_READ_FAILED')
  const raw = record(await response.json()), workflow = record(raw?.data)
  if (raw?.success !== true || workflow?.project_id !== projectId || workflow.revision !== snapshot.revision
    || !Number.isSafeInteger(workflow.epoch) || !Array.isArray(workflow.jobs)) return 'state_changed'
  if (workflow.jobs.some(value => {
    const job = record(value)
    return !job || !['completed','failed','cancelled','interrupted'].includes(String(job.status))
  })) return 'state_changed'
  signal?.throwIfAborted()
  const saved = await client.write({projectId, action:'confirm_research', expectedRevision:snapshot.revision,
    requestId:crypto.randomUUID(), signal, payload:{mode:'materials', prompt:state.prompt ?? '', acknowledge_cost:false,
      preserve_research:true, report_id:state.report_id}})
  return saved.research.materials_only_current === true && saved.research.report_id === state.report_id ? 'recovered' : 'state_changed'
}
