const __wuyuDeletingMaterials = new Set();
let __wuyuMaterialDeleteIntent = null;

function __wuyuMaterialText(key) {
  const copy = {
    delete: ['删除材料', 'Delete material'],
    retry: ['重试清理', 'Retry cleanup'],
    cancel: ['取消', 'Cancel'],
    commit: ['永久删除', 'Delete permanently'],
    title: ['确认删除材料', 'Confirm material deletion'],
    busy: ['请先等待上传结束或停止当前项目任务，再删除材料。', 'Finish uploading or stop the current project task before deleting materials.'],
    capacity: ['材料解析超过项目容量，请缩小文件或删除不需要的资料。', 'Material parsing exceeds project capacity. Reduce the file or delete unneeded materials.'],
    confirm: ['将永久删除所选原文件及解析预览，并清除本项目阅读草稿和缓存。已生成的 PPT 和对话历史保留。此操作无法恢复。', 'Permanently delete the selected original file and parsed previews, and clear this project’s reading drafts and caches. Generated PPT files and chat history remain. This cannot be undone.'],
    pending: ['材料文件清理未完成，请重试；完成前暂停后续处理。', 'Material cleanup is incomplete. Retry cleanup before continuing.'],
    error: ['删除未完成，请刷新材料列表后重试。', 'Deletion did not complete. Refresh the material list and retry.'],
    owner: ['只能删除自己项目中上传的材料。', 'Only uploaded materials in your own project can be deleted.'],
    conflict: ['项目状态已变化，请刷新后重新确认删除。', 'The project changed. Refresh and confirm deletion again.'],
    usage: ['项目材料容量', 'Project material capacity'],
  };
  return copy[key][document.documentElement.lang === 'en' ? 1 : 0];
}

function __wuyuMaterialBusy() {
  return __wuyuDeletingMaterials.size > 0 || (m.snapshot.jobs || []).some(job => ['queued', 'running'].includes(job.status));
}

function __wuyuMaterialDeleteButton(material) {
  if (!material.id || material.origin === 'web_research') return '';
  const busy = __wuyuMaterialBusy();
  return '<div class="evidence-controls"><button class="detail-button" data-material-delete="' + $(material.id) + '" ' +
    (!m.connected || busy ? 'disabled' : '') + '>' + $(__wuyuMaterialText('delete')) + '</button>' +
    (busy ? '<small>' + $(__wuyuMaterialText('busy')) + '</small>' : '') +
    (material.error_code === 'MATERIAL_PROJECT_LIMIT' ? '<p class="material-warning">' + $(__wuyuMaterialText('capacity')) + '</p>' : '') + '</div>';
}

