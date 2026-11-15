import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { conversationSummary, registerWorkflowTools } from '../dist/workflow-tools.js';
import { compactWorkflowSnapshot } from '../dist/workflow-model-view.js';
const require=createRequire(import.meta.url);
const {transform,rules}=require('../tools/research-reuse-flow.cjs');
globalThis.fetch=()=>{throw Error('Network forbidden in local contract tests')};
const snapshot=()=>({project_id:'own-project',revision:8,epoch:1,brief:{page_count:12},workflow:{phase:'requirements',pages:[]},jobs:[],materials:[],research:{required:true,status:'choice_required',consent_current:false,can_reuse_existing_report:true,reused_for_current_scope:false,report_id:'old-report',report:{sources:[{id:'s1',url:'https://example.invalid',text:'PRIVATE_RAW'}],findings:[]}}});
test('retained report gives explicit existing-materials control, no generation action',()=>{const s=snapshot(),before=JSON.stringify(s),r=conversationSummary(s);assert.equal(r.stage,'research_reuse');assert.equal(r.required_action,null);assert.deepEqual(r.choices,[]);assert.equal(r.review_target.controls_location,'current_requirements');assert.equal(r.review_target.report_id,'old-report');assert.match(r.summary,/仍保留/);assert.doesNotMatch(r.summary,/未启动搜索/);assert.equal(JSON.stringify(s),before)});
test('ineligible report retains existing search choice behavior',()=>{const s=snapshot();s.research.can_reuse_existing_report=false;const r=conversationSummary(s);assert.equal(r.stage,'research_choice');assert.equal(r.review_target.controls_location,'composer')});
test('reuse flags reach bounded model view, raw source text does not',()=>{const s=snapshot(),before=JSON.stringify(s),r=compactWorkflowSnapshot(s);assert.equal(r.research.can_reuse_existing_report,true);assert.equal(r.research.reused_for_current_scope,false);assert.doesNotMatch(JSON.stringify(r),/PRIVATE_RAW/);assert.equal(JSON.stringify(s),before)});
test('actual MCP enum cannot grant report or template consent',()=>{const tools=new Map();registerWorkflowTools({registerTool:(name,options,callback)=>tools.set(name,{options,callback})},new Proxy({},{get(){throw Error('API must not be called')}}),{capabilitySecret:'fixture-secret'});const schema=tools.get('presentation_workflow_action').options.inputSchema.action;for(const action of ['accept_research','confirm_research','supplement_research','confirm_template','confirm_template_choice'])assert.equal(schema.safeParse(action).success,false);assert.equal(schema.safeParse('request_research').success,true)});
for(const name of Object.keys(rules)){
 test('generator reproduces actual reviewed runtime '+name,()=>{const current=fs.readFileSync(new URL('../dist/'+name.split('/').pop(),import.meta.url));assert.deepEqual(transform(name,current),current);if(process.env.REUSE134_BASELINE){const base=fs.readFileSync(process.env.REUSE134_BASELINE+'/'+name);assert.deepEqual(transform(name,base),current)}});
 test('generator refuses unknown drift '+name,()=>assert.throws(()=>transform(name,Buffer.from('unreviewed runtime'))));
}
