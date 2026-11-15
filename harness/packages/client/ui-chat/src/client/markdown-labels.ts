/** Localized copy adapters for Cordis-free Markdown primitives. */

import type { MarkdownLabels } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ChatViewSlotProps } from './contract/slots.ts'
import { assetLinkLabel } from './asset-link-label.ts'

/**
 * Build the complete Markdown chrome copy for one locale revision.
 * @param t - Chat locale seat.
 * @returns Labels for code fences and footnotes.
 */
export function markdownLabels(t: ChatViewSlotProps['t']): MarkdownLabels {
  const fileLabel = t('assetLink.file')
  return {
    code: { copyLabel: t('copy'), copiedLabel: t('copied') },
    footnotes: t('markdown.footnotes'),
    resolveBareLinkLabel: href => assetLinkLabel(href, fileLabel),
  }
}
