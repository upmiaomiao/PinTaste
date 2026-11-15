function __wuyuTemplateCategoryText(key) {
  const copy = {
    title: ['模板分类', 'Template category'], all: ['全部', 'All'],
    '整套PPT': ['整套PPT', 'Full presentation'], '工艺流程图': ['工艺流程图', 'Process flow'],
    '技术路线': ['技术路线', 'Technical roadmap'], '摘要图': ['摘要图', 'Graphical abstract'],
    '论文方法图': ['论文方法图', 'Research methods'],
    empty: ['当前分类下暂无匹配模板，请尝试其他分类或关键词。', 'No matching templates. Try another category or keyword.'],
    singleReview: ['请查看这张实际参考图，确认后用于单图或一页任务。', 'Review this reference image before confirming it for an image or one-slide task.']
  };
  return copy[key]?.[document.documentElement.lang === 'en' ? 1 : 0] ?? key;
}
function __wuyuTemplateCategories() {
  return `<p class="preview-note template-category-title">${$(__wuyuTemplateCategoryText('title'))}</p><div class="template-modes template-categories" role="group" aria-label="${$(__wuyuTemplateCategoryText('title'))}">${['', '整套PPT', '工艺流程图', '技术路线', '摘要图', '论文方法图'].map(category => `<button type="button" data-template-category="${$(category)}" aria-pressed="${(k.category || '') === category}" ${k.loading ? 'disabled' : ''}>${$(__wuyuTemplateCategoryText(category || 'all'))}</button>`).join('')}</div>`;
}
function __wuyuTemplateListRequest(offset = 0) {
  return { operation: 'list', scope: k.scope || 'all', search: k.query, ...(k.category ? { template_category: k.category } : {}), offset, limit: 24 };
}
