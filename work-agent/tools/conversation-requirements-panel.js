function __wuyuRequirementsCopy(key) {
  const copy = {
    title: ['当前需求', 'Current requirements'], hint: ['直接在左侧对话说明或修改要求，已明确的要求会保留在当前对话。', 'Describe or change requirements in the conversation. Explicit choices stay with this conversation.'],
    task_mode: ['创作方式', 'Creation approach'], creation_kind: ['内容类型', 'Content type'], page_count: ['页数', 'Slides'], language: ['内容语言', 'Content language'], topic: ['主题', 'Topic'], audience: ['受众', 'Audience'], goal: ['目标', 'Goal'], style: ['风格', 'Style'], content_policy: ['内容处理', 'Content handling'], search_mode: ['联网方式', 'Research'], requirements: ['补充要求', 'Additional requirements'],
    task_auto: ['根据对话确定', 'Decide in conversation'], task_create: ['从想法创作', 'Create from an idea'], task_relayout: ['基于资料创作', 'Create from materials'],
    auto: ['待确定', 'To be decided'], auto_pages: ['待规划', 'To be planned'], page_unplanned: ['页数待规划', 'Slide count to be planned'], pages: ['页', 'slides'], zh: ['中文', 'Chinese'], en: ['英文', 'English'],
    deck: ['整套PPT', 'Full presentation'], focused: ['少量页面', 'Content slides'], asset: ['专业单图', 'Professional figure'], preserve: ['保留原稿内容', 'Preserve source content'], reorganize: ['重新梳理内容', 'Reorganize content'],
    off: ['未开启', 'Off'], web: ['联网搜索', 'Web search'], deep: ['深度探索', 'Deep research'],
    user: ['你已指定', 'Your choice'], inherited: ['沿用已有信息', 'Inherited'], suggested: ['系统建议', 'Suggested'], default: ['未指定', 'Unspecified'],
    unbound: ['已记录', 'Recorded'], applied: ['已用于当前工作', 'Applied to current work'], pending: ['新要求已记录，待衔接当前工作', 'New requirements recorded; pending application'], conflict: ['要求有冲突，请在对话中继续说明', 'Requirements conflict. Clarify them in the conversation.'],
    unavailable: ['当前对话需求尚未同步，可在原对话中继续说明。', 'Current conversation requirements are not synchronized yet. Continue in the same conversation.'],
    template: ['当前模板', 'Current template'], template_pending: ['待推荐或确认', 'Pending recommendation or confirmation'], confirmed: ['已确认', 'Confirmed'],
    materials: ['材料状态', 'Materials'], ready: ['份已就绪', 'ready'], processing: ['份处理中', 'processing'], failed: ['份未完成', 'incomplete'],
    progress: ['当前进度', 'Current progress'], stepFailed: ['当前步骤未完成，已保留成果，可在原对话中继续处理', 'This step is incomplete. Results are retained; continue in the same conversation.']
  };
  return copy[key]?.[document.documentElement.lang === 'en' ? 1 : 0] ?? key;
}
function __wuyuCurrentRequirements() {
  const value = window.__wuyu79Requirements;
  return value && typeof value === 'object' && !Array.isArray(value) && Number.isSafeInteger(value.revision) && value.values && typeof value.values === 'object' && value.sources && typeof value.sources === 'object' ? value : null;
}
function __wuyuRequirementValue(field, values) {
  const value = values[field];
  if (field === 'task_mode') return __wuyuRequirementsCopy(value === 'create' ? 'task_create' : value === 'relayout' ? 'task_relayout' : 'task_auto');
  if (field === 'page_count') return Number.isInteger(value) && value > 0 ? value + ' ' + __wuyuRequirementsCopy('pages') : __wuyuRequirementsCopy('auto_pages');
  if (field === 'language') return __wuyuRequirementsCopy(['zh', 'en'].includes(value) ? value : 'auto');
  if (field === 'creation_kind') return __wuyuRequirementsCopy(value === 'pages' ? 'focused' : ['deck', 'asset'].includes(value) ? value : 'auto');
  if (field === 'content_policy') return __wuyuRequirementsCopy(['preserve', 'reorganize'].includes(value) ? value : 'auto');
  if (field === 'search_mode') return __wuyuRequirementsCopy(['off', 'web', 'deep'].includes(value) ? value : 'off');
  return typeof value === 'string' && value.trim() ? value : __wuyuRequirementsCopy('auto');
}
function __wuyuRequirementsView() {
  const record = __wuyuCurrentRequirements(), values = record?.values || {}, sources = record?.sources || {}, selection = m.snapshot.template_selection || {};
  const fields = ['task_mode', 'topic', 'audience', 'goal', 'page_count', 'language', 'style', 'content_policy', 'creation_kind', 'search_mode', 'requirements'];
  const rows = fields.map(field => `<div><dt>${$(__wuyuRequirementsCopy(field))}</dt><dd><span>${$(__wuyuRequirementValue(field, values))}</span><small class="requirement-source">${$(__wuyuRequirementsCopy(['user', 'inherited', 'suggested', 'default'].includes(sources[field]) ? sources[field] : 'default'))}</small></dd></div>`).join('');
  const materials = (m.snapshot.materials || []).filter(item => (!item.role || item.role === 'material') && item.selected_for_content !== false);
  const counts = [materials.filter(item => item.status === 'completed').length, materials.filter(item => !['completed', 'failed', 'cancelled'].includes(item.status)).length, materials.filter(item => ['failed', 'cancelled'].includes(item.status)).length];
  const state = record ? __wuyuRequirementsCopy(record.projection_status || 'unbound') : __wuyuRequirementsCopy('unavailable');
  return `<section class="current-requirements" data-current-requirements><h2>${$(__wuyuRequirementsCopy('title'))}</h2><p class="preview-note">${$(__wuyuRequirementsCopy('hint'))}</p><p class="preview-note" role="status">${$(state)}</p><dl class="brief-details">${rows}<div><dt>${$(__wuyuRequirementsCopy('template'))}</dt><dd data-effective-template>${$(selection.confirmed === true ? selection.title : __wuyuRequirementsCopy('template_pending'))}${selection.confirmed === true ? `<small class="requirement-source">${$(__wuyuRequirementsCopy('confirmed'))}</small>` : ''}</dd></div><div><dt>${$(__wuyuRequirementsCopy('materials'))}</dt><dd>${counts.map((count, index) => count + ' ' + __wuyuRequirementsCopy(['ready', 'processing', 'failed'][index])).join(' · ')}</dd></div><div><dt>${$(__wuyuRequirementsCopy('progress'))}</dt><dd>${$(F$())}</dd></div></dl>${Ge(__wuyu_panel18_text('需求'), __wuyu_panel18_text('把需求调整意见放入对话'))}</section>`;
}
function __wuyuRequirementsProgress() {
  const record = __wuyuCurrentRequirements(), pages = pt();
  const applied = record?.projection_status === 'applied' && record.binding?.applied_revision === record.revision;
  const explicit = applied ? record.values.page_count : null;
  const total = Number.isInteger(explicit) && explicit > 0 ? explicit : pages.some(page => page.image_version) ? pages.length : 0;
  return total ? pages.filter(nr).length + '/' + total + ' ' + __wuyuRequirementsCopy('pages') : __wuyuRequirementsCopy('page_unplanned');
}
window.addEventListener('message', event => {
  if (event.source !== window.parent || !event.data || typeof event.data !== 'object') return;
  if (event.data.method !== 'ui/notifications/host-context-changed') return;
  const context = event.data.params;
  if (!context || typeof context !== 'object' || !Object.prototype.hasOwnProperty.call(context, 'current_requirements')) return;
  const next = context.current_requirements;
  if (JSON.stringify(next) === JSON.stringify(window.__wuyu79Requirements)) return;
  window.__wuyu79Requirements = next;
  m.onchange();
});
