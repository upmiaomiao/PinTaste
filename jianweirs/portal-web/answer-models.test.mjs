import test from 'node:test';
import assert from 'node:assert/strict';
import { modelChip, modelPayload, modelPicker, selectedModel, FALLBACK_MODELS } from './answer-models.mjs';
import { modelBadge, thinkingBlock } from './task-results.mjs';

const MODELS = [
  { id: 'jianwei', label: 'wuyu-jianwei-rs', title: '无隅见微 · 遥感判读', available: true, supports_thinking: false, default: true },
  { id: 'shensi', label: 'wuyu-shensi', title: '无隅慎思 · 专业问答', available: true, supports_thinking: true, default: false },
];

test('default draft keeps 见微 and sends no model fields', () => {
  assert.deepEqual(modelPayload({}, MODELS), {});
  assert.deepEqual(modelPayload({ answer_model: 'jianwei' }, MODELS), {});
});

test('慎思 sends the model and thinking switch', () => {
  assert.deepEqual(modelPayload({ answer_model: 'shensi' }, MODELS), { answer_model: 'shensi', deep_thinking: true });
  assert.deepEqual(modelPayload({ answer_model: 'shensi', deep_thinking: false }, MODELS), { answer_model: 'shensi', deep_thinking: false });
});

test('an unavailable or unknown choice falls back to 见微', () => {
  const offline = MODELS.map(m => m.id === 'shensi' ? { ...m, available: false } : m);
  assert.equal(selectedModel(offline, 'shensi').id, 'jianwei');
  assert.deepEqual(modelPayload({ answer_model: 'shensi' }, offline), {});
  assert.equal(selectedModel(null, 'shensi').id, 'jianwei');
  assert.equal(selectedModel(FALLBACK_MODELS, 'nonsense').label, 'wuyu-jianwei-rs');
});

test('composer chip shows the thinking toggle only for 慎思', () => {
  assert.doesNotMatch(modelChip(MODELS, { answer_model: 'jianwei' }), /thinking-toggle/);
  const chip = modelChip(MODELS, { answer_model: 'shensi' });
  assert.match(chip, /data-action="thinking-toggle" aria-pressed="true"/);
  assert.match(chip, /wuyu-shensi/);
  assert.match(modelChip(MODELS, { answer_model: 'shensi', deep_thinking: false }), /aria-pressed="false"/);
});

test('picker marks the current model and disables unconfigured ones', () => {
  const offline = MODELS.map(m => m.id === 'shensi' ? { ...m, available: false, title: '<b>x</b>' } : m);
  const html = modelPicker(offline, { answer_model: 'jianwei' });
  assert.match(html, /data-model="jianwei" role="radio" aria-checked="true"/);
  assert.match(html, /data-model="shensi" role="radio" aria-checked="false" disabled/);
  assert.match(html, /管理员尚未配置/);
  assert.doesNotMatch(html, /<b>x<\/b>/);
});

test('thinking block is folded, escaped and absent when empty', () => {
  assert.equal(thinkingBlock('', 't1'), '');
  assert.equal(thinkingBlock(undefined, 't1'), '');
  const html = thinkingBlock('第一步<script>\n\n第二步', 't1');
  assert.match(html, /^<details class="answer-thinking"/);
  assert.doesNotMatch(html, /<details[^>]* open/);
  assert.match(html, /不是已核实的证据/);
  assert.match(html, /&lt;script&gt;/);
  assert.equal((html.match(/<p>/g) || []).length, 2);
});

test('model badge only names 慎思', () => {
  assert.equal(modelBadge('jianwei'), '');
  assert.equal(modelBadge(undefined), '');
  assert.match(modelBadge('shensi'), /无隅慎思/);
});
