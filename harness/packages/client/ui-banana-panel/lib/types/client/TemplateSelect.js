import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
/**
 * TemplateSelect: the composer's slide template-image control, placed in the
 * `conversation.input.left` tool row next to the style and image-mode controls.
 * It offers the six built-in demo decks plus the user's own uploaded template
 * images, and an "upload template" row. On selection it prepends a model-facing
 * prefix to the outgoing draft so the Banana Slides agent maps it to
 * `template_preset` (preset) or `user_template_id` (uploaded). The prefix rides
 * the user message text (model-visible and logged); `default` adds no prefix.
 */
import { useEffect, useRef, useState } from 'react';
import { IconChevronDownOutline14, Menu, } from '@deepseek-ai/dsh-client-ui-primitives';
import { connectBananaClient } from "./banana-mcp.js";
import { BANANA_MCP_TOKEN, BANANA_MCP_URL } from "./config.js";
import css from './ImageModeSelect.module.css';
/** Selectable built-in templates, in menu order. `name` is the verbatim model-facing Chinese label. */
const TEMPLATES = [
    { id: 'default', name: '无模板' },
    { id: 'business-plan', name: '商业计划书' },
    { id: 'circular-economy', name: '循环经济' },
    { id: 'phd-defense-1', name: '博士开题答辩 1' },
    { id: 'phd-defense-2', name: '博士开题答辩 2' },
    { id: 'xuejian', name: '学件材料' },
    { id: 'solid-waste', name: '固体废物治理' },
];
/**
 * Model-facing prefix prepended to the outgoing message for each non-default
 * preset. Kept verbatim (not localized): the agent matches the trailing id to a
 * built-in `template_preset`.
 */
