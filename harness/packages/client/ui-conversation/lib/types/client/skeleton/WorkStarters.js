import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import css from './WorkStarters.module.css';
/** Suggestions write an editable draft; they never start generation or approve a page. */
export function WorkStarters({ t, onChoose, disabled }) {
    return (_jsxs("div", { className: css.root, children: [_jsx("div", { className: css.cards, children: ['academic', 'business'].map(kind => (_jsxs("button", { type: "button", disabled: disabled, onClick: () => onChoose(t(`work.${kind}.prompt`)), children: [_jsx("span", { className: css.icon, "aria-hidden": "true", children: kind === 'academic' ? '01' : '02' }), _jsxs("span", { children: [_jsx("strong", { children: t(`work.${kind}.title`) }), _jsx("small", { children: t(`work.${kind}.description`) })] }), _jsx("span", { className: css.arrow, "aria-hidden": "true", children: "\u2197" })] }, kind))) }), _jsx("p", { className: css.hint, children: t('work.reviewHint') })] }));
}
//# sourceMappingURL=WorkStarters.js.map