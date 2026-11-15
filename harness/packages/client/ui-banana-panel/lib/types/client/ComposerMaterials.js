import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useRef, useState } from 'react';
import { previewTargetFromUrl } from "./preview-target.js";
import { MATERIAL_ACCEPT } from "./material-upload.js";
import { bindMaterialInputs } from "./material-input.js";
import css from './ImageModeSelect.module.css';
const phaseKeys = { queued: 'upload.queued', uploading: 'upload.sending', registering: 'upload.registering', uploaded: 'upload.uploaded', attached: 'upload.attached', failed: 'upload.failed', paused: 'upload.paused' };
const errorKeys = { MATERIAL_UPLOAD_LIMIT: 'upload.error.fileLimit', MATERIAL_COUNT_LIMIT: 'upload.error.projectLimit', MATERIAL_BATCH_LIMIT: 'upload.error.batchLimit', UPLOAD_NETWORK: 'upload.error.network', UPLOAD_TIMEOUT: 'upload.error.timeout', UPLOAD_TYPE_UNSUPPORTED: 'upload.error.type', UPLOAD_EMPTY: 'upload.error.empty', UPLOAD_ID_CONFLICT: 'upload.error.conflict', PROJECT_CONTEXT_DENIED: 'upload.error.project', UNAUTHORIZED: 'upload.error.auth' };
/** Material files remain in the Session upload controller; the view handles gestures only. */
export function ComposerMaterials({ sessionId, useSessions, useStore, useMaterialUploads, actions, t, openDetails, intakeFiles, retryUpload, pauseUpload, removeUpload, syncUploads }) {
    const handle = useStore(s => s.projectHandle), projects = useStore(s => s.projects), currentId = useStore(s => s.projectId), uploads = useMaterialUploads(s => s);
    const work = useSessions(s => s.byId[sessionId]?.projectionValues?.agentPreset === 'wuyu-work');
    const state = useRef({ handle, projects, currentId });
    state.current = { handle, projects, currentId };
    const [notice, setNotice] = useState(''), [menuOpen, setMenuOpen] = useState(false), [dragging, setDragging] = useState(false);
    const root = useRef(null), picker = useRef(null), menu = useRef(null), attachButton = useRef(null), uploadRole = useRef('material'), intake = useRef(intakeFiles);
    intake.current = intakeFiles;
    useEffect(() => { const card = root.current?.closest('[data-composer-card]'); if (!card || !work)
        return; return bindMaterialInputs(card, files => { void intake.current(files, 'material', state.current.handle); }, setDragging); }, [work]);
    useEffect(() => {
        const open = (target) => { const s = state.current; if (!s.handle || target.project_id && !s.projects[target.project_id]) {
            setNotice(t('preview.unknownProject'));
            return;
        } actions.openPreview(target); openDetails(); };
        const click = (event) => { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
            return; const anchor = event.target?.closest('a[href]'); if (!anchor)
            return; const target = previewTargetFromUrl(anchor.href, location.href); if (!target)
            return; event.preventDefault(); event.stopPropagation(); open(target); };
        const hash = () => { const target = previewTargetFromUrl(location.href, location.href); if (target)
            open(target); };
        document.addEventListener('click', click, true);
        window.addEventListener('hashchange', hash);
        hash();
        return () => { document.removeEventListener('click', click, true); window.removeEventListener('hashchange', hash); };
    }, [actions, openDetails, t, handle, projects, currentId]);
    const choose = (role) => { if (!picker.current)
        return; uploadRole.current = role; picker.current.accept = MATERIAL_ACCEPT[role]; setMenuOpen(false); picker.current.click(); };
    const limits = uploads.limits, mb = (bytes) => Math.round(bytes / 1024 / 1024), failed = uploads.rows.filter(row => row.phase === 'failed' || row.phase === 'paused').length, attached = uploads.rows.filter(row => row.phase === 'attached').length;
    const error = (code) => t(errorKeys[code] || 'upload.error.service', { mb: limits ? mb(limits.max_file_bytes) : 100, count: limits?.max_project_files || 100 });
    return _jsxs("div", { ref: root, className: css.materialControls, "data-material-upload-controls": true, children: [_jsxs("div", { ref: menu, className: css.attachmentMenu, onBlur: event => { if (!event.currentTarget.contains(event.relatedTarget))
                    setMenuOpen(false); }, onKeyDown: event => { if (event.key === 'Escape') {
                    setMenuOpen(false);
                    attachButton.current?.focus();
                    event.preventDefault();
                }
                else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && menuOpen) {
                    const items = Array.from(menu.current?.querySelectorAll('[role=menuitem]') || []), index = items.indexOf(document.activeElement), next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
                    items[next]?.focus();
                    event.preventDefault();
                } }, children: [_jsx("button", { ref: attachButton, type: "button", className: css.trigger, "aria-haspopup": "menu", "aria-expanded": menuOpen, onClick: () => setMenuOpen(open => !open), children: t('material.attach') }), menuOpen ? _jsx("div", { role: "menu", "aria-label": t('material.attach'), className: css.attachmentOptions, children: ['material', 'template', 'logo'].map(role => _jsx("button", { type: "button", role: "menuitem", onClick: () => choose(role), children: t(role === 'material' ? 'material.roleContent' : role === 'template' ? 'material.roleTemplate' : 'material.roleLogo') }, role)) }) : null] }), _jsx("input", { ref: picker, type: "file", multiple: true, hidden: true, accept: MATERIAL_ACCEPT.material, onChange: event => { const files = Array.from(event.target.files || []); event.target.value = ''; void intakeFiles(files, uploadRole.current, handle); } }), handle ? _jsx("button", { type: "button", className: css.trigger, onClick: () => { actions.openPreview({ view: 'materials' }); openDetails(); }, children: t('preview.open') }) : null, dragging ? _jsx("span", { className: css.uploadNotice, role: "status", children: t('upload.dropHint') }) : null, uploads.rows.length ? _jsxs("details", { className: css.uploadQueue, open: uploads.busy || uploads.syncFailed || failed > 0, children: [_jsx("summary", { children: t('upload.summary', { total: uploads.rows.length, done: attached, failed }) }), _jsxs("section", { className: css.uploadQueueBody, "aria-label": t('upload.queueLabel'), children: [_jsx("p", { className: css.uploadLimits, children: limits ? t('upload.limits', { mb: mb(limits.max_file_bytes), project: limits.max_project_files, batch: limits.max_batch_files, batchMb: mb(limits.max_batch_bytes) }) : t('upload.loadingLimits') }), _jsx("p", { className: css.uploadLimits, children: t('upload.pasteHint') }), _jsx("ol", { className: css.uploadRows, children: uploads.rows.map(row => _jsxs("li", { "data-upload-id": row.id, "data-upload-phase": row.phase, children: [_jsxs("div", { className: css.uploadName, children: [_jsx("strong", { title: row.filename, children: row.filename }), _jsx("small", { children: row.size < 1024 * 1024 ? Math.ceil(row.size / 1024) + ' KB' : (row.size / 1024 / 1024).toFixed(1) + ' MB' })] }), _jsxs("span", { children: [t(phaseKeys[row.phase]), row.phase === 'uploading' ? ' ' + row.percent + '%' : ''] }), row.phase === 'uploading' || row.phase === 'registering' ? _jsx("progress", { max: "100", value: row.percent, "aria-label": t('upload.progress', { filename: row.filename }) }) : null, row.error && row.phase !== 'paused' ? _jsx("p", { className: css.uploadError, children: error(row.error) }) : null, _jsxs("div", { className: css.uploadActions, children: [row.phase === 'queued' || row.phase === 'uploading' || row.phase === 'registering' ? _jsx("button", { type: "button", onClick: () => pauseUpload(row.id), children: t('upload.pause') }) : null, row.phase === 'failed' || row.phase === 'paused' ? _jsx("button", { type: "button", onClick: () => retryUpload(row.id), children: t('upload.retry') }) : null, !['uploading', 'registering', 'uploaded'].includes(row.phase) ? _jsx("button", { type: "button", onClick: () => removeUpload(row.id), children: t('upload.removeRecord') }) : null] })] }, row.id)) }), failed ? _jsx("button", { type: "button", className: css.trigger, onClick: () => retryUpload(), children: t('upload.retryFailed') }) : null, uploads.syncFailed ? _jsxs("div", { role: "status", className: css.uploadError, children: [t('upload.syncPending'), _jsx("button", { type: "button", disabled: uploads.syncing, onClick: () => void syncUploads(), children: t('material.retryContext') })] }) : null] })] }) : null, uploads.intakeError ? _jsx("span", { role: "status", className: css.uploadNotice, children: error(uploads.intakeError) }) : null, notice ? _jsx("span", { role: "status", className: css.uploadNotice, children: notice }) : null] });
}
//# sourceMappingURL=ComposerMaterials.js.map