function __wuyuHistoryText(key, number) {
  const words = {
    title: ['历史图片', 'Image history'], page: ['第 {n} 页', 'Slide {n}'], record: ['图像记录 {n}', 'Image record {n}'],
    hint: ['重新生成会保留已保存的旧页图和样张。这里仅供查看和下载，不改变当前版本或审核状态。', 'Regeneration retains saved slide images and samples. Viewing and downloading history does not change the current image or its approval.'],
    empty: ['这个项目还没有保存过生成页图或样张。', 'No generated slide images or samples have been saved in this project.'],
    filter: ['按页筛选历史图片', 'Filter image history by slide'], all: ['全部页面', 'All slides'], current: ['当前页图', 'Current slide image'],
    preview: ['查看原图', 'Open original'], download: ['下载图片', 'Download image'], previous: ['上一组', 'Previous'], next: ['下一组', 'Next'],
    unavailable: ['图片暂时无法加载，可刷新详情后重试。历史记录仍保留。', 'The image could not be loaded. Refresh the details and try again; its history record is retained.'],
    sample: ['样张', 'Sample'], missingTime: ['时间未记录', 'Time unavailable'], count: ['共 {n} 张已保存图片', '{n} saved images'],
  };
  return (words[key]?.[document.documentElement.lang === 'en' ? 1 : 0] || key).replace('{n}', String(number ?? ''));
}
function __wuyuHistoryState(snapshot) {
  const project = snapshot.project_id || '';
  if (m.previewHistory?.project !== project) m.previewHistory = { project, page: 'all', offset: 0 };
  return m.previewHistory;
}
function __wuyuHistoryRecords(snapshot) {
  const seen = new Set();
  return (snapshot.artifacts || []).filter(item => {
    const history = item?.metadata?.image_history;
    if (!item || !['slide_image','sample_image'].includes(item.kind) || !history || history.project_id !== snapshot.project_id
      || !Number.isInteger(history.page_number) || history.page_number < 1 || history.page_number > 80 || !Number.isInteger(history.record_number) || history.record_number < 1 || seen.has(item.id)) return false;
    seen.add(item.id); return true;
  }).reverse();
}
function __wuyuHistoryDate(value) {
  const date = typeof value === 'string' ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString(document.documentElement.lang === 'en' ? 'en-GB' : 'zh-CN') : __wuyuHistoryText('missingTime');
}
function __wuyuImageHistory(snapshot) {
  const state = __wuyuHistoryState(snapshot), records = __wuyuHistoryRecords(snapshot);
  const pages = [...new Set(records.map(item => item.metadata.image_history.page_number))].sort((a, b) => a - b);
  if (state.page !== 'all' && !pages.includes(Number(state.page))) { state.page = 'all'; state.offset = 0; }
  const filtered = records.filter(item => state.page === 'all' || item.metadata.image_history.page_number === Number(state.page));
  state.offset = Math.min(state.offset, Math.max(0, Math.floor((filtered.length - 1) / 12) * 12));
  const cards = filtered.slice(state.offset, state.offset + 12).map(item => {
    const history = item.metadata.image_history, preview = Y(item.preview_url || item.url), download = Y(item.download_url);
    const label = __wuyuHistoryText('page', history.page_number) + ' · ' + __wuyuHistoryText('record', history.record_number);
    const filename = `slide-${history.page_number}-record-${history.record_number}.png`;
    return `<article class="wuyu-history-card" data-history-record="${$(item.id)}"><div class="content-title"><h3>${$(label)}</h3>${item.kind === 'sample_image' ? `<span class="status-chip">${$(__wuyuHistoryText('sample'))}</span>` : ''}${history.is_current === true ? `<span class="status-chip">${$(__wuyuHistoryText('current'))}</span>` : ''}</div><small>${$(__wuyuHistoryDate(item.created_at))}</small><div class="wuyu-history-image">${preview ? `<img data-history-image src="${$(preview)}" alt="${$(label)}" loading="lazy" decoding="async">` : ''}<p data-history-error ${preview ? 'hidden' : ''}>${$(__wuyuHistoryText('unavailable'))}</p></div><div class="wuyu-history-actions">${preview ? `<a class="download-link" href="${$(preview)}" target="_blank" rel="noopener noreferrer">${$(__wuyuHistoryText('preview'))} ↗</a>` : ''}${download ? `<a class="download-link" href="${$(download)}" download="${$(filename)}" target="_blank" rel="noopener noreferrer">${$(__wuyuHistoryText('download'))} ↓</a>` : ''}</div></article>`;
  }).join('');
  return `<section class="wuyu-image-history"><h2>${$(__wuyuHistoryText('title'))}</h2><p class="preview-note">${$(__wuyuHistoryText('hint'))}</p><div class="wuyu-history-controls"><label for="wuyu-history-page">${$(__wuyuHistoryText('filter'))}</label><select id="wuyu-history-page" data-history-page><option value="all" ${state.page === 'all' ? 'selected' : ''}>${$(__wuyuHistoryText('all'))}</option>${pages.map(number => `<option value="${number}" ${String(number) === state.page ? 'selected' : ''}>${$(__wuyuHistoryText('page', number))}</option>`).join('')}</select><small role="status">${$(__wuyuHistoryText('count', filtered.length))}</small></div>${records.length ? `<div class="wuyu-history-grid">${cards}</div><div class="wuyu-history-pagination"><button type="button" id="wuyu-history-previous" class="detail-button" data-history-offset="${Math.max(0, state.offset - 12)}" ${state.offset === 0 ? 'disabled' : ''}>${$(__wuyuHistoryText('previous'))}</button><span>${filtered.length ? state.offset + 1 : 0}–${Math.min(state.offset + 12, filtered.length)} / ${filtered.length}</span><button type="button" id="wuyu-history-next" class="detail-button" data-history-offset="${state.offset + 12}" ${state.offset + 12 >= filtered.length ? 'disabled' : ''}>${$(__wuyuHistoryText('next'))}</button></div>` : `<p class="preview-note">${$(__wuyuHistoryText('empty'))}</p>`}</section>`;
}
document.addEventListener('change', event => {
  if (!event.target?.matches?.('[data-history-page]')) return;
  const state = __wuyuHistoryState(m.snapshot); state.page = event.target.value; state.offset = 0; ke();
});
document.addEventListener('click', event => {
  const button = event.target?.closest?.('button[data-history-offset]');
  if (!button || button.disabled) return;
  const offset = Number(button.dataset.historyOffset);
  if (Number.isInteger(offset) && offset >= 0) { __wuyuHistoryState(m.snapshot).offset = offset; ke(); }
});
document.addEventListener('error', event => {
  const image = event.target;
  if (!image?.matches?.('[data-history-image]')) return;
  image.hidden = true; const warning = image.parentElement?.querySelector('[data-history-error]'); if (warning) warning.hidden = false;
}, true);
