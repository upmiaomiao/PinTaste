import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewText,readingText,answerExcerpt,renderReadableAnswer,renderCoreReport} from './answer-layout.mjs';
import {mergeTurns,upsertConversation} from './conversation-state.mjs';
import {renderTaskResults} from './task-results.mjs';

test('a follow-up updates one sidebar conversation while keeping the original title',()=>{
 const root={id:'first',conversation_id:'first',objective:'核查临水堆存',status:'succeeded',created_at:'2026-10-27T01:00:00Z'};
 const summaries=upsertConversation([],root);
 const result=upsertConversation(summaries,{id:'second',conversation_id:'first',objective:'补充成都政策',status:'running',created_at:'2026-10-27T01:01:00Z'});
 assert.equal(result.length,1);assert.equal(result[0].id,'second');assert.equal(result[0].title,'核查临水堆存');
 assert.equal(result[0].created_at,root.created_at);
 assert.equal(result[0].turn_count,2);
});

test('turn updates preserve full earlier answers and merge overlapping history pages once',()=>{
 const original={id:'first',created_at:'2026-10-27T01:00:00Z',summary:'原答复',activity_entries:[{id:'a',summary:'原处理记录'}]};
 const next={id:'second',created_at:'2026-10-27T01:01:00Z',summary:'追问答复'};
 const result=mergeTurns([next],[original,{...next,status:'succeeded'}]);
 assert.equal(result.length,2);assert.deepEqual(result[0],original);assert.equal(result[1].summary,'追问答复');
 assert.equal(result[1].status,'succeeded');
});

test('server conversation revisions keep order when a clock moves backwards',()=>{
 const first={id:'first',created_at:'2026-10-27T14:01:00+08:00',core_report:{revision:1}};
 const second={id:'second',parent_task_id:'first',created_at:'2026-10-27T14:00:00+08:00',core_report:{revision:2}};
 assert.deepEqual(mergeTurns([second],[first]).map(turn=>turn.id),['first','second']);
});

test('reading reflow produces actual lists and preserves every factual clause',()=>{
 const source='建议核查以下内容：①物料来源与用途。②岸边排水去向。③是否存在有效拦截措施。';
 const result=readingText(source);
 assert.match(result,/- 物料来源/);assert.match(result,/- 岸边排水/);assert.match(result,/- 是否存在/);
 const numbered=readingText('1）核对许可；2）检查排水；3）记录物料性质。');
 assert.match(numbered,/1\. 核对许可/);assert.match(numbered,/\n2\. 检查排水/);
});

test('a long answer keeps a short lead and complete expandable evidence without cutting its final limitation',()=>{
 const original='可见露天堆体，需核对岸边排水。'.repeat(30)+'最后限制：尚无取样检测，不判断已经污染。';
 const excerpt=answerExcerpt(original);
 assert.ok(excerpt.lead.length<=260);assert.ok(excerpt.rest.endsWith('不判断已经污染。'));
 assert.equal((excerpt.lead+excerpt.rest).replace(/\s/g,''),readingText(original).replace(/\s/g,''));
 const html=renderReadableAnswer(original,{id:'original'});
 assert.match(html,/<details[^>]*data-ui-state="answer-original"/);assert.match(html,/最后限制/);assert.doesNotMatch(html,/<details[^>]*\sopen/);
});

test('consumer review names are uniform, including saved model identifiers',()=>{
 assert.equal(reviewText('见微初判与 Gemini 全面复核'),'见微识别与独立复核');
 assert.equal(reviewText('Gemini 复核：不确定。'),'独立复核：不确定。');
 assert.equal(reviewText('gemini-3.8-flash'),'独立复核模型');
});

test('a long contextual answer starts with the conclusion and collapses detailed sections',()=>{
 const presentation={enabled:true,workflow_mode:'contextual_qa',heading:'答复',lead:'建议优先核查排水和防流失设施。',sections:[
  {id:'policy',heading:'本轮检索到的政策与适用边界',paragraphs:['只引用已取得的原文片段。'.repeat(80)]},
  {id:'review',heading:'Gemini 全面复核',paragraphs:['需补充许可台账。']}
 ]};
 const html=renderTaskResults({id:'turn2',consumer_conclusion:{text:presentation.lead,presentation}});
 assert.ok(html.indexOf('建议优先')<html.indexOf('<details'));
 assert.match(html,/<summary>详细依据与核查清单/);assert.doesNotMatch(html,/Gemini/);assert.match(html,/独立复核/);
 assert.equal((html.match(/只引用已取得的原文片段/g)||[]).length,80);
});

test('core reports use the turn snapshot URL and reject executable URLs',()=>{
 const html=renderCoreReport({revision:3,artifacts:[
  {name:'report.html',url:'/portal/api/conversations/root/artifacts/report.html?through_task_id=turn3'},
  {name:'report.docx',url:'javascript:alert(1)'}]},{latest:true});
 assert.match(html,/会话核心报告/);assert.match(html,/第 3 版/);assert.match(html,/through_task_id=turn3/);assert.doesNotMatch(html,/javascript:/);
});
