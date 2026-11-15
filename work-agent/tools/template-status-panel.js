const __wuyuTemplateWatch = { handle: '', seen: new Set(), started: 0, polls: 0, timer: null, stopped: false };

function __wuyuTemplateText(key) {
  const text = {
    ready: ['查看版式', 'View layout'], failed: ['解析未完成，请重新上传或检查文件', 'Parsing failed. Re-upload or check the file.'],
    noPreview: ['解析已结束，但没有可用预览', 'Parsing ended without a usable preview'],
    cancelled: ['解析已取消', 'Parsing cancelled'], pending: ['解析中', 'Parsing…'],
    slow: ['解析时间较长，已暂停自动刷新；可手动刷新解析状态。', 'Parsing is taking longer. Automatic refresh has paused; refresh the parsing status manually.'],
    unavailable: ['模板参考页不足，请选择其他模板。', 'This template has too few reference pages. Choose another template.'],
    confirmed: ['已确认这套模板', 'Template confirmed'],
    privateReview: ['查看实际版式后，确认使用', 'Review the available layout, then confirm'],
    libraryReview: ['查看至少4页版式后，确认使用', 'Review at least 4 layouts, then confirm'],
    missingReview: ['可用预览不足，请选择其他模板', 'Too few previews. Choose another template'],
    draftChanged: ['资料或需求已变化，需要根据当前资料重新整理草稿；请在左侧对话中提出重新整理。已有 PPT 仍可下载。', 'Materials or requirements changed. Ask in the chat to reorganize the draft using the current materials. Existing PPT files remain available.'],
  };
  return text[key][document.documentElement.lang === 'en' ? 1 : 0];
}

function __wuyuTemplateMaterialState(material) {
  return __wuyuTemplateText(material.ready ? 'ready' : material.status === 'completed' ? 'noPreview' : material.status === 'failed' ? 'failed' : material.status === 'cancelled' ? 'cancelled' : 'pending');
}

function __wuyuTemplatePrivate() {
  return Boolean(k.materialId || ['private_generated_design', 'private_uploaded_template'].includes(Oe()?.preview_kind));
}

function __wuyuTemplateRequired() {
  return __wuyuTemplatePrivate() ? 1 : 4;
}

function __wuyuTemplateConfirmationHint() {
  if (wp()) return __wuyuTemplateText('confirmed');
  if (!__wuyuTemplatePrivate() && Oe()?.reference_sufficient !== true) return __wuyuTemplateText('unavailable');
  if (pr().length < __wuyuTemplateRequired()) return __wuyuTemplateText('missingReview');
  return __wuyuTemplateText(__wuyuTemplatePrivate() ? 'privateReview' : 'libraryReview');
}

function __wuyuTemplateWatchReset() {
  clearTimeout(__wuyuTemplateWatch.timer);
  Object.assign(__wuyuTemplateWatch, { handle: m.handle || '', seen: new Set(), started: 0, polls: 0, timer: null });
}

function __wuyuTemplateSchedule() {
  const watch = __wuyuTemplateWatch;
  clearTimeout(watch.timer); watch.timer = null;
  if (watch.handle !== m.handle) __wuyuTemplateWatchReset();
  if (watch.stopped || !m.connected || !m.handle || m.previewTab !== 'templates' || k.tab !== 'upload' || document.visibilityState === 'hidden') return;
  const pending = (k.snapshot.materials || []).filter(row => ['pending', 'processing', 'queued', 'running'].includes(row.status));
  if (!pending.length) { watch.started = 0; watch.seen.clear(); watch.polls = 0; return; }
  if (pending.some(row => !watch.seen.has(row.id))) { watch.started = Date.now(); watch.polls = 0; for (const row of pending) watch.seen.add(row.id); }
  if (Date.now() - watch.started >= 120000 || watch.polls >= 30 || k.loading) return;
  const handle = m.handle, sequence = k.sequence;
  watch.timer = setTimeout(() => {
    watch.timer = null;
    if (watch.stopped || handle !== m.handle || sequence !== k.sequence || k.loading || document.visibilityState === 'hidden' || m.previewTab !== 'templates' || k.tab !== 'upload') return;
    if (Date.now() - watch.started >= 120000) { ze(); return; }
    watch.polls++;
    void $e({ operation: 'materials' });
  }, watch.polls < 5 ? 3000 : 5000);
}

function __wuyuTemplateEnsure() {
  if (__wuyuTemplateWatch.handle !== m.handle) __wuyuTemplateWatchReset();
  if (!__wuyuTemplateWatch.timer) __wuyuTemplateSchedule();
}

function __wuyuTemplateWatchNotice() {
  const pending = (k.snapshot.materials || []).some(row => ['pending', 'processing', 'queued', 'running'].includes(row.status));
  return pending && __wuyuTemplateWatch.started && (Date.now() - __wuyuTemplateWatch.started >= 120000 || __wuyuTemplateWatch.polls >= 30) ? '<p class="preview-note" role="status" data-template-watch-paused>' + $(__wuyuTemplateText('slow')) + '</p>' : '';
}

function __wuyuReadingResetNotice() {
  const reading = m.snapshot.reading || {};
  if (reading.status !== 'idle' || !(reading.draft_revision > 0) || !Zs(m.snapshot).every(job => !['queued', 'running', 'pending'].includes(job.status)) || !(m.snapshot.materials || []).some(row => row.role === 'material' && row.selected_for_content !== false)) return '';
  return '<p class="coverage-notice" role="status" data-reading-reset-notice>' + $(__wuyuTemplateText('draftChanged')) + '</p>';
}

document.addEventListener('visibilitychange', () => __wuyuTemplateSchedule());
window.addEventListener('pagehide', () => { __wuyuTemplateWatch.stopped = true; clearTimeout(__wuyuTemplateWatch.timer); });
window.addEventListener('pageshow', () => { __wuyuTemplateWatch.stopped = false; __wuyuTemplateSchedule(); });
