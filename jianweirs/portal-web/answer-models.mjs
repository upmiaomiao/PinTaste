// Answer model choice in the composer: 见微（影像判读）or 慎思（专业问答）.
// The server owns the list and availability; the browser only remembers a choice.

export const FALLBACK_MODELS = [
  { id: 'jianwei', label: 'wuyu-jianwei-rs', title: '无隅见微 · 遥感判读', description: '基于影像证据作答，适合判读、核查和报告。', available: true, supports_thinking: false, default: true },
];

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

export function modelList(models) {
  return Array.isArray(models) && models.length ? models : FALLBACK_MODELS;
}

export function selectedModel(models, id) {
  const list = modelList(models);
  return list.find(m => m.id === id && m.available) || list.find(m => m.default) || list[0];
}

// Only the fields the server validates; jianwei is the default and is omitted.
export function modelPayload(draft, models) {
  const model = selectedModel(models, draft.answer_model);
  if (!model || model.id === 'jianwei') return {};
  return { answer_model: model.id, deep_thinking: model.supports_thinking ? draft.deep_thinking !== false : false };
}

export function modelChip(models, draft, icon = () => '') {
  const model = selectedModel(models, draft.answer_model);
  const thinking = model.supports_thinking
    ? `<button type="button" class="composer-chip ${draft.deep_thinking !== false ? 'selected' : ''}" data-action="thinking-toggle" aria-pressed="${draft.deep_thinking !== false}">${icon('spark')}深度思考${draft.deep_thinking !== false ? icon('check') : ''}</button>`
    : '';
  return `${thinking}<button type="button" class="composer-chip model-chip${model.id !== 'jianwei' ? ' model-chip-shensi' : ''}" data-action="model-open" aria-haspopup="dialog" title="当前分析模型：${esc(model.title || model.label)}">${icon('cpu')}${esc(model.label)}${icon('chevron')}</button>`;
}

export function modelPicker(models, draft, icon = () => '') {
  const current = selectedModel(models, draft.answer_model);
  const options = modelList(models).map(model => {
    const selected = model.id === current.id;
    const disabled = !model.available;
    return `<button type="button" class="model-option${selected ? ' selected' : ''}${disabled ? ' disabled' : ''}" data-action="model-select" data-model="${esc(model.id)}" role="radio" aria-checked="${selected}" ${disabled ? 'disabled aria-disabled="true"' : ''}><span class="model-option-main">${icon('cpu')}<span><strong>${esc(model.label)}</strong><small>${esc(model.title || '')}</small></span></span><span class="model-option-desc">${esc(disabled ? '管理员尚未配置，暂不可用' : model.description || '')}</span>${selected ? icon('check') : ''}</button>`;
  }).join('');
  return `<header class="modal-header"><h2>分析模型</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></header><div class="modal-body model-options" role="radiogroup" aria-label="分析模型">${options}<p class="model-options-note">切换到无隅慎思后，追问会结合高德地图查询所在区域与周边设施；开启联网检索时还会查政策和近期事件。影像判读仍由见微完成。</p></div>`;
}
