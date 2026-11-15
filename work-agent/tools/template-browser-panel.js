// Read-only template paging. Confirmation, previews and workflow actions stay in their original handlers.
function __wuyuTemplateBrowserKey(filters = {}) {
  return JSON.stringify([filters.scope || 'all', filters.search || '', filters.template_category || '']);
}
function __wuyuTemplateBrowserPage(total, offset = 0) {
  const count = Number.isSafeInteger(total) && total >= 0 ? total : 0;
  const requested = Number.isSafeInteger(offset) && offset >= 0 ? offset : 0;
  const pages = Math.max(1, Math.ceil(count / 7));
  const current = Math.min((pages - 1) * 7, Math.floor(requested / 7) * 7);
  return { total: count, pages, page: current / 7 + 1, offset: current, needsCorrection: current !== requested };
}
function __wuyuTemplateBrowserCards() {
  // A detail request does not invalidate a loaded list. A new filter must not show another category's cards.
  if (k.listRequest && __wuyuTemplateBrowserKey(k.listRequest) !== __wuyuTemplateBrowserKey(k.snapshot.filters)) return [];
  return (k.snapshot.items || []).slice(0, 7);
}
function __wuyuTemplateBrowserPagination() {
  if (k.tab !== 'all') return '';
  const page = __wuyuTemplateBrowserPage(k.snapshot.total, k.snapshot.filters?.offset);
  if (page.pages <= 1) return '';
  const en = document.documentElement.lang === 'en';
  const status = en ? `Page ${page.page} of ${page.pages} · ${page.total} templates` : `第 ${page.page} / ${page.pages} 页 · 共 ${page.total} 个模板`;
  return `<nav class="template-pagination" data-preview-key="template-pagination" aria-label="${en ? 'Template pages' : '模板分页'}"><button type="button" data-template-previous aria-controls="template-browser-options" ${k.loading || page.page === 1 ? 'disabled' : ''}>${en ? 'Previous page' : '上一页'}</button><span role="status" aria-live="polite" aria-atomic="true" aria-current="page">${$(status)}</span><button type="button" data-template-more aria-controls="template-browser-options" ${k.loading || page.page === page.pages ? 'disabled' : ''}>${en ? 'Next page' : '下一页'}</button></nav>`;
}
function __wuyuTemplateBrowserScroll() {
  if (m.previewTab !== 'templates' || k.handle !== m.handle) return () => {};
  const browser = document.querySelector('.template-browser'), nodes = new Set();
  if (!browser) return () => {};
  for (let node = browser; node instanceof HTMLElement; node = node.parentElement) nodes.add(node);
  for (const node of browser.querySelectorAll('*')) if (node instanceof HTMLElement && (node.scrollTop || node.scrollLeft)) nodes.add(node);
  const positions = [...nodes].map(node => [node, node.scrollTop, node.scrollLeft]);
  return () => { for (const [node, top, left] of positions) if (node.isConnected) { node.scrollTop = top; node.scrollLeft = left; } };
}

// A failed list already records its own scope/search/category/page before dispatch.
function __wuyuTemplateBrowserRetryRequest() {
  return k.listRequest ? { ...k.listRequest } : __wuyuTemplateListRequest(k.snapshot.filters?.offset || 0);
}

