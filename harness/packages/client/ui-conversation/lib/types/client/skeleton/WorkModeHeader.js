import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import css from './WorkModeHeader.module.css';
/** Independent Work and Chat entry points; no shared-account assumption. */
export function WorkModeHeader({ t }) {
    return (_jsxs("header", { className: css.root, "data-wuyu-mode": "work", children: [_jsxs("div", { className: css.identity, children: [_jsx("span", { className: css.name, children: t('work.name') }), _jsx("span", { className: css.domain, children: t('work.domain') })] }), _jsxs("nav", { className: css.switcher, "aria-label": t('work.modeLabel'), children: [_jsx("a", { href: "/chat", children: t('work.chat') }), _jsx("a", { href: "/work", "aria-current": "page", children: t('work.work') })] })] }));
}
//# sourceMappingURL=WorkModeHeader.js.map