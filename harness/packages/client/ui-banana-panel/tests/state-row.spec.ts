import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { bananaToolRow } from '../src/client/tool-row.ts'

const records = JSON.parse(readFileSync(new URL('./fixtures/work-state-37.json', import.meta.url), 'utf8')) as {
  name: string; payload: Record<string, unknown>; expected: { titleKey: string; state: string }
}[]

describe('recorded work state projection', () => {
  for (const record of records) it(record.name, () => {
    // These serialized fixtures retain the fields consumed by the row projection.
    const block = { kind: 'tool-result', isError: record.payload.__isError === true,
      content: [{ type: 'text', text: JSON.stringify(record.payload) }],
      call: { argsRaw: JSON.stringify({ action: 'analyze_materials' }) } }
    expect(bananaToolRow('mcp__banana__presentation_workflow_action', block as unknown as Parameters<typeof bananaToolRow>[1])).toEqual(record.expected)
  })
})