// 154 safe upload/confirmation recovery presentation. No raw error text is retained.
const __wuyuTemplate154Copy={"templateAction.notReady":["模板仍在解析，请等待完成，刷新模板信息后再确认。","The template is still being parsed. Wait for it to finish, refresh the template information, then confirm again."],"templateAction.notAvailable":["此模板当前不可用，请刷新并重新选择可用模板。","This template is unavailable. Refresh and select an available template."],"templateAction.scopeMismatch":["模板类型与当前任务不匹配，请选择适合整套PPT或单页、单图的模板。","This template does not match the task. Choose a template for a full presentation or for a single slide or image, as appropriate."],"templateAction.previewInsufficient":["模板可用参考预览不足，请选择其他模板或上传自己的模板。","This template has too few usable reference previews. Choose another template or upload your own."],"templateAction.sourceChanged":["模板文件已变化或不可用，请刷新；必要时重新上传并查看预览后确认。","The template file has changed or is unavailable. Refresh; if needed, upload it again and review its previews before confirming."],"templateAction.revisionChanged":["项目已有更新，请刷新当前项目信息，重新检查模板后再确认。","The project has changed. Refresh its information and review the template before confirming again."],"templateAction.refreshEntry":["确认入口已失效，请刷新工作台并从模板预览重新确认。","The confirmation entry is no longer valid. Refresh the workspace and confirm from the template preview."],"templateAction.uploadLimit":["文件超过当前项目上传上限，请刷新上传信息后选择更小文件；已选文件会保留。","This file exceeds the project's upload limit. Refresh the upload information and choose a smaller file. Your selected file is retained."],"templateAction.limitsUnavailable":["暂时无法读取上传限额，请刷新上传信息后重试；已选文件会保留。","Upload limits could not be loaded. Refresh the upload information and try again. Your selected file is retained."],"templateAction.invalidFile":["请选择非空的PPTX、PDF、PNG、JPG或WebP文件。","Choose a nonempty PPTX, PDF, PNG, JPG or WebP file."],"templateAction.limitsLoading":["正在读取当前项目的上传限额…","Reading this project's upload limits…"],"templateAction.refreshLimits":["刷新上传信息","Refresh upload information"],"templateAction.uploadHint":["支持PPTX、PDF或图片。当前上限 {bytes} 字节（约 {mb} MiB）。","PPTX, PDF or images. Current limit: {bytes} bytes (about {mb} MiB)."],"templateAction.refreshTemplate":["刷新模板信息","Refresh template information"],"templateDecision.error":["暂时无法完成，请刷新当前项目后重试。","Could not complete this action. Refresh the project and retry."],"templateAction.readingInProgress":["资料阅读正在进行，请待本轮结束后再确认或更换模板；当前阅读会继续。","Material reading is in progress. Wait for this run to finish before confirming or changing the template. The current reading will continue."],"templateAction.parsePending":["模板仍在本地解析，请等待解析完成后再开始资料阅读；尚未发起新的阅读请求。","The template is still being parsed locally. Wait for parsing to finish before starting material reading. No new reading request has started."],"templateAction.parseIncomplete":["模板解析尚未完成，当前没有处理它的解析任务。请刷新模板状态核对；若仍未恢复，请检查并恢复当前模板解析。原文件保留，本次未启动阅读。","Template parsing is incomplete and no parser task is processing it. Refresh the template status; if it remains incomplete, inspect and recover the current parse. The original is retained and no reading request has started."]};
const __wuyuTemplate154Codes={"TEMPLATE_NOT_READY":409,"TEMPLATE_NOT_AVAILABLE":409,"TEMPLATE_SCOPE_MISMATCH":409,"TEMPLATE_PREVIEW_INSUFFICIENT":409,"TEMPLATE_SOURCE_CHANGED":409,"REVISION_CONFLICT":409,"TEMPLATE_CONFIRMATION_UI_REQUIRED":403,"MATERIAL_UPLOAD_LIMIT":413,"TEMPLATE_LIMITS_UNAVAILABLE":503,"TEMPLATE_UPLOAD_INVALID_FILE":400,"TEMPLATE_READING_IN_PROGRESS":409,"TEMPLATE_PARSE_PENDING":409,"TEMPLATE_PARSE_INCOMPLETE":409};
const __wuyuTemplate154Keys={"TEMPLATE_NOT_READY":"templateAction.notReady","TEMPLATE_NOT_AVAILABLE":"templateAction.notAvailable","TEMPLATE_SCOPE_MISMATCH":"templateAction.scopeMismatch","TEMPLATE_PREVIEW_INSUFFICIENT":"templateAction.previewInsufficient","TEMPLATE_SOURCE_CHANGED":"templateAction.sourceChanged","REVISION_CONFLICT":"templateAction.revisionChanged","TEMPLATE_CONFIRMATION_UI_REQUIRED":"templateAction.refreshEntry","MATERIAL_UPLOAD_LIMIT":"templateAction.uploadLimit","TEMPLATE_LIMITS_UNAVAILABLE":"templateAction.limitsUnavailable","TEMPLATE_UPLOAD_INVALID_FILE":"templateAction.invalidFile","TEMPLATE_ACTION_UNAVAILABLE":"templateDecision.error","TEMPLATE_READING_IN_PROGRESS":"templateAction.readingInProgress","TEMPLATE_PARSE_PENDING":"templateAction.parsePending","TEMPLATE_PARSE_INCOMPLETE":"templateAction.parseIncomplete"};
function __wuyuTemplate154Text(key) {
  return __wuyuTemplate154Copy[key]?.[document.documentElement.lang==='en'?1:0] || '';
}
function __wuyuTemplate154Failure(value,operation) {
  if(!value||typeof value!=='object'||Array.isArray(value)||!['confirm','upload'].includes(operation)||value.operation!==operation||!Number.isSafeInteger(value.status)||value.status<400||value.status>599)return null;
  const code=value.code;
  const known=typeof code==='string'&&Object.prototype.hasOwnProperty.call(__wuyuTemplate154Codes,code)&&__wuyuTemplate154Codes[code]===value.status;
  const upload=['MATERIAL_UPLOAD_LIMIT','TEMPLATE_LIMITS_UNAVAILABLE','TEMPLATE_UPLOAD_INVALID_FILE'];
  if(code!=='TEMPLATE_ACTION_UNAVAILABLE'&&(!known||(!['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(code)&&(operation==='upload'?!upload.includes(code):upload.includes(code)))))return null;
  return {code,status:value.status,operation};
}
function __wuyuTemplate154Receive(data,operation) {
  k.actionFailure=__wuyuTemplate154Failure(data?.actionFailure,operation);
  if(data&&['idle','loading','ready','error'].includes(data.templateLimitsStatus)&&Number.isSafeInteger(data.maxTemplateBytes)&&data.maxTemplateBytes>=0){
    k.snapshot={...k.snapshot,templateLimitsStatus:data.templateLimitsStatus,maxTemplateBytes:data.maxTemplateBytes};
  }
}
function __wuyuTemplate154Error() {
  const key=k.actionFailure?__wuyuTemplate154Keys[k.actionFailure.code]:'templateDecision.error';
  return `<p class="preview-error" role="alert">${$(__wuyuTemplate154Text(key))}<button type="button" data-template-retry>${$(__wuyuTemplate154Text('templateAction.refreshTemplate'))}</button></p>`;
}
function __wuyuTemplate154UploadAllowed() {
  return k.snapshot.templateLimitsStatus==='ready'&&Number.isSafeInteger(k.snapshot.maxTemplateBytes)&&k.snapshot.maxTemplateBytes>0&&k.file&&k.file.size>0&&k.file.size<=k.snapshot.maxTemplateBytes&&/\.(pptx|pdf|png|jpe?g|webp)$/i.test(k.file.name);
}
function __wuyuTemplate154UploadLimits() {
  const ready=k.snapshot.templateLimitsStatus==='ready',limit=k.snapshot.maxTemplateBytes;
  const text=ready?__wuyuTemplate154Text('templateAction.uploadHint').replace('{bytes}',String(limit)).replace('{mb}',String(Number((limit/1024/1024).toFixed(2)))):__wuyuTemplate154Text(k.snapshot.templateLimitsStatus==='error'?'templateAction.limitsUnavailable':'templateAction.limitsLoading');
  return `<div data-preview-key="template-upload-limits"><p class="preview-note" role="status">${$(text)}</p>${ready&&k.file&&k.file.size>limit?`<p class="preview-error" role="alert">${$(__wuyuTemplate154Text('templateAction.uploadLimit'))}</p>`:''}<button type="button" data-template-limits-retry ${k.loading||k.snapshot.templateLimitsStatus==='loading'?'disabled':''}>${$(__wuyuTemplate154Text('templateAction.refreshLimits'))}</button></div>`;
}
