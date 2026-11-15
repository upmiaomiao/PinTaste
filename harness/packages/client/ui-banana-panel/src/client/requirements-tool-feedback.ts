/** Fixed requirement failures select localized copy without displaying tool error text. */
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import type { BananaPanelKey } from './locales.ts'

const failureKeys: Record<string, BananaPanelKey> = {
  PPT_REQUIREMENTS_SOURCE_REQUIRED: 'current.sourceRequired',
  PPT_CONFIG_CONFLICT: 'current.updateConflict',
  REQUEST_ID_CONFLICT: 'current.updateConflict',
  PPT_REQUIREMENTS_INVALID: 'current.updateInvalid',
  PPT_CONFIG_INVALID: 'current.updateInvalid',
  PPT_REQUIREMENTS_PROJECTION_PENDING: 'current.syncPending',
}

/**
 * Select copy from an exact fixed error, preserving unknown errors as uncertain outcomes.
 * @param block The persisted tool result or current streaming call.
 * @param operation Whether the tool reads or updates requirements.
 * @returns A locale key; error details are never rendered.
 */
export function requirementsToolFailureKey(block: ToolCallViewProps['block'], operation: 'read' | 'update'): BananaPanelKey {
  if (operation === 'read') return 'current.readToolFailed'
  if (!('kind' in block) || !block.isError) return 'current.syncIncomplete'
  const texts = block.content.filter(item => item.type === 'text').map(item => item.text.trim())
  if (texts.length !== 1) return 'current.syncIncomplete'
  const code = texts[0]!.replace(/^Error:\s*/, '')
  return Object.hasOwn(failureKeys, code) ? failureKeys[code]! : 'current.syncIncomplete'
}
