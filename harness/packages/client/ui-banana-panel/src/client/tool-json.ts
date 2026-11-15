/** The Host MCP adapter joins text blocks with newlines before journaling them. */
export function toolJsonObjects(texts: readonly string[]): Record<string, unknown>[] {
  const objects: Record<string, unknown>[] = []
  for (const text of texts) {
    // Accept a complete JSON block, or its complete trailing block after human copy.
    // Never extract JSON from quoted fragments, fenced examples or partial braces.
    const starts = [0]
    for (const match of text.matchAll(/\n(?=\s*\{)/g)) {
      if (starts.length === 8) break
      starts.push(match.index + 1)
    }
    for (const start of starts) {
      try {
        const value: unknown = JSON.parse(text.slice(start))
        if (value && typeof value === 'object' && !Array.isArray(value)) {
          objects.push(value as Record<string, unknown>)
          break
        }
      } catch { /* Human copy is not a machine context. */ }
    }
  }
  return objects
}
