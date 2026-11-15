import assert from 'node:assert/strict';
import test from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createBananaMcpServer } from '../dist/server.js';
import { safeError, conversationSummary } from '../dist/workflow-tools.js';
import { actionRecovery, workflowErrorGuidance, editableExportInstruction } from '../dist/workflow-action-contract.js';
import { BananaApiError } from '../dist/api-client.js';
import { issueProjectHandle } from '../dist/tokens.js';

const id='70000000-0000-4000-8000-000000000085',secret='self-created-recovery85-secret';
const config={capabilitySecret:secret,frontendUrl:'https://fixture.invalid',publicAgentUrl:'https://fixture.invalid/ppt-agent',publicBackendUrl:'http://fixture.invalid',backendUrl:'http://fixture.invalid'};
const handle=issueProjectHandle(id,secret);
const snapshot={project_id:id,revision:5,epoch:1,paused:false,brief:{creation_kind:'pages',page_count:1,language:'zh',output_preference:'image'},workflow:{phase:'requirements',output_preference:'image',pages:[],template:{samples:{}}},materials:[],jobs:[],artifacts:[]};
const privateText='self85 private prose normal words; sk-self85-secret https://private.invalid/?token=self85 C:/Users/self85/secret.txt';
const cases=[
 ['DRAFT_REVIEW_REQUIRED',409,'review_current_draft',true],
 ['RESEARCH_REVIEW_REQUIRED',409,'review_research_in_ui',true],
 ['RESEARCH_CONFIRMATION_REQUIRED',403,'obtain_research_consent_in_ui',true],
 ['TEMPLATE_SELECTION_REQUIRED',409,'preview_and_confirm_template_in_ui',true],
 ['TEMPLATE_CONFIRMATION_UI_REQUIRED',403,'preview_and_confirm_template_in_ui',true],
 ['REVISION_CONFLICT',409,'read_current_revision',false],
 ['IDEMPOTENCY_CONFLICT',409,'inspect_prior_request',false],
 ['IDEMPOTENCY_CONTEXT_CHANGED',409,'inspect_prior_request',false],
 ['QUOTA_EXCEEDED',403,'wait_for_quota',false],
 ['PAGE_REVISION_INPUT_INVALID',400,'correct_page_revision_input',false],
 ['INVALID_WORKFLOW_ACTION',400,'inspect_current_step',false],
 ['WORKFLOW_BUSY',409,'inspect_pending_jobs',false],
 ['reading_cached_plan_invalid',409,'request_specific_draft_feedback',true],
 ['FORBIDDEN',403,'reopen_authorized_project',false],
 ['UNAUTHORIZED',401,'reopen_authorized_project',false],
];
for(const [code,status,next,confirm]of cases)test('fixed safe guidance: '+code,()=>{
 const payload={error:{code,message:privateText,api_key:'self85-private-key',details:{url:'https://private.invalid'}}};
 const known=workflowErrorGuidance(payload),safe=safeError(new BananaApiError(privateText,status,payload)),recovery=actionRecovery('generate_images',status,payload);
 assert.equal(known.error_code,code);assert.equal(recovery.error_code,code);assert.equal(recovery.next_action,next);assert.equal(recovery.requires_user_confirmation,confirm);assert.equal(recovery.repeat_paid_action_automatically,false);
 assert.equal(safe.isError,true);assert.equal(safe.content[0].text,known.message);assert(!JSON.stringify({safe,recovery}).includes('self85'));
 assert(!safe.content[0].text.includes('刷新状态后重试'));assert(!Object.hasOwn(recovery,'payload'));assert(!Object.hasOwn(recovery,'message'));assert(!Object.hasOwn(recovery,'diagnostics'));
});
test('same 409 has distinct draft/template/version/idempotency/quota next decisions',()=>{
 const decisions=['DRAFT_REVIEW_REQUIRED','TEMPLATE_SELECTION_REQUIRED','REVISION_CONFLICT','IDEMPOTENCY_CONFLICT','QUOTA_EXCEEDED'].map(code=>actionRecovery('export',409,{error:{code}}).next_action);assert.equal(new Set(decisions).size,decisions.length);
});
test('unknown 400/409/422 never echoes even ordinary backend prose',()=>{
 for(const status of [400,409,422]){
  const safe=safeError(new BananaApiError('Plain ordinary self85 prose without credential patterns',status,{error:{code:'UNKNOWN_PRIVATE_self85',message:privateText}})),recovery=actionRecovery('export',status,{error:{code:'UNKNOWN_PRIVATE_self85',message:privateText}});
  assert.equal(recovery.error_code,'WORKFLOW_ACTION_FAILED');assert.equal(recovery.next_action,'inspect_current_step');assert(!JSON.stringify({safe,recovery}).includes('self85'));assert(!safe.content[0].text.includes('项目已更新'));assert.equal(recovery.repeat_paid_action_automatically,false);
 }
});
test('only an exact nested whitelisted code is accepted, not shape or prose hints',()=>{
 for(const payload of [null,[],{error:[]},{error:{code:'__proto__'}},{error:{code:'constructor'}},{error:{code:42}},{code:'DRAFT_REVIEW_REQUIRED'},{error:{code:'DRAFT_REVIEW_REQUIRED\n'}},{error:{message:'DRAFT_REVIEW_REQUIRED'}},{error:{code:'source_hash_mismatch',diagnostics:{validation_code:'reading_cached_plan_invalid'}}}])assert.equal(workflowErrorGuidance(payload),null);
});
test('security/source/cache-container errors never receive feedback recovery',()=>{
 for(const code of ['source_hash_mismatch','reading_cache_integrity_failed','cache_path_invalid','source_path_invalid','security_violation','reading_synthesis_source_mismatch']){
  const r=actionRecovery('analyze_materials',409,{error:{code,diagnostics:{response_cached:true,validation_code:'reading_cached_plan_invalid'}}});assert.equal(r.error_code,'WORKFLOW_ACTION_FAILED');assert.notEqual(r.next_action,'request_specific_draft_feedback');assert.equal(r.repeat_paid_action_automatically,false);
 }
});
test('83 image-first conversion instructions and exact safe rejection remain',()=>{
 assert(editableExportInstruction.includes("format:'editable_pptx'"));const r=actionRecovery('start_tracing',400,{error:{code:'INVALID_REQUEST',message:privateText}});assert(r.instruction.includes('conversation.editable_conversion'));assert(r.instruction.includes('Do not repeat confirm_images'));const s=safeError(new BananaApiError('All current images must be reviewed before tracing',400));assert(s.content[0].text.includes('可编辑转换尚未启动'));assert.equal(actionRecovery('start_tracing',400,{error:{code:'QUOTA_EXCEEDED'}}).next_action,'wait_for_quota');
});
test('unknown action cannot echo arbitrary strings through recovery metadata',()=>{
 const r=actionRecovery(privateText,409,{error:{code:'UNKNOWN'}});assert.equal(r.action,'unknown');assert(!JSON.stringify(r).includes('self85'));
});
test('validated cached-plan failure asks for user feedback without required paid action',()=>{
 const data=structuredClone(snapshot);data.reading={protocol_version:1,status:'failed',draft_revision:3,coverage_complete:false};data.jobs=[{id:'fixture-plan85',action:'analyze_materials',status:'failed',input_epoch:1,error_code:'reading_cached_plan_invalid',error_message:'self fixture plan failed',retryable:false}];const c=conversationSummary(data);assert.equal(c.stage,'content_plan_recovery');assert.equal(c.required_action,null);assert.equal(c.requires_user_confirmation,true);assert.equal(c.recovery.keep_project,true);assert.equal(c.recovery.keep_reading,true);assert.equal(c.recovery.next_action,'request_specific_draft_feedback');assert.equal(c.recovery.repeat_paid_action_automatically,false);assert.equal(c.review_target.draft_revision,3);
});
test('real SDK responses preserve code, decision and secrecy without implicit retry',async()=>{
 let active={code:'DRAFT_REVIEW_REQUIRED',status:409},posts=0,reads=0;
 const api={async get(path){reads++;assert.equal(path,`/api/projects/${id}/workflow`);return{data:structuredClone(snapshot)}},async post(path){posts++;assert.equal(path,`/api/projects/${id}/workflow/action`);throw new BananaApiError(privateText,active.status,{error:{code:active.code,message:privateText,keys:['self85'],details:{url:'https://private.invalid'}}})}};
 const server=createBananaMcpServer(config,api),client=new Client({name:'self-recovery85',version:'1'}),[a,b]=InMemoryTransport.createLinkedPair();await server.connect(b);await client.connect(a);
 try{
  const schema=(await client.listTools()).tools.find(tool=>tool.name==='presentation_workflow_action');assert(schema.description.includes('conversation.editable_conversion'));assert(!schema.description.includes('Only then start_tracing.'));assert(!schema.inputSchema.properties.action.enum.includes('confirm_template_choice'));assert(!schema.inputSchema.properties.action.enum.includes('confirm_research'));
  for(const [code,status,next,confirm]of cases){active={code,status};const before=posts,r=await client.callTool({name:'presentation_workflow_action',arguments:{project_handle:handle,action:'export',payload:{format:'editable_pptx'},expected_revision:5,request_id:'00000000-0000-4000-8000-000000000085'}});assert.equal(r.isError,true);assert.equal(posts,before+1);assert.equal(reads,0);assert.equal(r.structuredContent.workflow_action_error.error_code,code);assert.equal(r.structuredContent.workflow_action_error.next_action,next);assert.equal(r.structuredContent.workflow_action_error.requires_user_confirmation,confirm);assert(!JSON.stringify(r).includes('self85'));assert.equal(r.structuredContent.workflow_action_error.repeat_paid_action_automatically,false);}
  active={code:'UNKNOWN_PRIVATE_self85',status:409};const unknown=await client.callTool({name:'presentation_workflow_action',arguments:{project_handle:handle,action:'export',payload:{format:'editable_pptx'}}});assert.equal(unknown.structuredContent.workflow_action_error.error_code,'WORKFLOW_ACTION_FAILED');assert(!JSON.stringify(unknown).includes('self85'));
  const accepted=await client.callTool({name:'get_presentation_workflow',arguments:{project_handle:handle}});assert(!accepted.isError);assert.equal(reads,1);assert.equal(posts,cases.length+1);
 }finally{await client.close();await server.close()}
});
