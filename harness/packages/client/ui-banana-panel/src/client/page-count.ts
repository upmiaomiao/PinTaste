/** Per-Session, unsent PPT page-count choice; no generation or project writes. */
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'

export interface PageCountSnapshot { fixed: boolean; draft: string }

/** Parse the entire draft; partial numbers, fractions and out-of-range values are invalid. */
export function selectedPageCount(snapshot: PageCountSnapshot): number | null {
  if (!snapshot.fixed) return null
  if (!/^[1-9]\d?$/.test(snapshot.draft)) throw new Error('PPT_PAGE_COUNT_INVALID')
  const count = Number(snapshot.draft)
  if (count < 1 || count > 80) throw new Error('PPT_PAGE_COUNT_INVALID')
  return count
}

/** Session lifetime owns both the visible draft and the submitted choice. */
export class PageCountController {
  readonly state: SnapshotStore<PageCountSnapshot> = createSnapshotStore<PageCountSnapshot>({ fixed: false, draft: '12' })

  /** Choose whether this submission fixes a full-deck count. */
  choose(fixed: boolean): void { this.state.update(draft => { draft.fixed = fixed }) }

  /** Retain the user's complete edit, including an invalid intermediate value. */
  edit(value: string): void { this.state.update(draft => { draft.draft = value }) }

  /** Append the exact visible selection to ordinary logged text; invalid choices block submission. */
  submission(text: string, summary: (count: number) => string): string {
    const count = selectedPageCount(this.state.getSnapshot())
    if (count === null) return text
    const line = summary(count)
    return text.trimEnd().endsWith(line) ? text : text + '\n\n' + line
  }
}
