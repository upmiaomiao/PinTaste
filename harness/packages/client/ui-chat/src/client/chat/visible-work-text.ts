/** Reserved confirmation envelopes are transport data, never user-facing copy. */
const reservedEnvelope = /<wuyu-(template-selection|image-approval|material-context)(?=[\s>]|$)/gi

/**
 * @param raw - Unmodified durable or pending user text.
 * @returns a display-only projection; an unfinished reserved envelope hides its remaining transport payload.
 */
export function visibleWorkText(raw: string): string {
  let visible = '', cursor = 0
  // A fresh regexp keeps calls independent when durable and pending rows render together.
  const opening = new RegExp(reservedEnvelope.source, reservedEnvelope.flags)
  for (let match = opening.exec(raw); match; match = opening.exec(raw)) {
    visible += raw.slice(cursor, match.index)
    const closing = new RegExp('</wuyu-' + match[1] + '\\s*>', 'gi')
    closing.lastIndex = opening.lastIndex
    const end = closing.exec(raw)
    if (!end) return visible.trimEnd()
    cursor = closing.lastIndex
    opening.lastIndex = cursor
  }
  return visible + raw.slice(cursor)
}
