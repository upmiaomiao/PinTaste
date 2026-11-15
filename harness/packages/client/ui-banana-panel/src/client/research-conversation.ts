/** Authenticated research choices and progress belong to the conversation. */
export type ResearchMode = 'materials' | 'web' | 'deep'
export interface ResearchProfile {
  label: string
  model_calls: number
  searches: number
  fetches: number
  token_limit: number
  estimated_minutes?: { min: number; max: number }
}
export interface ResearchOperation {
  kind: string; status: string; provider?: string; query?: string; url?: string
  result_count?: number; source_ids?: string[]; source_titles?: string[]; preview?: string; error_code?: string
}
export interface ResearchSource { id: string; title: string; url: string; provider?: string; source_kind?: string }
export interface ResearchReport { summary: string; partial?: boolean; findings?: unknown[]; sources?: ResearchSource[] }
export interface ConversationResearch {
  required: boolean
  status: string
  mode?: ResearchMode | null
  prompt?: string
  report_id?: string | null
  can_supplement?: boolean
  consent_current?: boolean
  ready_for_planning?: boolean
  can_use_existing_materials?: boolean
  materials_only_current?: boolean
  round?: number
  profiles: Record<ResearchMode, ResearchProfile>
  progress?: { stage?: string; query?: string; provider?: string; source_count?: number; model_calls?: number; operations?: ResearchOperation[]; round_operation_start?: number }
  report?: ResearchReport | null
  previous_report?: ResearchReport | null
  partial_sources?: ResearchSource[]
  feedback_history?: { round: number; prompt: string; confirmed_at: string }[]
}
export interface ResearchSnapshot { project_id: string; revision: number; brief?: { topic?: string; goal?: string; requirements?: string }; research: ConversationResearch }
export interface ResearchCard { seq: number; handle: string; projectId: string; reportId?: string; abortSeq?: number }
export interface ResearchWrite {
  projectId: string
  action: 'confirm_research' | 'supplement_research' | 'accept_research' | 'pause'
  payload: Record<string, unknown>
  requestId: string
  expectedRevision?: number
  signal?: AbortSignal
}

/** A rejected request is distinct from an uncertain transport outcome. */
export class ResearchRequestError extends Error {
  constructor(readonly code: string, readonly definitive: boolean, readonly status?: number) { super(code) }
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const natural = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0
function profile(value: unknown): value is ResearchProfile {
  return record(value) && typeof value.label === 'string'
    && ['model_calls', 'searches', 'fetches', 'token_limit'].every(key => natural(value[key]))
    && (value.estimated_minutes === undefined || record(value.estimated_minutes)
      && natural(value.estimated_minutes.min) && natural(value.estimated_minutes.max)
      && Number(value.estimated_minutes.min) <= Number(value.estimated_minutes.max))
}

/** @param value - untrusted first-party response. @returns project-bound research state. */
export function researchSnapshot(value: unknown, projectId: string): ResearchSnapshot {
  if (!record(value) || value.project_id !== projectId || !Number.isSafeInteger(value.revision) || Number(value.revision) < 0
    || !record(value.research) || typeof value.research.status !== 'string' || !record(value.research.profiles)
    || !['materials', 'web', 'deep'].every(mode => profile(((value.research as Record<string, unknown>).profiles as Record<string, unknown>)[mode]))) {
    throw new ResearchRequestError('INVALID_RESPONSE', false)
  }
  return value as unknown as ResearchSnapshot
}

/** Read and write through the owned same-origin bridge, never browser-supplied credentials. */
export class ResearchConversationClient {
  private readonly request: typeof fetch
  constructor(request: typeof fetch = fetch) {
    this.request = (input, init) => request.call(globalThis, input, init)
  }

  private async response(response: Response, projectId: string) {
    const body: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const error = record(body) && record(body.error) ? body.error : {}
      throw new ResearchRequestError(typeof error.code === 'string' ? error.code : 'REQUEST_FAILED', response.status >= 400 && response.status < 500, response.status)
    }
    if (!record(body) || body.success !== true) throw new ResearchRequestError('INVALID_RESPONSE', false, response.status)
    return researchSnapshot(body.data, projectId)
  }

  /** @param projectId - observed owned project id. @param signal - mounted-view cancellation. */
  async read(projectId: string, signal?: AbortSignal): Promise<ResearchSnapshot> {
    return this.response(await this.request('/agent-api/api/projects/' + encodeURIComponent(projectId) + '/workflow/research', {
      credentials: 'same-origin', redirect: 'error', cache: 'no-store', ...(signal ? { signal } : {}),
    }), projectId)
  }

  /** Preserve the exact admitted revision on transport retry; a new choice receives a new key. */
  async write(operation: ResearchWrite): Promise<ResearchSnapshot> {
    if (operation.expectedRevision === undefined) operation.expectedRevision = (await this.read(operation.projectId)).revision
    return this.response(await this.request('/agent-api/api/projects/' + encodeURIComponent(operation.projectId) + '/workflow/action', {
      method: 'POST', credentials: 'same-origin', redirect: 'error', signal: operation.signal ? AbortSignal.any([operation.signal, AbortSignal.timeout(45000)]) : AbortSignal.timeout(45000),
      headers: { 'Content-Type': 'application/json', 'X-Wuyu-Interaction': 'research-confirmation', 'Idempotency-Key': operation.requestId },
      body: JSON.stringify({ action: operation.action, payload: operation.payload, expected_revision: operation.expectedRevision }),
    }), operation.projectId)
  }
}
