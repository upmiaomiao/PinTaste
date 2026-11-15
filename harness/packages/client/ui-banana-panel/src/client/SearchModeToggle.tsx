/** Composer switches: 联网搜索 and 深度探索. Off by default: the deck then uses the uploads and 无隅's own knowledge. */
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { isGenerationWorkSession } from './generation-settings.ts'
import type { ArmedSearch } from './search-mode.ts'
import type { PreferencesSnapshot } from './presentation-preferences.ts'
import css from './SearchModeToggle.module.css'

export interface SearchModeInjected {
  hooks: { preferences:ObservableSnapshot<PreferencesSnapshot> }
  toggleSearchMode(mode: ArmedSearch): void
}
type Props = PropsRuntime<'conversation.input.left'> & PropsLocale<'bananaPanel'> & InjectFace<SearchModeInjected>

const ICONS: Record<ArmedSearch, string> = {
  web: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-9-9h18M12 3c2.5 2.6 3.7 5.6 3.7 9s-1.2 6.4-3.7 9c-2.5-2.6-3.7-5.6-3.7-9S9.5 5.6 12 3Z',
  deep: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5-2 5 5M8 11h6M11 8v6',
}

export function SearchModeToggle({ sessionId, useSessions, useInput, usePreferences, toggleSearchMode, t }: Props) {
  const work = useSessions(state => isGenerationWorkSession(state.byId[sessionId]))
  const busy = useInput(state => state.phase !== 'plain')
  const relayout = usePreferences(s=>(s.boundTaskMode??s.taskMode)==='relayout')
  const mode = usePreferences(state => state.searchMode)
  const saving = usePreferences(state => ['loading','saving','error','conflict'].includes(state.status))
  if (!work) return null
  return <div className={css.root} role="group" aria-label={t('search.group')}>
    {(['web', 'deep'] as const).map(value => {
      const on = !relayout && mode === value
      return <button key={value} type="button" className={css.chip} aria-pressed={on} disabled={busy || relayout || saving}
        title={relayout ? t('taskEntry.searchDisabled') : t(value === 'web' ? 'search.web.hint' : 'search.deep.hint')} onClick={() => toggleSearchMode(value)}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d={ICONS[value]} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        {t(value === 'web' ? 'search.web' : 'search.deep')}
        {on ? <svg className={css.check} width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> : null}
      </button>
    })}
  </div>
}
