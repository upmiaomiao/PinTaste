import test from 'node:test';
import assert from 'node:assert/strict';
import { conversationSummary, registerWorkflowTools } from '../dist/workflow-tools.js';
import { compactWorkflowSnapshot } from '../dist/workflow-model-view.js';

globalThis.fetch=()=>{throw Error('All external/network calls forbidden in these tests')};
const fixture=(status='failed',count=25,partial=true)=>({project_id:'self-project-170',project_handle:'self-handle',revision:8,epoch:1,
  brief:{page_count:10},workflow:{phase:'requirements',pages:[]},jobs:[],materials:[],
  research:{required:true,status,mode:'deep',consent_current:true,report_id:'retained-report',can_use_existing_materials:true,
    can_supplement:count>0,ready_for_planning:false,progress:{source_count:count},
    partial_sources:Array.from({length:count},(_,n)=>({id:'S'+n,text:'PRIVATE_RAW',url:'https://example.invalid/'+n})),
    ...(partial?{report:{partial:true,summary:'',findings:[],sources:[]}}:{})}});

for(const [status,count,report] of [['failed',25,true],['paused',25,true],['failed',25,false],['failed',0,false]]) {
 test(`${status}/${count}/${report} points to a real no-search recovery control`,()=>{
  const current=fixture(status,count,report),before=JSON.stringify(current),result=conversationSummary(current);
  assert.equal(result.stage,'research_recovery');assert.equal(result.recovery.kind,'research');
  assert.equal(result.recovery.use_existing_materials,true);assert.equal(result.required_action,null);
  assert.deepEqual(result.choices,[]);assert.equal(result.review_target.controls_location,'conversation');
  assert.match(result.next_question,/不新增检索，改用现有材料/);assert.match(result.next_question,/右侧.*没有/);
  assert.equal(JSON.stringify(current),before);
 });
}
test('completed report remains on its old explicit reuse path',()=>{
 const current=fixture('accepted');Object.assign(current.research,{can_use_existing_materials:false,can_reuse_existing_report:true});
 assert.equal(conversationSummary(current).stage,'research_reuse');
});
test('old projection never advertises an absent supplement button for failed sources',()=>{
 const current=fixture();delete current.research.can_use_existing_materials;
 const result=conversationSummary(current);assert.equal(result.stage,'research_paused');assert.deepEqual(result.choices,[]);
});
test('material receipt removes old research failure as a blocker without completing research',()=>{
 const current=fixture();Object.assign(current.research,{can_use_existing_materials:false,can_supplement:false,materials_only_current:true,ready_for_planning:true});
 current.reading={protocol_version:1,status:'idle'};
 current.jobs=[{id:'old-research',status:'failed',action:'run_research',error_code:'research_synthesis_invalid',input_epoch:0}];
 const result=conversationSummary(current);assert.equal(current.research.status,'failed');
 assert.notEqual(result.stage,'research_recovery');assert.notEqual(result.stage,'research_paused');assert.notEqual(result.stage,'failed');
 assert.notEqual(result.required_action,'run_research');
});
test('material receipt resolves a legacy research failure without input_epoch, not an unrelated failure',()=>{
 const current=fixture();Object.assign(current.research,{can_use_existing_materials:false,can_supplement:false,materials_only_current:true,ready_for_planning:true});
 current.reading={protocol_version:1,status:'idle'};
 current.jobs=[{id:'legacy-research',status:'failed',action:'run_research',error_code:'research_synthesis_invalid'}];
 // A missing epoch is deliberately retained by currentWorkflowJobs. With no
 // material receipt, this exact job reaches the ordinary failure branch.
 const unconfirmed=structuredClone(current);unconfirmed.research.materials_only_current=false;
 const blocked=conversationSummary(unconfirmed);assert.equal(blocked.stage,'failed');assert.equal(blocked.failure.action,'run_research');
 const before=JSON.stringify(current),result=conversationSummary(current);
 assert.equal(result.stage,'reading');assert.equal(result.required_action,'analyze_materials');assert.equal(result.failure,undefined);
 assert.equal(JSON.stringify(current),before);assert.equal(current.research.status,'failed');
 const unrelated=structuredClone(current);unrelated.jobs[0].action='analyze_materials';
 const stillBlocked=conversationSummary(unrelated);assert.equal(stillBlocked.stage,'failed');assert.equal(stillBlocked.failure.action,'analyze_materials');
});
test('bounded model view preserves only public material-scope flags and no raw source text',()=>{
 const current=fixture(),before=JSON.stringify(current),result=compactWorkflowSnapshot(current);
 assert.equal(result.research.can_use_existing_materials,true);assert.equal(result.research.materials_only_current,undefined);
 current.research.materials_only_current=true;assert.equal(compactWorkflowSnapshot(current).research.materials_only_current,true);
 assert.doesNotMatch(JSON.stringify(result),/PRIVATE_RAW/);assert.equal(JSON.stringify({...current,research:{...current.research,materials_only_current:undefined}}),before);
});
test('MCP cannot stand in for a real user material-scope or paid-research confirmation',()=>{
 const tools=new Map();registerWorkflowTools({registerTool:(name,options,callback)=>tools.set(name,{options,callback})},new Proxy({},{get(){throw Error('API forbidden')}}),{capabilitySecret:'self-fixture'});
 const schema=tools.get('presentation_workflow_action').options.inputSchema.action;
 for(const action of ['confirm_research','supplement_research','accept_research'])assert.equal(schema.safeParse(action).success,false);
});