function __wuyuMaterialSummary() {
  const limit = m.snapshot.material_limits || {}, current = m.snapshot.material_usage || {}, en = document.documentElement.lang === 'en';
  const number = value => Number(value || 0).toLocaleString(en ? 'en' : 'zh-CN');
  const mib = value => (Number(value || 0) / 1048576).toFixed(1);
  const text = en ? number(current.files) + ' / ' + number(limit.max_project_files) + ' files · ' + mib(current.bytes) + ' / ' + mib(limit.max_project_bytes) +
    ' MB · ' + number(current.text_chars) + ' / ' + number(limit.max_project_text_chars) + ' extracted characters · ' + number(current.chunks) + ' / ' + number(limit.max_project_chunks) + ' text segments' :
    number(current.files) + ' / ' + number(limit.max_project_files) + ' 个文件 · ' + mib(current.bytes) + ' / ' + mib(limit.max_project_bytes) +
    ' MB · ' + number(current.text_chars) + ' / ' + number(limit.max_project_text_chars) + ' 提取字符 · ' + number(current.chunks) + ' / ' + number(limit.max_project_chunks) + ' 个文字片段';
  const pending = (m.snapshot.material_deletions || []).map(entry => '<div class="material-warning" data-material-cleanup>' + $(__wuyuMaterialText('pending')) +
    ' <button class="detail-button" data-material-delete="' + $(entry.material_id) + '" ' + (!m.connected || __wuyuMaterialBusy() ? 'disabled' : '') + '>' + $(__wuyuMaterialText('retry')) + '</button></div>').join('');
  if (__wuyuMaterialDeleteIntent && __wuyuMaterialDeleteIntent.handle !== m.handle) __wuyuMaterialDeleteIntent = null;
  const intent = __wuyuMaterialDeleteIntent;
  const confirmation = intent ? '<section class="detail-card" role="dialog" aria-modal="false" aria-labelledby="material-delete-title" data-material-delete-confirm>' +
    '<h3 id="material-delete-title">' + $(__wuyuMaterialText('title')) + '</h3><strong>' + $(intent.filename) + '</strong><p>' + $(__wuyuMaterialText('confirm')) + '</p>' +
    '<div class="evidence-controls"><button class="detail-button" data-material-delete-cancel>' + $(__wuyuMaterialText('cancel')) + '</button>' +
    '<button class="detail-button" data-material-delete-commit ' + (__wuyuMaterialBusy() || !m.connected ? 'disabled' : '') + '>' + $(__wuyuMaterialText('commit')) + '</button></div></section>' : '';
  return '<div class="coverage-notice" data-material-capacity><strong>' + $(__wuyuMaterialText('usage')) + '</strong><p>' + $(text) + '</p></div>' + pending + confirmation;
}

async function __wuyuCommitMaterialDelete(intent) {
  const { id, handle, revision } = intent;
  if (!m.connected || handle !== m.handle || __wuyuMaterialBusy()) return;
  __wuyuMaterialDeleteIntent = null;
  __wuyuDeletingMaterials.add(id);
  m.onchange();
  try {
    const reply = await j$({ operation: 'delete_material', material_id: id, confirmed: true, expected_revision: revision, request_id: crypto.randomUUID() });
    if (handle !== m.handle) return;
    const result = reply.data;
    if (result?.ok !== true) {
      const code = result?.code;
      _$( __wuyuMaterialText(code === 'MATERIAL_IN_USE' ? 'busy' : code === 'MATERIAL_DELETE_PENDING' ? 'pending' : code === 'MATERIAL_DELETE_FORBIDDEN' ? 'owner' : code === 'REVISION_CONFLICT' ? 'conflict' : 'error'));
    } else xe.clear();
    await et(true);
  } catch {
    if (handle === m.handle) {
      _$( __wuyuMaterialText('error'));
      await et(true);
    }
  } finally {
    __wuyuDeletingMaterials.delete(id);
    if (handle === m.handle) m.onchange();
  }
}

document.addEventListener('click', event => {
  const button = event.target instanceof Element ? event.target.closest('[data-material-delete],[data-material-delete-cancel],[data-material-delete-commit]') : null;
  if (!button || button.disabled) return;
  event.preventDefault();
  event.stopPropagation();
  if (button.hasAttribute('data-material-delete-cancel')) {
    __wuyuMaterialDeleteIntent = null;
    m.onchange();
    return;
  }
  if (button.hasAttribute('data-material-delete-commit')) {
    if (__wuyuMaterialDeleteIntent) void __wuyuCommitMaterialDelete(__wuyuMaterialDeleteIntent);
    return;
  }
  const id = button.getAttribute('data-material-delete');
  const material = (m.snapshot.materials || []).find(row => row.id === id);
  const pending = (m.snapshot.material_deletions || []).find(row => row.material_id === id);
  if (!id || !m.connected || !m.handle || (!material && !pending) || __wuyuMaterialBusy()) return;
  __wuyuMaterialDeleteIntent = { id, handle: m.handle, revision: m.snapshot.revision, filename: material?.filename || id };
  m.onchange();
  document.querySelector('[data-material-delete-cancel]')?.focus();
}, true);

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && __wuyuMaterialDeleteIntent) {
    __wuyuMaterialDeleteIntent = null;
    m.onchange();
  }
});
