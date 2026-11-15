import test from 'node:test';
import assert from 'node:assert/strict';
import { conversationSummary, registerWorkflowTools, workflowContent } from '../dist/workflow-tools.js';
import { issueProjectHandle } from '../dist/tokens.js';

const config={capabilitySecret:'offline-fixture-only',frontendUrl:'https://fixture.invalid'};
const pid='11111111-1111-4111-8111-111111111111';
const base=()=>({project_id:pid,revision:7,epoch:1,brief:{page_count:1},workflow:{pages:[]},materials:[],artifacts:[],reading:{protocol_version:1,status:'reading',selected_materials:1,read_materials:0,total_reader_tasks:1,completed_reader_tasks:0,total_segments:302,completed_segments:0},jobs:[{id:'reader-job',action:'analyze_materials',status:'running',input_epoch:1}]});
function runtime(snapshot,fail=false){
 const tools=new Map();let reads=0;
 registerWorkflowTools({registerTool(name,options,handler){tools.set(name,{options,handler});}},{async get(){reads++;if(fail)throw Error('fixture transport unavailable');return {data:snapshot};},async post(){throw Error('Read-only wait attempted a write');}},config);
 return {tools,get reads(){return reads;},wait:(overrides={})=>tools.get('wait_presentation_workflow').handler({project_handle:issueProjectHandle(pid,config.capabilitySecret),job_id:'reader-job',after_revision:7,timeout_ms:0,...overrides},{signal:new AbortController().signal})};
}
test('running single material reports verified completions rather than a streaming percentage',()=>{
 const summary=conversationSummary(base());assert.equal(summary.stage,'working');assert.equal(summary.stop_wait,false);assert.equal(summary.required_action,'wait_presentation_workflow');assert.match(summary.summary,/整份材料读完并校验后更新/);assert.equal(summary.progress.reading.completed_materials,0);assert.equal(summary.progress.reading.total_materials,1);
});
test('synthesis displays page planning after verified source reading',()=>{
 const snapshot=base();Object.assign(snapshot.reading,{source_reading_complete:true,read_materials:1,completed_reader_tasks:1,completed_segments:302,synthesis:{status:'pending'}});const summary=conversationSummary(snapshot);assert.match(summary.summary,/正在整理逐页内容计划/);assert.equal(summary.progress.reading.stage,'planning');assert.equal(summary.progress.reading.completed_segments,302);assert.equal(summary.stop_wait,false);
});
test('invalid wire counters never become progress claims',()=>{
 const snapshot=base();Object.assign(snapshot.reading,{completed_segments:'302',completed_reader_tasks:-1,total_segments:NaN});const progress=conversationSummary(snapshot).progress.reading;assert.equal(progress.completed_segments,0);assert.equal(progress.completed_reader_tasks,0);assert.equal(progress.total_segments,0);
});
test('bounded wait expiry is nonterminal and carries the same instruction in textual and structured paths',async()=>{
 const run=runtime(base());const result=await run.wait();assert.equal(run.reads,1);assert.equal(result.structuredContent.wait_status,'timeout');assert.equal(result.structuredContent.stop_wait,false);const context=JSON.parse(result.content[1].text);assert.match(context.wait_instruction,/不是阅读任务失败或任务超时/);assert.match(context.wait_instruction,/原 job_id/);assert.match(context.wait_instruction,/不要仅因计数暂时不变/);assert.equal(context.wait_job_id,'reader-job');assert.equal(context.changed,false);
});
for(const status of ['failed','cancelled','paused'])test(status+' stops waiting without a paid retry',async()=>{
 const snapshot=base();Object.assign(snapshot.jobs[0],{status,error_code:status==='failed'?'reading_failed':undefined,retryable:false});if(status==='paused')snapshot.paused=true;const run=runtime(snapshot);const result=await run.wait();assert.equal(result.structuredContent.stop_wait,true);const context=JSON.parse(result.content[1].text);assert.doesNotMatch(context.wait_instruction||'',/这不是阅读任务失败/);assert.equal(run.reads,1);
});
test('completed draft stops at actual draft review',async()=>{
 const snapshot=base();snapshot.jobs[0].status='completed';Object.assign(snapshot.reading,{status:'draft_ready',draft_revision:1,coverage_complete:true,can_continue:true,source_reading_complete:true,content_plan_complete:true,read_materials:1});const result=await runtime(snapshot).wait();assert.equal(result.structuredContent.wait_status,'settled');assert.equal(result.structuredContent.stop_wait,true);assert.equal(result.structuredContent.conversation.stage,'draft_review');
});
test('superseded wait target is terminal even while another current job runs',async()=>{
 const snapshot=base();snapshot.jobs[0].input_epoch=0;snapshot.jobs.push({id:'current-job',action:'analyze_materials',input_epoch:1,status:'running'});const result=await runtime(snapshot).wait();assert.equal(result.structuredContent.wait_status,'superseded');assert.equal(result.structuredContent.stop_wait,true);
});
test('missing target and state transport errors end polling',async()=>{
 const snapshot=base();snapshot.jobs=[];for(const run of [runtime(snapshot),runtime(base(),true)]){const result=await run.wait();assert.equal(result.structuredContent.wait_status,'error');assert.equal(result.structuredContent.stop_wait,true);assert.equal(run.reads,1);}
});
test('wait tool keeps its read-only annotation and 25 second validated ceiling',()=>{
 const {options}=runtime(base()).tools.get('wait_presentation_workflow');assert.equal(options.annotations.readOnlyHint,true);assert.equal(options.inputSchema.timeout_ms.parse(undefined),25000);assert.throws(()=>options.inputSchema.timeout_ms.parse(25001));assert.match(options.description,/not a background job failure/);
});
test('stale reading does not label an unrelated image job as reading progress',()=>{
 const snapshot=base();snapshot.jobs[0].action='generate_images';assert.equal(conversationSummary(snapshot).progress.reading,undefined);
});
test('a snapshot with no pending jobs settles rather than claiming an expired reading wait',async()=>{
 const snapshot=base();snapshot.jobs=[];const result=await runtime(snapshot).wait({job_id:undefined});assert.equal(result.structuredContent.wait_status,'settled');assert.equal(result.structuredContent.stop_wait,true);
});
