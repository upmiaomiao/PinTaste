import type { BananaCallResult } from './banana-mcp.ts'

const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const projectId = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value)

/** Read private preview data from UI metadata, retaining older uncompressed results.
 * The model-facing structuredContent deliberately omits private image URLs.
 * Validate both envelopes against the mounted handle and each other's project.
 */
export function readTemplateWorkflowSnapshot(result: BananaCallResult, handle: string): Record<string, unknown> {
  if (result.isError) throw new Error('TEMPLATE_WORKFLOW_UNAVAILABLE')
  const metadata = result._meta?.['wuyu/workflow-snapshot']
  const full = object(metadata) ? metadata : null
  const summary = object(result.structuredContent) ? result.structuredContent : null
  const snapshot = full && projectId(full.project_id) ? full : summary && projectId(summary.project_id) ? summary : null
  if (!snapshot) throw new Error('TEMPLATE_WORKFLOW_UNAVAILABLE')
  for (const candidate of [full, summary]) {
    if (candidate && (Object.hasOwn(candidate, 'project_handle') && candidate.project_handle !== handle
      || Object.hasOwn(candidate, 'project_id') && candidate.project_id !== snapshot.project_id)) throw new Error('TEMPLATE_WORKFLOW_UNAVAILABLE')
  }
  return snapshot
}
