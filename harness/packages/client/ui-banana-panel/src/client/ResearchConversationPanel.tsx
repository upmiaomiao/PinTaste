/**
 * Template / image decisions beside the conversation.
 *
 * Web and deep search no longer has a card here: it is switched on in the composer, runs as a
 * background job, shows as one tool row in the conversation, and its details live in the
 * project's 材料 tab.
 */
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { createBananaPanelStore } from './store.ts'
import { BananaReviewTail, type ReviewActions } from './BananaReviewTail.tsx'
import type { ConversationDecisions } from './turn-review-decisions.ts'

type DecisionsProps = PropsRuntime<'conversation.chat.turnTail'> & PropsLocale<'bananaPanel'>
  & PropsStore<ReturnType<typeof createBananaPanelStore>> & ReviewActions & { matched: ConversationDecisions }

/** Render this Turn's real review decisions once, outside the Tool-process disclosure. */
export function BananaConversationDecisions({ matched, ...props }: DecisionsProps) {
  return matched.review.length ? <BananaReviewTail {...props} matched={matched.review} /> : null
}
