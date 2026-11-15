import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {accountingSummary} from './web/accountingSummary.mjs';
import chat from './web/messages/chat.mjs';

const sourceT=readFileSync(new URL('./web/uiLanguage.mjs',import.meta.url),'utf8');
const t=language=>new Function('language','english',
  sourceT.slice(sourceT.indexOf('export function t(')).replace('export function t(','function t(')+'\nreturn t;')(language,chat);
const render=(accounting,language='zh-CN')=>t(language)(accountingSummary(accounting,language));

test('observed stop estimate shows attributed platform tokens and its estimate basis',()=>{
  assert.equal(render({tokens:1760,estimated:true,basis:'stopped_observed_estimate'}),
    '本次平台计入 1,760 tokens（按已发送输入和已收到输出估算）');
});
test('English observed estimate uses existing translator and same amount',()=>{
  assert.equal(render({tokens:1760,estimated:true,basis:'stopped_observed_estimate'},'en'),
    'Platform quota counted: 1,760 tokens (estimated from sent input and received output)');
});
test('unknown in-flight use retains reservation estimate instead of claiming measured use',()=>{
  const value=render({tokens:1040000,estimated:true,basis:'unknown_future_reason'});
  assert.match(value,/1,040,000/);assert.match(value,/按预留额度估算/);
  assert.doesNotMatch(value,/已发送输入/);
});
test('confirmed usage is distinct from estimated use in both languages',()=>{
  assert.equal(render({tokens:897,estimated:false,basis:'stopped_reported_usage'}),
    '本次平台计入 897 tokens（模型报告用量）');
  assert.equal(render({tokens:897,estimated:false,basis:'stopped_reported_usage'},'en'),
    'Platform quota counted: 897 tokens (model-reported usage)');
});
test('per-round request budget is visibly estimated rather than model-reported usage',()=>{
  assert.equal(render({tokens:4200,estimated:true,basis:'stopped_round_budget_estimate'}),
    '本次平台计入 4,200 tokens（按各轮请求预算估算）');
  assert.equal(render({tokens:4200,estimated:true,basis:'stopped_round_budget_estimate'},'en'),
    'Platform quota counted: 4,200 tokens (estimated from per-round request budgets)');
});
test('legacy exact accounting retains its amount without inventing a basis',()=>{
  assert.equal(render({tokens:1183,estimated:false}),'本次平台计入 1,183 tokens');
});
test('zero attribution remains visible',()=>{
  assert.equal(render({tokens:0,estimated:true,basis:'upstream_rejected_before_output'}),
    '本次平台计入 0 tokens（按预留额度估算）');
});
test('legacy estimates without an amount retain the old fixed message',()=>{
  assert.equal(render({estimated:true}),'本次用量按预留额度估算');
  assert.equal(render({estimated:true},'en'),'Usage is estimated from the reserved allowance');
});
test('malformed amounts are never formatted as a quota debit',()=>{
  for(const tokens of ['1040000',-1,Infinity,NaN,Number.MAX_SAFE_INTEGER+1]){
    assert.equal(accountingSummary({tokens,estimated:false}),null);
    assert.deepEqual(accountingSummary({tokens,estimated:true}),{message:'本次用量按预留额度估算'});
  }
});
test('absent accounting stays absent and neither source object nor locale is changed',()=>{
  for(const value of [null,undefined,'unsafe'])assert.equal(accountingSummary(value),null);
  const value=Object.freeze({tokens:1040000,estimated:true,basis:'stopped_observed_estimate'});
  assert.equal(accountingSummary(value,'en').values.v0,'1,040,000');
  assert.deepEqual(value,{tokens:1040000,estimated:true,basis:'stopped_observed_estimate'});
});
test('platform labels never imply provider payment',()=>{
  for(const estimated of [true,false])for(const basis of ['stopped_observed_estimate','stopped_reported_usage','stopped_round_budget_estimate','unknown']){
    const text=render({tokens:20,estimated,basis},'en');
    assert.doesNotMatch(text,/paid|billed|charged by|actual cost/i);
    assert.match(text,/Platform quota counted/);
  }
});
test('footer integrates the descriptor while preserving output-token and copy controls',()=>{
  const main=readFileSync(new URL('./web/main.jsx',import.meta.url),'utf8');
  assert.match(main,/import \{accountingSummary\} from '\.\/accountingSummary\.mjs';/);
  assert.match(main,/const accounting=accountingSummary\(turn.accounting,getUiLanguage\(\)\);/);
  assert.match(main,/\{accounting&&<span>\{t\(accounting\)\}<\/span>\}/);
  assert.ok(main.includes('turn.meta.usage.completion_tokens.toLocaleString(getUiLanguage())'));
  assert.ok(main.includes('onClick={()=>copy(rendered,rs)}'));
});
test('all reservation refusal reasons are translated in the English UI',()=>{
  for(const message of [
    '今日提问次数已达上限，请次日再试。',
    '本次任务的预留额度超过账号日额度，请联系管理员核对任务预算或选择其他模型。',
    '已有任务正在预留可用额度，请等待这些任务结束后再试；这不代表今日额度已经用完。',
    '今日可用额度不足以预留本次任务，请次日再试或选择预留需求更低的模型。',
  ]){
    assert.equal(t('zh-CN')(message),message);
    assert.equal(typeof chat[message],'string');
    assert.notEqual(t('en')(message),message);
    assert.doesNotMatch(t('en')(message),/[\u4e00-\u9fff]/);
  }
});
