import assert from 'node:assert/strict';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {InMemoryTransport} from '@modelcontextprotocol/sdk/inMemory.js';
import {createBananaMcpServer} from '../dist/server.js';
import {conversationSummary,workflowContent,safeError} from '../dist/workflow-tools.js';
import {actionRecovery} from '../dist/workflow-action-contract.js';
import {BananaApiError} from '../dist/api-client.js';
import {issueProjectHandle} from '../dist/tokens.js';
const id='70000000-0000-4000-8000-000000000083',secret='self-created-export83-secret';
const base={project_id:id,revision:88,epoch:6,paused:false,brief:{creation_kind:'pages',page_count:6,language:'zh',output_preference:'image'},workflow:{phase:'exported',output_preference:'image',review_policy:'template_and_final',template:{samples:{}},pages:Array.from({length:6},(_,i)=>({number:i+1,image_version:2,generation_status:'ready',confirmed_image_version:null,trace_version:0,artifact:{image_uri:`/api/projects/${id}/workflow/artifacts/self-image-${i+1}`}}))},template_selection:{confirmed:true},export_capabilities:{image_pptx:true},materials:[],artifacts:[],jobs:[]};
let checks=0;const check=(name,fn)=>{fn();checks++;};
check('explicit image-first conversion route without automatic execution',()=>{const s=conversationSummary(base);assert(['delivered_images','exported'].includes(s.stage));assert.equal(s.required_action,null);assert.equal(s.requires_user_confirmation,false);assert.equal(s.editable_conversion.action,'export');assert.deepEqual(s.editable_conversion.payload,{format:'editable_pptx'});assert.equal(s.editable_conversion.expected_revision,88);assert.equal(s.editable_conversion.requires_explicit_user_request,true);assert.equal(s.editable_conversion.may_call_paid_model,true);});
for(const [name,edit]of[
 ['unreviewed',s=>{s.export_capabilities.image_pptx=false;s.workflow.phase='final_image_review';}],
 ['stale-review',s=>{s.export_capabilities.image_pptx=false;s.workflow.pages[0].image_version++;}],
 ['paused',s=>{s.paused=true;}],
 ['running',s=>{s.jobs=[{id:'running',action:'export',status:'running',input_epoch:6}];}],
 ['failed',s=>{s.jobs=[{id:'failed',action:'export',status:'failed',input_epoch:6,error_code:'invalid_trace'}];}],
 ['native-tracing',s=>{s.brief.output_preference='editable';s.workflow.output_preference='editable';s.workflow.phase='tracing';}],
 ['non-delivery-phase',s=>{s.workflow.phase='page_generation';}],
 ['inconsistent-delivery-mode',s=>{s.workflow.output_preference='editable';}],
])check(name+' does not imply permission to begin conversion',()=>{const s=structuredClone(base);edit(s);assert.equal(conversationSummary(s).editable_conversion,undefined);});
check('ready-to-export and deck use same explicit route',()=>{const s=structuredClone(base);s.brief.creation_kind='deck';s.workflow.phase='ready_to_export';assert.equal(conversationSummary(s).editable_conversion.action,'export');});
check('failed wrong action supplies conditional recovery, never retry',()=>{const r=actionRecovery('start_tracing',400,{error:{code:'INVALID_REQUEST',message:'private text https://fixture.invalid sk-fake-private'}});assert.equal(r.next_tool,'get_presentation_workflow');assert.equal(r.repeat_paid_action_automatically,false);assert(r.instruction.includes('conversation.editable_conversion'));assert(r.instruction.includes('explicit conversion request'));assert(!JSON.stringify(r).includes('sk-fake'));});
check('quota still stops conversion',()=>{const r=actionRecovery('start_tracing',400,{error:{code:'QUOTA_EXCEEDED'}});assert(r.instruction.startsWith('Stop work.'));});
check('misleading image-review error has accurate safe localized explanation',()=>{const error=safeError(new BananaApiError('All current images must be reviewed before tracing',400));assert(error.isError);assert(error.content[0].text.includes('可编辑转换尚未启动'));assert(error.content[0].text.includes('只有当前版本未审核'));});
let current=structuredClone(base),reads=0,posts=[];
const api={async get(path){assert.equal(path,`/api/projects/${id}/workflow`);reads++;return{data:structuredClone(current)};},async post(path,body){assert.equal(path,`/api/projects/${id}/workflow/action`);posts.push(structuredClone(body));if(body.action==='start_tracing')throw new BananaApiError('All current images must be reviewed before tracing',400,{error:{code:'INVALID_REQUEST'}});assert.equal(body.action,'export');assert.deepEqual(body.payload,{format:'editable_pptx'});assert.equal(body.expected_revision,88);current={...structuredClone(base),revision:89,brief:{...base.brief,output_preference:'editable'},workflow:{...base.workflow,phase:'tracing',output_preference:'editable'},jobs:[{id:'conversion83',action:'export',status:'queued',input_epoch:6}]};return{data:structuredClone(current)};}};
const config={capabilitySecret:secret,frontendUrl:'https://fixture.invalid',publicAgentUrl:'https://fixture.invalid/ppt-agent',publicBackendUrl:'http://fixture.invalid',backendUrl:'http://fixture.invalid'};
const handle=issueProjectHandle(id,secret),server=createBananaMcpServer(config,api),client=new Client({name:'editable-export83',version:'1'}),[a,b]=InMemoryTransport.createLinkedPair();await server.connect(b);await client.connect(a);
try{
 const tools=(await client.listTools()).tools,action=tools.find(t=>t.name==='presentation_workflow_action');assert(action.description.includes('conversation.editable_conversion'));assert(!action.description.includes('Only then start_tracing.'));checks++;
 const rejected=await client.callTool({name:'presentation_workflow_action',arguments:{project_handle:handle,action:'start_tracing',expected_revision:88,request_id:'00000000-0000-4000-8000-000000000083'}});assert(rejected.isError);assert(rejected.structuredContent.workflow_action_error.instruction.includes('editable_pptx'));assert.equal(posts.length,1);assert.equal(reads,0);checks++;
 let read=await client.callTool({name:'get_presentation_workflow',arguments:{project_handle:handle}});assert(!read.isError);assert.equal(reads,1);assert.equal(posts.length,1);const hint=read.structuredContent.conversation.editable_conversion;assert(hint);assert.equal(read.structuredContent.conversation.required_action,null);assert(read.content.some(c=>c.type==='text'&&c.text.includes('editable_conversion')));checks++;
 await client.callTool({name:'get_presentation_workflow',arguments:{project_handle:handle}});assert.equal(posts.length,1);checks++;
 const done=await client.callTool({name:'presentation_workflow_action',arguments:{project_handle:handle,action:hint.action,payload:hint.payload,expected_revision:hint.expected_revision,request_id:'10000000-0000-4000-8000-000000000083'}});assert(!done.isError,JSON.stringify(done.content));assert.equal(posts.length,2);assert.equal(posts[1].action,'export');assert(!Object.hasOwn(posts[1].payload,'rebuild'));assert.equal(done.structuredContent.conversation.editable_conversion,undefined);assert.equal(done.structuredContent.conversation.required_action,'wait_presentation_workflow');checks++;
 const denied=await client.callTool({name:'presentation_workflow_action',arguments:{project_handle:'invalid-self-created-handle83',action:'export',payload:{format:'editable_pptx'}}});assert(denied.isError);assert.equal(posts.length,2);checks++;
 console.log(JSON.stringify({passed:true,checks,actual_MCP_SDK_in_memory:true,external_backend_transport_only_mocked:true,no_automatic_conversion_or_approval:true,backend_posts_are_fixture_only:posts.length,live_model_search_cos_calls:0}));
}finally{await client.close();await server.close();}
