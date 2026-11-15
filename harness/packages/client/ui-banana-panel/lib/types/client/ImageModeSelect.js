import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * ImageModeSelect: the composer's slide image-generation mode control, placed
 * in the `conversation.input.left` tool row. It shows the Session's chosen
 * mode (stable/creative/best) and, on selection, prepends a model-facing
 * prefix to the outgoing draft so the Banana Slides agent maps it to the
 * `create_presentation` `image_mode` argument. The prefix rides the user
 * message text (model-visible and logged); `best` adds no prefix because it
 * is the tool's default.
 */
import { useState } from 'react';
import { IconChevronDownOutline14, Menu, } from '@deepseek-ai/dsh-client-ui-primitives';
import css from './ImageModeSelect.module.css';
/**
 * Model-facing prefix prepended to the outgoing message for each non-default
 * mode. Kept verbatim (not localized): the agent matches these to the
 * `image_mode` enum values stable/creative/best.
 */
const MODE_PREFIX = {
    stable: '（图片生成模式：稳定 / stable）',
    creative: '（图片生成模式：创意 / creative）',
    best: '',
};
/**
 * Strip any image-mode prefix this control previously inserted. Not anchored
 * so it coexists with the style prefix (PPT风格) already in the draft.
 */
const PREFIX_RE = /\s*（图片生成模式：[^）]*）\s*/;
/** Selectable modes, in menu order. */
const MODES = ['stable', 'creative', 'best'];
/** Composer slide image-mode dropdown. */
export function ImageModeSelect({ useInput, inputActions, useStore, actions, t, }) {
    const [open, setOpen] = useState(false);
    const imageMode = useStore(s => s.imageMode);
    const draft = useInput(s => s.draft);
    const labelOf = (mode) => mode === 'stable' ? t('mode.stable') : mode === 'creative' ? t('mode.creative') : t('mode.best');
    const detailOf = (mode) => mode === 'stable' ? t('mode.stable.detail') : mode === 'creative' ? t('mode.creative.detail') : t('mode.best.detail');
    const items = MODES.map(mode => ({
        id: mode,
        label: (_jsxs("span", { className: css.option, children: [_jsx("span", { className: css.optionLabel, children: labelOf(mode) }), _jsx("span", { className: css.optionDetail, children: detailOf(mode) })] })),
    }));
    const choose = (id) => {
        setOpen(false);
        const mode = id;
        const stripped = draft.replace(PREFIX_RE, '');
        const next = MODE_PREFIX[mode] === '' ? stripped : `${MODE_PREFIX[mode]}${stripped}`;
        inputActions.setDraft(next);
        actions.setImageMode(mode);
    };
    return (_jsx(Menu, { open: open, items: items, selectedId: imageMode, onSelect: choose, onClose: () => { setOpen(false); }, side: "top", anchor: (_jsxs("button", { type: "button", className: css.trigger, "aria-label": t('mode.label'), title: detailOf(imageMode), onClick: () => { setOpen(!open); }, children: [_jsx("span", { className: css.triggerLabel, children: labelOf(imageMode) }), _jsx("span", { className: open ? css.chevronOpen : css.chevron, "aria-hidden": true, children: _jsx(IconChevronDownOutline14, {}) })] })) }));
}
//# sourceMappingURL=ImageModeSelect.js.map