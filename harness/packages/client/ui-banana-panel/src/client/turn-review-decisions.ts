/** Real, project-bound workflow decisions accumulated within one durable Turn. */
import { isAppendSurfaceEvent } from '@deepseek-ai/dsh-session/surface'
import type { TurnTailOwnerProps } from '@deepseek-ai/dsh-client-ui-chat/client'
import type { ConversationNodeDefinition } from '@deepseek-ai/dsh-client-ui-conversation/client'
import { projectContext } from './preview-target.ts'
import { reviewDecision, type ReviewDecision } from './review-decision.ts'
import { toolJsonObjects } from './tool-json.ts'
import type { ResearchCard } from './research-conversation.ts'

/** Only validated server facts reach the visible review card. */
export interface ReviewCard {
  readonly seq: number
  readonly handle: string
  readonly projectId?: string
  readonly decision: ReviewDecision
}

interface ProjectObservation {
  readonly handle: string
  readonly projectId?: string
  readonly revision: number
  readonly card: ReviewCard | null
  readonly research?: ResearchCard
}

export interface ReviewDecisionsTurnData {
  readonly observations: readonly ProjectObservation[]
}
/** One chain entry composes the independent research and visual review decisions. */
export interface ConversationDecisions {
  readonly research: ResearchCard | null
  readonly review: readonly ReviewCard[]
}

declare module '@deepseek-ai/dsh-client-ui-conversation/client' {
  interface ConversationTurnDataMap {
    /** This Turn's latest observed server decision per owned project. */
    'wuyu-review-decisions': ReviewDecisionsTurnData
  }
}

interface ReviewState extends ReviewDecisionsTurnData {
  readonly turn: number
  readonly calls: ReadonlyMap<string, string>
}

// Material reads, research and template prose are untrusted content, even when
// their text happens to resemble a valid workflow JSON result.
const WORKFLOW_TOOLS = new Set([
  'mcp__banana__create_presentation',
  'mcp__banana__get_presentation_workflow',
  'mcp__banana__presentation_workflow_action',
  'mcp__banana__wait_presentation_workflow',
  'mcp__banana__open_presentation_panel',
])

/**
 * Fold paired first-party Tool results into Turn data without reading prose,
 * neighboring Chat Nodes, or a second event subscription.
 * @param base - the current browser origin used to validate owned image URLs.
 * @returns a replayable, location-only Conversation Definition.
 */
export function createReviewDecisionsDefinition(base: string): ConversationNodeDefinition<ReviewState> {
  return {
    kind: 'wuyu-review-decisions',
    match: event => {
      if (event.type === 'turn/start') return { id: String(event.data.turn), role: 'start' }
      if (event.type === 'turn/end' || event.type === 'tool/call'
        || event.type === 'tool/result' && isAppendSurfaceEvent(event)) {
        return { id: String(event.data.turn), role: 'update' }
      }
      return null
    },
    start: (_context, match) => {
      if (match.event.type !== 'turn/start') throw new Error('review decisions require turn/start')
      return { turn: match.event.data.turn, calls: new Map(), observations: [] }
    },
    update: (context, match) => {
      const event = match.event
      if (event.type === 'turn/end') {
        if (event.data.reason.kind !== 'aborted' || event.data.reason.reason.kind !== 'user') return context.state
        return { ...context.state, observations: context.state.observations.map(item => item.research?.reportId
          ? { ...item, research: { ...item.research, abortSeq: event.seq } } : item) }
      }
      if (event.type === 'tool/call') {
        if (!WORKFLOW_TOOLS.has(event.data.name)) return context.state
        const calls = new Map(context.state.calls)
        calls.set(String(event.data.callId), event.data.name)
        return { ...context.state, calls }
      }
      if (event.type !== 'tool/result'
        || !context.state.calls.has(String(event.data.message.source.callId))) return context.state
      const result = event.data.message.content[0]
      if (result.isError === true) return context.state
      const texts = result.content.filter(item => item.type === 'text').map(item => item.text)
      const project = projectContext(texts)
      if (!project) return context.state
      const raw = toolJsonObjects(texts).find(value => value.project_handle === project.handle
        && value.conversation && typeof value.conversation === 'object'
        && !Array.isArray(value.conversation)
        && Number.isSafeInteger(value.revision) && Number(value.revision) >= 0)
      if (!raw) return context.state
      const revision = Number(raw.revision)
      const conversation = raw.conversation as Record<string, unknown>
      const research = raw.research && typeof raw.research === 'object' && !Array.isArray(raw.research)
        ? raw.research as Record<string, unknown> : undefined
      // Historical MCP text carries the server conversation decision but not
      // structuredContent.research. The read-only projection supplies details.
      const reviewTarget = conversation.review_target && typeof conversation.review_target === 'object'
        ? conversation.review_target as Record<string, unknown> : undefined
      const reportId = typeof research?.report_id === 'string' ? research.report_id
        : typeof reviewTarget?.report_id === 'string' ? reviewTarget.report_id : undefined
      const previous = context.state.observations.findIndex(item => item.handle === project.handle
        || project.projectId !== undefined && item.projectId === project.projectId)
      if (previous >= 0 && context.state.observations[previous]!.revision > revision) return context.state
      const decision = reviewDecision(texts, project.handle, base)
      const observation: ProjectObservation = {
        handle: project.handle,
        ...project.projectId === undefined ? {} : { projectId: project.projectId },
        revision,
        card: decision === null ? null : { seq: event.seq, ...project, decision },
        ...project.projectId
          ? { research: { seq: event.seq, handle: project.handle, projectId: project.projectId,
            ...(reportId ? { reportId } : {}) } } : {},
      }
      const observations = [...context.state.observations]
      if (previous < 0) observations.push(observation)
      else observations[previous] = observation
      return { ...context.state, observations }
    },
    buildLocationData: (context, scope, previous) => {
      if (scope !== 'turn' || context.state === undefined) return null
      if (previous?.kind === 'turn' && previous.turn === context.state.turn
        && previous.key === 'wuyu-review-decisions'
        && previous.value.observations === context.state.observations) return previous
      return { kind: 'turn', turn: context.state.turn, key: 'wuyu-review-decisions',
        value: { observations: context.state.observations } }
    },
  }
}

/**
 * Claim the completed-Turn extension only for this Turn's actual decisions.
 * No decision or consent is inherited by a later Turn.
 */
export function selectReviewDecisions(owner: TurnTailOwnerProps): readonly ReviewCard[] | null {
  const observations = owner.turn.data.get('wuyu-review-decisions')?.observations ?? []
  const cutoff = owner.turn.end?.seq ?? owner.seq
  const cards = observations.flatMap(item => item.card && item.card.seq <= cutoff ? [item.card] : [])
  return cards.length ? cards : null
}

/** @returns this Turn's latest trusted research observation, independent of the details column. */
export function selectResearchConversation(owner: TurnTailOwnerProps): ResearchCard | null {
  const observations = owner.turn.data.get('wuyu-review-decisions')?.observations ?? []
  const cutoff = owner.turn.end?.seq ?? owner.seq
  return observations.reduce<ResearchCard | null>((latest, item) =>
    item.research && item.research.seq <= cutoff && (!latest || latest.seq < item.research.seq) ? item.research : latest, null)
}

/** @returns both decisions so the first matching chain registration cannot hide either. */
export function selectConversationDecisions(owner: TurnTailOwnerProps): ConversationDecisions | null {
  // Search has no conversation card any more (composer switch + background job + 材料 tab).
  const review = selectReviewDecisions(owner) ?? []
  return review.length ? { research: null, review } : null
}
