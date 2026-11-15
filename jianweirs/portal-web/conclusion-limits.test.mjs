import test from 'node:test';
import assert from 'node:assert/strict';
import {conclusionLimits, renderConclusionLimits} from './conclusion-limits.mjs';

const task = {
  limitations: ['模型观测和候选尚待人工复核', '不能凭图像确认危险属性、污染状态或违法事实', '法规检索记录不等于现行性或适用性审查'],
  fusion: {boundary: '可能性等级为筛查优先级，不是违法概率、危险属性或现场事实认定'},
  package: {quality: {warnings: ['上传元数据为用户声明，未独立核验', '获取时间不等于卫星拍摄时间', '预览图不代替科学波段数值分析']}},
};

test('scattered limitations become one plain-language list without losing any of them', () => {
  const items = conclusionLimits(task);
  assert.equal(items.length, 7);
  assert.ok(items.some((text) => /不是违法概率/.test(text)));
  assert.ok(items.some((text) => /不能确认是否危险废物/.test(text)));
  assert.ok(items.every((text) => !/元数据|模型观测|筛查优先级/.test(text)), '技术用语已改写成通俗说法');
});

test('the same limitation from two sources is shown once', () => {
  const items = conclusionLimits({limitations: ['模型观测和候选尚待人工复核'], package: {limitations: ['模型观测和候选尚待人工复核']}});
  assert.equal(items.length, 1);
});

test('an unknown limitation is kept verbatim, and the block is escaped and absent when empty', () => {
  const items = conclusionLimits({limitations: ['<script>x</script> 本图云量较高']});
  assert.equal(items[0], '<script>x</script> 本图云量较高');
  const html = renderConclusionLimits({limitations: ['<script>x</script> 本图云量较高']});
  assert.match(html, /本次结论的限制/); assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.equal(renderConclusionLimits({}), '');
});

test('at most eight limits are shown, so the block stays short', () => {
  const many = Array.from({length: 20}, (_, i) => '限制说明 ' + i);
  assert.equal(conclusionLimits({limitations: many}).length, 8);
});
