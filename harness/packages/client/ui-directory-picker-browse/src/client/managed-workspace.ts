/** Tenant-owned named Workspace creation; callers never submit a filesystem path. */
import type { WorkspaceId } from '@deepseek-ai/dsh-api-workspace-controller/client'

/** The committed server registration exposed to the picking owner. */
export interface NamedWorkspace {
  workspaceId: WorkspaceId
  title: string
}

/** Bounded status classification; server paths and raw proxy errors stay out of UI. */
export class NamedWorkspaceError extends Error {
  /** @param status - HTTP status, or zero when transport fails. */
  constructor(readonly status: number) { super('Named workspace creation failed') }
}

/**
 * Register a Workspace inside the current account's server-selected scope.
 * @param name - display name, never a directory or server path.
 * @param requestId - stable idempotency key retained while retrying this submission.
 * @param signal - the visible creation flow's lifetime.
 * @returns opaque Workspace identity and display name.
 */
export async function createNamedWorkspace(name: string, requestId: string, signal: AbortSignal): Promise<NamedWorkspace> {
  let response: Response
  try {
    response = await fetch('/api/workspace/named', {
      method: 'POST', credentials: 'same-origin', signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, requestId }),
    })
  } catch (error) {
    if (signal.aborted) throw error
    throw new NamedWorkspaceError(0)
  }
  if (!response.ok) throw new NamedWorkspaceError(response.status)
  const value: unknown = await response.json()
  if (value === null || typeof value !== 'object' || !('workspaceId' in value) || !('title' in value)
    || Object.keys(value).sort().join(',') !== 'title,workspaceId'
    || typeof value.workspaceId !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.workspaceId)
    || typeof value.title !== 'string' || value.title.length < 1 || value.title.length > 80 || /[/\\\p{C}]/u.test(value.title)) throw new NamedWorkspaceError(502)
  return { workspaceId: value.workspaceId as WorkspaceId, title: value.title }
}
