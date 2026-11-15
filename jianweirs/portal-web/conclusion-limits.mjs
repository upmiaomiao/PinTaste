import {escapeHTML as esc} from './lib.mjs';

/** 把证据包里分散的限制、提醒、边界说明，合并成一块"本次结论的限制"。
 *  只做去重和通俗改写，不新增也不删除任何限制内容。 */
const PLAIN = [
  [/模型观测和候选尚待人工复核/, '识别出的疑点还需要人工复核。'],
  [/不能凭图像确认危险属性、污染状态或违法事实/, '仅凭影像，不能确认是否危险废物、是否造成污染或是否违法。'],
  [/法规检索记录不等于现行性或适用性审查/, '检索到的政策只是参考，是否现行有效、是否适用，要对照原文核实。'],
  [/上传元数据为用户声明，未独立核验/, '上传影像的日期、位置等信息是你提供的，系统没有独立核实。'],
  [/获取时间不等于卫星拍摄时间/, '影像的获取时间不是卫星的拍摄时间。'],
  [/预览图不代替科学波段数值分析/, '预览图不能代替多光谱、雷达的数值分析。'],
  [/可能性等级为筛查优先级.*(?:认定)?/, '“可能性”等级只是核查的优先顺序，不是违法概率，也不是现场事实认定。'],
];
const norm = (text) => String(text || '').replace(/\s+/g, '').replace(/[。；;，,]+$/, '');
const asList = (value) => (Array.isArray(value) ? value : []);

export function conclusionLimits(task = {}) {
  const pkg = task.package || task.result || task.report || {};
  const raw = [
    ...asList(task.limitations), ...asList(pkg.limitations),
    ...asList(pkg.quality?.warnings),
    (task.fusion || pkg.fusion || {}).boundary,
  ].filter((value) => typeof value === 'string' && value.trim());
  const seen = new Set(), items = [];
  for (const value of raw) {
    const hit = PLAIN.find(([pattern]) => pattern.test(value));
    const text = hit ? hit[1] : String(value).trim();
    const key = norm(text);
    if (seen.has(key)) continue;
    seen.add(key); items.push(text);
  }
  return items.slice(0, 8);
}

export function renderConclusionLimits(task = {}, icon = () => '') {
  const items = conclusionLimits(task);
  if (!items.length) return '';
  return `<section class="turn-layer-block conclusion-limits"><h3>本次结论的限制</h3><ul>${items.map((text) => `<li>${esc(text)}</li>`).join('')}</ul></section>`;
}
