import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * StyleSelect: the composer's slide visual-style control, placed in the
 * `conversation.input.left` tool row next to the image-mode control. It shows
 * the Session's chosen style preset (default / 8 built-ins) and, on selection,
 * prepends a model-facing prefix to the outgoing draft so the Banana Slides
 * agent maps it to the `create_presentation` `template_style` argument. The
 * prefix rides the user message text (model-visible and logged); `default`
 * adds no prefix because it means "no style override".
 */
import { useState } from 'react';
import { IconChevronDownOutline14, Menu, } from '@deepseek-ai/dsh-client-ui-primitives';
import css from './ImageModeSelect.module.css';
/** Selectable styles, in menu order. `name` is the verbatim model-facing Chinese label. */
const STYLES = [
    { id: 'default', name: '默认' },
    { id: 'business-simple', name: '简约商务' },
    { id: 'tech-modern', name: '现代科技' },
    { id: 'academic-formal', name: '严谨学术' },
    { id: 'creative-fun', name: '活泼创意' },
    { id: 'minimalist-clean', name: '极简清爽' },
    { id: 'luxury-premium', name: '高端奢华' },
    { id: 'nature-fresh', name: '自然清新' },
    { id: 'gradient-vibrant', name: '渐变活力' },
];
/**
 * Model-facing prefix prepended to the outgoing message for each non-default
 * style. Kept verbatim (not localized): the agent matches the trailing id to a
 * built-in `template_style` preset.
 */
const STYLE_PREFIX = Object.fromEntries(STYLES.map(({ id, name }) => [id, id === 'default' ? '' : `（PPT风格：${name} / ${id}）`]));
/**
 * Strip any leading style prefix this control previously inserted. Not
 * anchored so it coexists with the image-mode prefix already in the draft.
 */
const PREFIX_RE = /\s*（PPT风格：[^）]*）\s*/;
/** Composer slide visual-style dropdown. */
export function StyleSelect({ useInput, inputActions, useStore, actions, t, }) {
    const [open, setOpen] = useState(false);
    const styleMode = useStore(s => s.styleMode);
    const draft = useInput(s => s.draft);
    const labelOf = (id) => t(`style.${id}`);
    const detailOf = (id) => t(`style.${id}.detail`);
    const items = STYLES.map(({ id }) => ({
        id,
        label: (_jsxs("span", { className: css.option, children: [_jsx("span", { className: css.optionLabel, children: labelOf(id) }), _jsx("span", { className: css.optionDetail, children: detailOf(id) })] })),
    }));
    const choose = (id) => {
        setOpen(false);
        const mode = id;
        const stripped = draft.replace(PREFIX_RE, '');
        const next = STYLE_PREFIX[mode] === '' ? stripped : `${STYLE_PREFIX[mode]}${stripped}`;
        inputActions.setDraft(next);
        actions.setStyleMode(mode);
    };
    return (_jsx(Menu, { open: open, items: items, selectedId: styleMode, onSelect: choose, onClose: () => { setOpen(false); }, side: "top", anchor: (_jsxs("button", { type: "button", className: css.trigger, "aria-label": t('style.label'), title: detailOf(styleMode), onClick: () => { setOpen(!open); }, children: [_jsx("span", { className: css.triggerLabel, children: labelOf(styleMode) }), _jsx("span", { className: open ? css.chevronOpen : css.chevron, "aria-hidden": true, children: _jsx(IconChevronDownOutline14, {}) })] })) }));
}
//# sourceMappingURL=StyleSelect.js.map