const TEMPLATE_PREFIX = Object.fromEntries(TEMPLATES.map(({ id, name }) => [id, id === 'default' ? '' : `（PPT模板图：${name} / ${id}）`]));
/** Model-facing prefix for a user-uploaded template (id is the backend UUID). */
function userTemplatePrefix(template) {
    return `（PPT用户模板：${template.name} / ${template.id}）`;
}
/** Strip a previously inserted template-image prefix (preset or uploaded). */
const PRESET_PREFIX_RE = /\s*（PPT模板图：[^）]*）\s*/;
const USER_PREFIX_RE = /\s*（PPT用户模板：[^）]*）\s*/;
/** Menu id for the trailing upload row (cannot collide with preset ids or UUIDs). */
const UPLOAD_ID = '__upload_template__';
/** Composer slide template-image dropdown with preset + user templates and upload. */
export function TemplateSelect({ useInput, inputActions, useStore, actions, t, }) {
    const [open, setOpen] = useState(false);
    const [userTemplates, setUserTemplates] = useState([]);
    const [uploading, setUploading] = useState(false);
    const clientRef = useRef(null);
    const fileRef = useRef(null);
    const templateMode = useStore(s => s.templateMode);
    const userTemplate = useStore(s => s.userTemplate);
    const draft = useInput(s => s.draft);
    // Connect once and load the user's uploaded templates; leave the list empty
    // when the service is unreachable (the preset rows still work).
    useEffect(() => {
        let disposed = false;
        let client = null;
        void (async () => {
            try {
                client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN);
                if (disposed) {
                    client.dispose();
                    return;
                }
                clientRef.current = client;
                const result = await client.callTool('list_user_templates', {});
                const data = result.structuredContent;
                setUserTemplates((data?.templates ?? [])
                    .filter((item) => typeof item.template_id === 'string')
                    .map((item) => ({ id: item.template_id, name: item.name?.trim() || item.template_id })));
            }
            catch {
                /* unreachable service: keep an empty list, presets remain usable */
            }
        })();
        return () => {
            disposed = true;
            client?.dispose();
            clientRef.current = null;
        };
    }, []);
    const labelOf = (id) => t(`template.${id}`);
    const detailOf = (id) => t(`template.${id}.detail`);
    const items = [
        { type: 'label', id: 'preset-label', text: t('template.presetLabel') },
        ...TEMPLATES.map(({ id }) => ({
            id,
            label: (_jsxs("span", { className: css.option, children: [_jsx("span", { className: css.optionLabel, children: labelOf(id) }), _jsx("span", { className: css.optionDetail, children: detailOf(id) })] })),
        })),
        { type: 'separator', id: 'sep-upload' },
        { type: 'label', id: 'my-label', text: t('template.myLabel') },
        ...(userTemplates.length === 0
            ? [{ type: 'label', id: 'my-empty', text: t('template.empty') }]
            : userTemplates.map((template) => ({
                id: template.id,
                label: (_jsx("span", { className: css.option, children: _jsx("span", { className: css.optionLabel, children: template.name }) })),
            }))),
        { type: 'separator', id: 'sep-items' },
        {
            id: UPLOAD_ID,
            disabled: uploading,
            label: _jsx("span", { className: css.optionLabel, children: uploading ? t('template.uploading') : t('template.upload') }),
        },
    ];
    const stripPrefix = (value) => value.replace(PRESET_PREFIX_RE, '').replace(USER_PREFIX_RE, '');
    const choose = (id) => {
        setOpen(false);
        if (id === UPLOAD_ID) {
            fileRef.current?.click();
            return;
        }
        const preset = TEMPLATES.find((entry) => entry.id === id);
        if (preset !== undefined) {
            const stripped = stripPrefix(draft);
            const next = TEMPLATE_PREFIX[preset.id] === '' ? stripped : `${TEMPLATE_PREFIX[preset.id]}${stripped}`;
            inputActions.setDraft(next);
            actions.setTemplateMode(preset.id);
            actions.setUserTemplate(null);
            return;
        }
        const uploaded = userTemplates.find((entry) => entry.id === id);
        if (uploaded !== undefined) {
            inputActions.setDraft(`${userTemplatePrefix(uploaded)}${stripPrefix(draft)}`);
            actions.setUserTemplate(uploaded);
            actions.setTemplateMode('default');
        }
    };
    const onFile = (event) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (file === null || file === undefined)
            return;
        void (async () => {
            try {
                setUploading(true);
                const base64 = await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(String(reader.result));
                    reader.onerror = () => reject(reader.error);
                    reader.readAsDataURL(file);
                });
                let client = clientRef.current;
                if (client === null) {
                    client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN);
                    clientRef.current = client;
                }
                const name = file.name.replace(/\.[^.]+$/, '');
                const result = await client.callTool('upload_user_template', { image_base64: base64, name });
                const created = result.structuredContent;
                if (typeof created?.template_id === 'string') {
                    const ref = { id: created.template_id, name: created.name?.trim() || created.template_id };
                    setUserTemplates((previous) => [...previous.filter((entry) => entry.id !== ref.id), ref]);
                    inputActions.setDraft(`${userTemplatePrefix(ref)}${stripPrefix(draft)}`);
                    actions.setUserTemplate(ref);
                    actions.setTemplateMode('default');
                }
            }
            catch {
                /* upload failure is visible through the unchanged list */
            }
            finally {
                setUploading(false);
            }
        })();
    };
    const selectedId = userTemplate !== null ? userTemplate.id : templateMode;
    const selectedDetail = userTemplate !== null ? userTemplate.name : detailOf(templateMode);
    return (_jsxs(_Fragment, { children: [_jsx(Menu, { open: open, items: items, selectedId: selectedId, onSelect: choose, onClose: () => { setOpen(false); }, side: "top", anchor: (_jsxs("button", { type: "button", className: css.trigger, "aria-label": t('template.label'), title: selectedDetail, onClick: () => { setOpen(!open); }, children: [_jsx("span", { className: css.triggerLabel, children: userTemplate !== null ? userTemplate.name : labelOf(templateMode) }), _jsx("span", { className: open ? css.chevronOpen : css.chevron, "aria-hidden": true, children: _jsx(IconChevronDownOutline14, {}) })] })) }), _jsx("input", { ref: fileRef, type: "file", accept: "image/png,image/jpeg,image/webp", style: { display: 'none' }, "aria-hidden": true, tabIndex: -1, onChange: onFile })] }));
}
//# sourceMappingURL=TemplateSelect.js.map