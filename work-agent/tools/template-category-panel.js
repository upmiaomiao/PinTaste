function __wuyuTemplateCategoryText(key) {
  const copy = {
    title: ['模板分类', 'Template category'], all: ['全部', 'All'],
    '整套PPT': ['整套PPT', 'Full presentation'], '工艺流程图': ['工艺流程图', 'Process flow'],
    '技术路线': ['技术路线', 'Technical roadmap'], '摘要图': ['摘要图', 'Graphical abstract'],
    '论文方法图': ['论文方法图', 'Research methods'],
    groupPpt: ['PPT 模板', 'Presentation templates'], groupImage: ['图片模板', 'Image templates'],
    groupMine: ['我的上传', 'My uploads'],
    empty: ['当前分类下暂无匹配模板，请尝试其他分类或关键词。', 'No matching templates. Try another category or keyword.'],
    singleReview: ['请查看这张实际参考图，确认后用于单图或一页任务。', 'Review this reference image before confirming it for an image or one-slide task.']
  };
  return copy[key]?.[document.documentElement.lang === 'en' ? 1 : 0] ?? key;
}
// Three separate groups. PPT = category 整套PPT; Image = the four figure categories;
// My uploads = scope "mine" with no category, so uncategorised private uploads stay reachable.
// k.category holds a category value, or '__mine' for the My uploads group.
const __wuyuTemplatePptCategory = '整套PPT';
const __wuyuTemplateImageCategories = ['工艺流程图', '技术路线', '摘要图', '论文方法图'];
const __wuyuTemplateMineKey = '__mine';
function __wuyuTemplateEffectiveCategory() {
  return k.category || __wuyuTemplatePptCategory;
}
function __wuyuTemplateCategories() {
  const current = __wuyuTemplateEffectiveCategory(), mine = current === __wuyuTemplateMineKey, imageGroup = !mine && current !== __wuyuTemplatePptCategory;
  const button = (category, label, pressed) => `<button type="button" data-template-category="${$(category)}" aria-pressed="${pressed}" ${k.loading ? 'disabled' : ''}>${$(label)}</button>`;
  const groups = `<div class="template-modes template-categories" role="group" aria-label="${$(__wuyuTemplateCategoryText('title'))}">${button(__wuyuTemplatePptCategory, __wuyuTemplateCategoryText('groupPpt'), !mine && !imageGroup)}${button(__wuyuTemplateImageCategories[0], __wuyuTemplateCategoryText('groupImage'), imageGroup)}${button(__wuyuTemplateMineKey, __wuyuTemplateCategoryText('groupMine'), mine)}</div>`;
  const images = imageGroup ? `<div class="template-modes template-categories" role="group" aria-label="${$(__wuyuTemplateCategoryText('groupImage'))}">${__wuyuTemplateImageCategories.map(category => button(category, __wuyuTemplateCategoryText(category), current === category)).join('')}</div>` : '';
  return `<p class="preview-note template-category-title">${$(__wuyuTemplateCategoryText('title'))}</p>${groups}${images}`;
}
function __wuyuTemplateListRequest(offset = 0) {
  const current = __wuyuTemplateEffectiveCategory();
  if (current === __wuyuTemplateMineKey) return { operation: 'list', scope: 'mine', search: k.query, offset, limit: 24 };
  return { operation: 'list', scope: k.scope || 'all', search: k.query, template_category: current, offset, limit: 24 };
}
