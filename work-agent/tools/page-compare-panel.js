function __wuyuCompareText(key, number) {
  const words = {
    open: ['修改前后对比', 'Compare before/after'], openRelayout: ['原稿对比', 'Compare with original'],
    region: ['第 {n} 页对比', 'Slide {n} comparison'], close: ['关闭对比', 'Close comparison'],
    before: ['修改前', 'Before'], after: ['修改后', 'After'], original: ['原稿', 'Original'], restyled: ['重排后', 'Restyled'],
    choose: ['对比对象', 'Compare with'], originalOption: ['原稿 · 第 {n} 页', 'Original · slide {n}'],
    modes: ['对比方式', 'Comparison mode'], side: ['并排', 'Side by side'], slider: ['滑动对比', 'Slider'],
    range: ['拖动或用方向键移动分隔线：左侧为对比对象，右侧为当前页图', 'Drag or use the arrow keys to move the divider: the compared image is on the left, the current image on the right'],
    request: ['本页最近的修改要求：', 'Latest change request for this slide: '],
    hint: ['仅供查看：对比不会改变当前页图或审核状态，也不支持把页面恢复为旧图。', 'View only: comparing does not change the current image or its approval, and a slide cannot be switched back to an older image.'],
    unavailable: ['图片暂时无法加载，可刷新详情后重试。', 'The image could not be loaded. Refresh the details and try again.'],
  };
  return (words[key]?.[document.documentElement.lang === 'en' ? 1 : 0] || key).replace('{n}', String(number ?? ''));
}
// Pairs each page's current image with what it can be compared against: the original
// re-layout page (when this is a re-layout project) and the page's own earlier images.
function __wuyuComparePairs(snapshot) {
  const pairs = new Map(), byPage = new Map(), sources = new Map(), position = new Map(), states = new Map();
  for (const page of Array.isArray(snapshot?.workflow?.pages) ? snapshot.workflow.pages : []) if (page && typeof page === 'object') states.set(page.number, page);
  // Artifacts arrive in the order they were saved. The newest original page of the current source deck
  // marks the latest prepare that rendered anything: records saved before it restyle an earlier upload.
  let prepared = -1;
  (Array.isArray(snapshot?.artifacts) ? snapshot.artifacts : []).forEach((item, index) => {
    if (item && typeof item === 'object') position.set(item, index);
    const label = item?.kind === 'relayout_source_page' ? item.metadata?.relayout_source : null;
    if (!label || label.project_id !== snapshot.project_id || !Number.isInteger(label.page_number) || label.in_current_source !== true) return;
    prepared = index;
    if (label.is_current !== true) sources.set(label.page_number, item);
  });
  for (const item of __wuyuHistoryRecords(snapshot || {})) {
    const number = item.metadata.image_history.page_number;
    byPage.set(number, [...byPage.get(number) || [], item]);
  }
  for (const [number, records] of byPage) {
    records.sort((a, b) => a.metadata.image_history.record_number - b.metadata.image_history.record_number);
    const at = records.findIndex(item => item.metadata.image_history.is_current === true);
    if (at < 0) continue;
    // image_version counts the images this page has had since its draft was last reset or the page was
    // added: only that many records, ending with the current one, are versions of this page. Older
    // records belong to an earlier draft and stay in Image history only.
    const current = records[at], versions = states.get(number)?.image_version;
    const options = (Number.isInteger(versions) && versions > 1 ? records.slice(Math.max(0, at - versions + 1), at) : [])
      .filter(item => position.get(item) > prepared).reverse()
      .map(item => ({ key: 'record:' + item.id, type: 'record', artifact: item }));
    const source = sources.get(number);
    if (source && source.id !== current.id && !(typeof source.sha256 === 'string' && source.sha256 === current.sha256)) options.unshift({ key: 'source:' + source.id, type: 'source', artifact: source });
    if (options.length) pairs.set(number, { page: number, current, options });
  }
  return pairs;
}
function __wuyuCompareState() {
  const project = m.snapshot.project_id || '';
  if (m.previewCompare?.project !== project) m.previewCompare = { project, page: 0, before: '', mode: 'side', position: 50 };
  return m.previewCompare;
}
function __wuyuCompareButton(page) {
  const pair = __wuyuComparePairs(m.snapshot).get(page.number);
  if (!pair) return '';
  const open = __wuyuCompareState().page === page.number;
  return `<button type="button" id="wuyu-compare-open" class="detail-button" data-compare-open="${page.number}" aria-expanded="${open}" aria-controls="wuyu-compare">${$(__wuyuCompareText(pair.options[0].type === 'source' ? 'openRelayout' : 'open'))}</button>`;
}
function __wuyuCompareImage(artifact, label, extra = '') {
  const url = Y(artifact.preview_url || artifact.url);
  return `${url ? `<img data-compare-image data-image-key="compare-${$(artifact.id)}${extra}" src="${$(url)}" alt="${$(label)}" loading="lazy" decoding="async">` : ''}<p data-compare-error ${url ? 'hidden' : ''}>${$(__wuyuCompareText('unavailable'))}</p>`;
}
function __wuyuCompareOptionLabel(option, page) {
  if (option.type === 'source') return __wuyuCompareText('originalOption', page);
  const history = option.artifact.metadata.image_history;
  return __wuyuHistoryText('record', history.record_number) + (option.artifact.kind === 'sample_image' ? ' · ' + __wuyuHistoryText('sample') : '') + ' · ' + __wuyuHistoryDate(option.artifact.created_at);
}
function __wuyuCompareView(page) {
  const state = __wuyuCompareState();
  if (state.page !== page.number) return '';
  const pair = __wuyuComparePairs(m.snapshot).get(page.number);
  if (!pair) { state.page = 0; return ''; }
  const option = pair.options.find(item => item.key === state.before) || pair.options[0];
  state.before = option.key;
  const source = option.type === 'source', beforeWord = __wuyuCompareText(source ? 'original' : 'before'), afterWord = __wuyuCompareText(source ? 'restyled' : 'after');
  const currentLabel = __wuyuHistoryText('record', pair.current.metadata.image_history.record_number) + ' · ' + __wuyuHistoryDate(pair.current.created_at);
  const beforeLabel = __wuyuCompareOptionLabel(option, page.number), position = Math.min(100, Math.max(0, Number(state.position) || 0));
  // The stored change text describes the request applied to the current image, so it is
  // only shown against the record that immediately precedes it.
  const change = !source && option === pair.options.find(item => item.type === 'record') && page.generation_status === 'ready' && pair.current.kind === 'slide_image'
    && typeof page.requested_change === 'string' ? page.requested_change.replace(/\s+/g, ' ').trim().slice(0, 240) : '';
  const body = state.mode === 'slider'
    ? `<div class="wuyu-compare-stage" data-compare-stage style="--wuyu-compare-pos:${position}%">${__wuyuCompareImage(pair.current, afterWord + ' · ' + currentLabel, '-stage')}<div class="wuyu-compare-top">${__wuyuCompareImage(option.artifact, beforeWord + ' · ' + beforeLabel, '-stage')}</div><span class="wuyu-compare-divider" aria-hidden="true"></span><span class="status-chip wuyu-compare-tag">${$(beforeWord)}</span><span class="status-chip wuyu-compare-tag after">${$(afterWord)}</span><input type="range" id="wuyu-compare-range" data-compare-range min="0" max="100" step="1" value="${position}" aria-label="${$(__wuyuCompareText('range'))}"></div>`
    : `<div class="wuyu-compare-side"><figure><figcaption><span class="status-chip">${$(beforeWord)}</span> ${$(beforeLabel)}</figcaption><div class="wuyu-compare-image">${__wuyuCompareImage(option.artifact, beforeWord + ' · ' + beforeLabel)}</div></figure><figure><figcaption><span class="status-chip">${$(afterWord)}</span> ${$(currentLabel)}</figcaption><div class="wuyu-compare-image">${__wuyuCompareImage(pair.current, afterWord + ' · ' + currentLabel)}</div></figure></div>`;
  return `<section id="wuyu-compare" class="wuyu-compare" role="region" aria-label="${$(__wuyuCompareText('region', page.number))}"><div class="wuyu-compare-bar"><label for="wuyu-compare-before">${$(__wuyuCompareText('choose'))}</label><select id="wuyu-compare-before" data-compare-before>${pair.options.map(item => `<option data-preview-key="compare-option-${$(item.key)}" value="${$(item.key)}" ${item === option ? 'selected' : ''}>${$(__wuyuCompareOptionLabel(item, page.number))}</option>`).join('')}</select><span class="wuyu-compare-modes" role="group" aria-label="${$(__wuyuCompareText('modes'))}">${['side', 'slider'].map(mode => `<button type="button" id="wuyu-compare-mode-${mode}" class="detail-button" data-compare-mode="${mode}" aria-pressed="${state.mode === mode}">${$(__wuyuCompareText(mode))}</button>`).join('')}</span><button type="button" id="wuyu-compare-close" class="detail-button" data-compare-close>${$(__wuyuCompareText('close'))}</button></div>${body}${change ? `<p class="preview-note wuyu-compare-request" title="${$(change)}">${$(__wuyuCompareText('request') + change)}</p>` : ''}<p class="preview-note">${$(__wuyuCompareText('hint'))}</p></section>`;
}
function __wuyuCompareClose() {
  __wuyuCompareState().page = 0; ke();
  document.getElementById('wuyu-compare-open')?.focus();
}
document.addEventListener('click', event => {
  const button = event.target?.closest?.('button[data-compare-open],button[data-compare-close],button[data-compare-mode]');
  if (!button || button.disabled) return;
  const state = __wuyuCompareState(), data = button.dataset;
  if (data.compareClose !== undefined || data.compareOpen !== undefined && state.page === Number(data.compareOpen)) { __wuyuCompareClose(); return; }
  if (data.compareOpen !== undefined) {
    const number = Number(data.compareOpen);
    if (!Number.isInteger(number) || !__wuyuComparePairs(m.snapshot).has(number)) return;
    state.page = number; state.before = ''; state.position = 50; ke();
    document.getElementById('wuyu-compare-before')?.focus();
    return;
  }
  if (['side', 'slider'].includes(data.compareMode)) { state.mode = data.compareMode; ke(); }
});
document.addEventListener('change', event => {
  if (!event.target?.matches?.('[data-compare-before]')) return;
  __wuyuCompareState().before = String(event.target.value); ke();
});
document.addEventListener('input', event => {
  const range = event.target;
  if (!range?.matches?.('[data-compare-range]')) return;
  const position = Math.min(100, Math.max(0, Number(range.value) || 0));
  __wuyuCompareState().position = position;
  range.closest('[data-compare-stage]')?.style.setProperty('--wuyu-compare-pos', position + '%');
});
document.addEventListener('keydown', event => {
  // The panel's own Escape handler runs first, closes the topmost dialog and marks the event as handled.
  if (event.key !== 'Escape' || event.defaultPrevented || !__wuyuCompareState().page || !document.getElementById('wuyu-compare') || document.querySelector('dialog[open]')) return;
  event.preventDefault(); __wuyuCompareClose();
});
document.addEventListener('error', event => {
  const image = event.target;
  if (!image?.matches?.('[data-compare-image]')) return;
  image.hidden = true; const warning = image.parentElement?.querySelector('[data-compare-error]'); if (warning) warning.hidden = false;
}, true);
