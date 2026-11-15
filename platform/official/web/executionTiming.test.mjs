import assert from 'node:assert/strict';
import test from 'node:test';
import { elapsedTiming, reasoningSections } from './executionTiming.mjs';
import english from './messages/chat.mjs';
test('live queue and execution advance their own phase with total time', () => {
  const base = { sampled_at: 123, phase: 'queued', queue_seconds: 5, execution_seconds: null, total_seconds: 5 };
  assert.deepEqual(elapsedTiming({ status: 'queued', executionTiming: base }, 2), { queue_seconds: 7, total_seconds: 7 });
  assert.deepEqual(elapsedTiming({ status: 'running', executionTiming: { ...base, phase: 'running', queue_seconds: 4, execution_seconds: 6, total_seconds: 10 } }, 3), { queue_seconds: 4, execution_seconds: 9, total_seconds: 13 });
});
test('terminal, mismatched and invalid timing cannot create a false live duration', () => {
  const value = { sampled_at: 123, phase: 'complete', queue_seconds: 4, execution_seconds: 6, total_seconds: 10 };
  assert.deepEqual(elapsedTiming({ status: 'complete', executionTiming: value }, 999), { queue_seconds: 4, execution_seconds: 6, total_seconds: 10 });
  assert.deepEqual(elapsedTiming({ status: 'running', executionTiming: value }, 999), { queue_seconds: 4, execution_seconds: 6, total_seconds: 10 });
  assert.equal(elapsedTiming({ status: 'complete' }), null);
  assert.equal(elapsedTiming({ executionTiming: { sampled_at: 123 } }), null);
});
test('round groups contain exactly the original Unicode reasoning without hidden or added text', () => {
  const value = { reasoning: 'first😀second', reasoningRounds: [{ round: 1, text: 'first😀' }, { round: 2, text: 'second' }] };
  assert.deepEqual(reasoningSections(value), value.reasoningRounds);
  for (const bad of [null, [{ round: 1, text: 'missing' }], [{ round: 1, text: 'first😀secondextra' }], [{ round: false, text: value.reasoning }]])
    assert.deepEqual(reasoningSections({ ...value, reasoningRounds: bad }), [{ round: null, text: value.reasoning }]);
});
test('old reasoning and empty turns remain safe without inventing a round number', () => {
  assert.deepEqual(reasoningSections({ reasoning: 'legacy text' }), [{ round: null, text: 'legacy text' }]);
  assert.deepEqual(reasoningSections({ reasoning: '' }), []);
});
test('English labels distinguish job timing from agent processing and final answer TTFT', () => {
  for (const key of ['任务耗时', '排队 {v0} 秒', '执行 {v0} 秒', '总等待 {v0} 秒', '模型与工具处理 {v0} 秒', '首段输出 {v0} 秒', '第 {v0} 轮思考', '未记录轮次的思考']) assert.equal(typeof english[key], 'string');
  assert(english['可能是思考内容，不代表最终答案首字；从模型与工具流程开始计时。'].includes('not the first word of the final answer'));
  assert.equal(english['当前模型尚未配置好联网工具，请联系管理员处理。你可以关闭联网工具继续普通问答。'], 'Online tools are not configured for this model. Please contact an administrator. You can turn off online tools and continue chatting.');
});
