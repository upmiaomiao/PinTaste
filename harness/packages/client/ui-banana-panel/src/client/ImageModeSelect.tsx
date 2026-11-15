/**
 * ImageModeSelect: the composer's slide image-generation mode control, placed
 * in the `conversation.input.left` tool row. It shows the Session's chosen
 * mode (stable/creative/best) and, on selection, prepends a model-facing
 * prefix to the outgoing draft so the Banana Slides agent maps it to the
 * `create_presentation` `image_mode` argument. The prefix rides the user
 * message text (model-visible and logged); `best` adds no prefix because it
 * is the tool's default.
 */
import { useState } from 'react'
import {
  IconChevronDownOutline14, Menu,
} from '@deepseek-ai/dsh-client-ui-primitives'
import type { MenuEntry } from '@deepseek-ai/dsh-client-ui-primitives'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls the ui-conversation SlotMap merge for the input.left seat.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { createBananaPanelStore, ImageMode } from './store.ts'
import css from './ImageModeSelect.module.css'

/**
 * Model-facing prefix prepended to the outgoing message for each non-default
 * mode. Kept verbatim (not localized): the agent matches these to the
 * `image_mode` enum values stable/creative/best.
 */
const MODE_PREFIX: Record<ImageMode, string> = {
  stable: '（图片生成模式：稳定 / stable）',
  creative: '（图片生成模式：创意 / creative）',
  best: '',
}

/**
 * Strip any image-mode prefix this control previously inserted. Not anchored
 * so it coexists with the style prefix (PPT风格) already in the draft.
 */
const PREFIX_RE = /\s*（图片生成模式：[^）]*）\s*/

/** Selectable modes, in menu order. */
const MODES: readonly ImageMode[] = ['stable', 'creative', 'best']

type ImageModeSelectProps =
  PropsRuntime<'conversation.input.left'>
  & PropsStore<ReturnType<typeof createBananaPanelStore>>
  & PropsLocale<'bananaPanel'>

/** Composer slide image-mode dropdown. */
export function ImageModeSelect({
  useInput, inputActions, useStore, actions, t,
}: ImageModeSelectProps) {
  const [open, setOpen] = useState(false)
  const imageMode = useStore(s => s.imageMode)
  const draft = useInput(s => s.draft)

  const labelOf = (mode: ImageMode): string =>
    mode === 'stable' ? t('mode.stable') : mode === 'creative' ? t('mode.creative') : t('mode.best')

  const detailOf = (mode: ImageMode): string =>
    mode === 'stable' ? t('mode.stable.detail') : mode === 'creative' ? t('mode.creative.detail') : t('mode.best.detail')

  const items: MenuEntry[] = MODES.map(mode => ({
    id: mode,
    label: (
      <span className={css.option}>
        <span className={css.optionLabel}>{labelOf(mode)}</span>
        <span className={css.optionDetail}>{detailOf(mode)}</span>
      </span>
    ),
  }))

  const choose = (id: string): void => {
    setOpen(false)
    const mode = id as ImageMode
    const stripped = draft.replace(PREFIX_RE, '')
    const next = MODE_PREFIX[mode] === '' ? stripped : `${MODE_PREFIX[mode]}${stripped}`
    inputActions.setDraft(next)
    actions.setImageMode(mode)
  }

  return (
    <Menu
      open={open}
      items={items}
      selectedId={imageMode}
      onSelect={choose}
      onClose={() => { setOpen(false) }}
      side="top"
      anchor={(
        <button
          type="button"
          className={css.trigger}
          aria-label={t('mode.label')}
          title={detailOf(imageMode)}
          onClick={() => { setOpen(!open) }}
        >
          <span className={css.triggerLabel}>{labelOf(imageMode)}</span>
          <span className={open ? css.chevronOpen : css.chevron} aria-hidden>
            <IconChevronDownOutline14 />
          </span>
        </button>
      )}
    />
  )
}
