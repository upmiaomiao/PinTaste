function __wuyuPrivateLibraryCopy(key) {
  const copy = {
    all: ['全部来源', 'All sources'], mine: ['我的模板', 'My templates'], public: ['公共模板', 'Public templates'],
    scope: ['模板来源', 'Template source'], upload: ['我的上传', 'My upload'], saved: ['我的设计', 'My design'],
    reuse: ['仅自己可见；用于其他项目时会创建独立副本，只参考视觉风格。', 'Private to you. Using it in another project creates an independent copy for visual reference.']
  };
  return copy[key]?.[document.documentElement.lang === 'en' ? 1 : 0] ?? key;
}
function __wuyuPrivateLibraryLabel(card) {
  return card.preview_kind === 'private_uploaded_template' ? __wuyuPrivateLibraryCopy('upload')
    : card.preview_kind === 'private_generated_design' ? __wuyuPrivateLibraryCopy('saved')
    : card.recommendation_reason || card.tags?.purpose?.join(' · ') || __wuyu_panel18_text('设计与排版参考');
}
function __wuyuPrivateLibraryScope() {
  return `<select id="template-library-scope" aria-label="${$(__wuyuPrivateLibraryCopy('scope'))}" style="max-width:100%;min-width:0;border:1px solid #d4dce7;border-radius:8px;padding:6px;background:var(--color-background-primary,white);color:inherit">${['all','mine','public'].map(scope => `<option value="${scope}" ${(k.scope || 'all') === scope ? 'selected' : ''}>${$(__wuyuPrivateLibraryCopy(scope))}</option>`).join('')}</select>`;
}
