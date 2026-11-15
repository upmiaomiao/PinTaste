/**
 * StyleSelect: the composer's slide visual-style control, placed in the
 * `conversation.input.left` tool row next to the image-mode control. It shows
 * the Session's chosen style preset (default / 8 built-ins) and, on selection,
 * prepends a model-facing prefix to the outgoing draft so the Banana Slides
 * agent maps it to the `create_presentation` `template_style` argument. The
 * prefix rides the user message text (model-visible and logged); `default`
 * adds no prefix because it means "no style override".
 */
import { useState } from 'react'
import {
  IconChevronDownOutline14, Menu,
} from '@deepseek-ai/dsh-client-ui-primitives'
import type { MenuEntry } from '@deepseek-ai/dsh-client-ui-primitives'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls the ui-conversation SlotMap merge for the input.left seat.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { createBananaPanelStore, StyleMode } from './store.ts'
import css from './ImageModeSelect.module.css'

/** Selectable styles, in menu order. `name` is the verbatim model-facing Chinese label. */
const STYLES: readonly { id: StyleMode; name: string }[] = [
  { id: 'default', name: '默认' },
  { id: 'business-simple', name: '简约商务' },
  { id: 'tech-modern', name: '现代科技' },
  { id: 'academic-formal', name: '严谨学术' },
  { id: 'creative-fun', name: '活泼创意' },
  { id: 'minimalist-clean', name: '极简清爽' },
  { id: 'luxury-premium', name: '高端奢华' },
  { id: 'nature-fresh', name: '自然清新' },
  { id: 'gradient-vibrant', name: '渐变活力' },
]

/**
 * Model-facing prefix prepended to the outgoing message for each non-default
 * style. Kept verbatim (not localized): the agent matches the trailing id to a
 * built-in `template_style` preset.
 */
const STYLE_PREFIX: Record<StyleMode, string> = Object.fromEntries(
  STYLES.map(({ id, name }) => [id, id === 'default' ? '' : `（PPT风格：${name} / ${id}）`]),
) as Record<StyleMode, string>

/**
 * Strip any leading style prefix this control previously inserted. Not
 * anchored so it coexists with the image-mode prefix already in the draft.
 */
const PREFIX_RE = /\s*（PPT风格：[^）]*）\s*/

type StyleSelectProps =
  PropsRuntime<'conversation.input.left'>
  & PropsStore<ReturnType<typeof createBananaPanelStore>>
  & PropsLocale<'bananaPanel'>

/** Composer slide visual-style dropdown. */
export function StyleSelect({
  useInput, inputActions, useStore, actions, t,
}: StyleSelectProps) {
  const [open, setOpen] = useState(false)
  const styleMode = useStore(s => s.styleMode)
  const draft = useInput(s => s.draft)

  const labelOf = (id: StyleMode): string => t(`style.${id}` as const)

  const detailOf = (id: StyleMode): string => t(`style.${id}.detail` as const)

  const items: MenuEntry[] = STYLES.map(({ id }) => ({
    id,
    label: (
      <span className={css.option}>
        <span className={css.optionLabel}>{labelOf(id)}</span>
        <span className={css.optionDetail}>{detailOf(id)}</span>
      </span>
    ),
  }))

  const choose = (id: string): void => {
    setOpen(false)
    const mode = id as StyleMode
    const stripped = draft.replace(PREFIX_RE, '')
    const next = STYLE_PREFIX[mode] === '' ? stripped : `${STYLE_PREFIX[mode]}${stripped}`
    inputActions.setDraft(next)
    actions.setStyleMode(mode)
  }

  return (
    <Menu
      open={open}
      items={items}
      selectedId={styleMode}
      onSelect={choose}
      onClose={() => { setOpen(false) }}
      side="top"
      anchor={(
        <button
          type="button"
          className={css.trigger}
          aria-label={t('style.label')}
          title={detailOf(styleMode)}
          onClick={() => { setOpen(!open) }}
        >
          <span className={css.triggerLabel}>{labelOf(styleMode)}</span>
          <span className={open ? css.chevronOpen : css.chevron} aria-hidden>
            <IconChevronDownOutline14 />
          </span>
        </button>
      )}
    />
  )
}
