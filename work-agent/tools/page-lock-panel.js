function __wuyuLockText(key, value) {
  const words = {
    lock: ['锁定此页', 'Lock slide'], unlock: ['已锁定 · 点击解锁', 'Locked · click to unlock'], chip: ['已锁定', 'Locked'],
    summary: ['已锁定 {n} 页：重新生成时这些页保持不变', '{n} slide(s) locked; they stay unchanged when others are regenerated'],
    none: ['可锁定满意的页面，再只重新生成其余页面', 'Lock the slides you like, then regenerate only the others'],
    regenerate: ['重新生成未锁定页', 'Regenerate unlocked slides'], edit: ['在线编辑 PPT', 'Edit PPT online'],
    editHint: ['需先完成可编辑版转换', 'Available after editable conversion'],
    busy: ['正在保存锁定…', 'Saving lock…'], failed: ['锁定未保存，请刷新后重试。', 'The lock was not saved. Refresh and try again.'],
    ask: ['请重新生成未锁定的页面（锁定页：{n}），模板和风格不变，总页数保持不变。修改意见：', 'Please regenerate the unlocked slides (locked: {n}), keeping the template, style and page count. Changes: '],
    editFailed: ['暂时无法打开在线编辑，请在对话中说明“在线编辑”。', 'The editor could not be opened. Ask for online editing in the conversation.'],
  };
  return (words[key]?.[document.documentElement.lang === 'en' ? 1 : 0] || key).replace('{n}', String(value ?? ''));
}
function __wuyuLockedNumbers() { return pt().filter(page => page.locked === true).map(page => page.number); }
function __wuyuLockButton(page) {
  if (!m.handle || page.generation_status !== 'ready') return '';
  const busy = m.lockBusy === page.number;
  return `<button type="button" class="detail-button wuyu-lock-toggle ${page.locked ? 'locked' : ''}" data-page-lock="${page.number}" aria-pressed="${page.locked === true}" ${busy ? 'disabled' : ''}>${$(busy ? __wuyuLockText('busy') : page.locked ? __wuyuLockText('unlock') : __wuyuLockText('lock'))}</button>`;
}
function __wuyuLockChip(page) { return page.locked === true ? `<em class="wuyu-lock-chip">${$(__wuyuLockText('chip'))}</em>` : ''; }
function __wuyuLockToolbar() {
  if (!m.handle || !pt().some(page => page.generation_status === 'ready')) return '';
  const locked = __wuyuLockedNumbers(), office = { available: (m.snapshot.workflow?.phase === 'exported' && m.snapshot.export_capabilities?.editable_pptx === true) || Boolean(m.snapshot.hybrid_export?.id) };
  return `<div class="wuyu-lock-toolbar" role="group"><small role="status">${$(locked.length ? __wuyuLockText('summary', locked.length) : __wuyuLockText('none'))}</small>${locked.length && locked.length < pt().length ? `<button type="button" class="detail-button" data-lock-regenerate>${$(__wuyuLockText('regenerate'))}</button>` : ''}<button type="button" class="detail-button" data-office-edit ${office.available ? '' : `disabled title="${$(__wuyuLockText('editHint'))}"`}>${$(__wuyuLockText('edit'))}</button>${m.lockError ? `<p class="material-warning">${$(m.lockError)}</p>` : ''}</div>`;
}
document.addEventListener('click', async event => {
  const toggle = event.target?.closest?.('button[data-page-lock]');
  if (toggle && !toggle.disabled) {
    const number = Number(toggle.dataset.pageLock), page = pt().find(item => item.number === number);
    if (!page) return;
    m.lockBusy = number; m.lockError = ''; ke();
    const response = await Ti('set_presentation_page_lock', { project_handle: m.handle, numbers: [number], locked: page.locked !== true, source: 'user_ui' }, true);
    m.lockBusy = null;
    if (response) gp(response); else m.lockError = m.lastToolErrors?.set_presentation_page_lock || __wuyuLockText('failed');
    ke(); return;
  }
  if (event.target?.closest?.('button[data-lock-regenerate]')) {
    try { await Rt.sendMessage({ role: 'user', content: [{ type: 'text', text: __wuyuLockText('ask', __wuyuLockedNumbers().join('、')) }] }); }
    catch { m.lockError = __wuyuLockText('failed'); ke(); }
    return;
  }
  const edit = event.target?.closest?.('button[data-office-edit]');
  if (edit && !edit.disabled) {
    const response = await Ti('open_presentation_editor', { project_handle: m.handle }, true);
    if (response?.editor_url) { try { await Rt.openLink({ url: response.editor_url }); return; } catch {} }
    m.lockError = response?.reason || m.lastToolErrors?.open_presentation_editor || __wuyuLockText('editFailed'); ke();
  }
});
