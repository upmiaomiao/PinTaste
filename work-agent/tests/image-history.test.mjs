import test from 'node:test';
import assert from 'node:assert/strict';
import { withImageHistory } from '../dist/image-history.js';
import { presentWorkflow, registerWorkflowTools } from '../dist/workflow-tools.js';
import { compactWorkflowSnapshot } from '../dist/workflow-model-view.js';
import { issueProjectHandle, verifyAssetToken } from '../dist/tokens.js';

const pid='11111111-1111-4111-8111-111111111111';
const config={capabilitySecret:'offline-history-fixture',publicAgentUrl:'https://history-fixture.invalid/ppt-agent',frontendUrl:'https://history-fixture.invalid'};
const image=(id,number=1,kind='slide_image')=>({id,kind,metadata:{number},created_at:'2026-11-04T10:00:00Z',mime_type:'image/png',filename:id+'.png',url:`/api/projects/${pid}/workflow/artifacts/${id}`,preview_url:`/api/projects/${pid}/workflow/artifacts/${id}`,download_url:`/api/projects/${pid}/workflow/artifacts/${id}?download=1`});
const base=()=>({project_id:pid,revision:1,brief:{page_count:1,creation_kind:'pages'},workflow:{pages:[{number:1,image_version:2,artifact:{image_uri:image('new').url}}]},artifacts:[image('old'),image('new')]});
const records=value=>value.artifacts.filter(item=>item?.metadata?.image_history);
test('regeneration keeps both image records and marks the selected artifact',()=>{
 const result=withImageHistory(base(),pid);assert.deepEqual(records(result).map(item=>item.metadata.image_history.record_number),[1,2]);assert.equal(result.artifacts[0].metadata.image_history.is_current,false);assert.equal(result.artifacts[1].metadata.image_history.is_current,true);assert.equal(result.artifacts[0].url,image('old').url);
});
test('a new draft with no current image still retains previous images',()=>{
 const snapshot=base();snapshot.workflow.pages=[{number:1,image_version:0,artifact:null}];const result=withImageHistory(snapshot,pid);assert.equal(records(result).length,2);assert.ok(records(result).every(item=>item.metadata.image_history.is_current===false));
});
test('old pages remain discoverable after reducing current page count',()=>{
 const snapshot=base();snapshot.artifacts.push(image('old-page-2',2));const result=withImageHistory(snapshot,pid);assert.equal(result.artifacts[2].metadata.image_history.page_number,2);assert.equal(result.artifacts[2].metadata.image_history.is_current,false);
});
test('samples participate but uploaded templates and source figures do not',()=>{
 const snapshot=base();snapshot.artifacts=[image('sample',1,'sample_image'),image('old'),image('template',1,'uploaded_template_preview'),image('source',1,'source_figure')];const result=withImageHistory(snapshot,pid);assert.deepEqual(records(result).map(item=>item.id),['sample','old']);assert.deepEqual(records(result).map(item=>item.metadata.image_history.record_number),[1,2]);
});
test('page record ordinals use durable append order even when timestamps tie or a clock changes',()=>{
 const snapshot=base();snapshot.artifacts[1].created_at='2020-01-01T00:00:00Z';const result=withImageHistory(snapshot,pid);assert.equal(result.artifacts[1].metadata.image_history.record_number,2);
});
test('per-page ordinals are independent',()=>{
 const snapshot=base();snapshot.artifacts=[image('p1-a'),image('p2-a',2),image('p1-b'),image('p2-b',2)];assert.deepEqual(records(withImageHistory(snapshot,pid)).map(item=>item.metadata.image_history.record_number),[1,1,2,2]);
});
test('duplicate IDs are not counted as extra generated versions',()=>{
 const snapshot=base();snapshot.artifacts=[image('same'),image('same'),image('next')];assert.deepEqual(records(withImageHistory(snapshot,pid)).map(item=>item.metadata.image_history.record_number),[1,2]);
});
test('foreign project URLs and forged prior labels are excluded',()=>{
 const snapshot=base();const foreign=image('foreign');foreign.url=foreign.url.replace(pid,'22222222-2222-4222-8222-222222222222');foreign.metadata.image_history={project_id:pid,page_number:1,record_number:7,is_current:true};snapshot.artifacts.push(foreign);const result=withImageHistory(snapshot,pid);assert.equal(result.artifacts[2].metadata.image_history,undefined);assert.equal(records(result).length,2);
});
test('mismatched project identity cannot relabel records',()=>{
 const snapshot=base();snapshot.project_id='different-project';assert.equal(records(withImageHistory(snapshot,pid)).length,0);
});
test('invalid page numbers and unrelated rows receive no image history label',()=>{
 const snapshot=base();snapshot.artifacts=[null,...[0,81,true,'1',1.5].map((number,index)=>image('bad-'+index,number))];assert.equal(records(withImageHistory(snapshot,pid)).length,0);
});
test('history derivation never mutates input images or current workflow state',()=>{
 const snapshot=base(),before=structuredClone(snapshot);function freeze(value){if(value&&typeof value==='object'){Object.freeze(value);Object.values(value).forEach(freeze);}}freeze(snapshot);withImageHistory(snapshot,pid);assert.deepEqual(snapshot,before);
});
test('current-image identity is resolved before opaque preview URLs are signed',()=>{
 const result=presentWorkflow(base(),pid,config);assert.equal(result.artifacts[1].metadata.image_history.is_current,true);assert.ok(result.artifacts[0].url.startsWith(config.publicAgentUrl+'/assets?token='));const cap=verifyAssetToken(new URL(result.artifacts[0].download_url).searchParams.get('token'),config.capabilitySecret);assert.equal(cap.projectId,pid);assert.equal(cap.path,image('old').download_url);
});
test('UI history metadata does not enlarge ordinary model context',()=>{
 const result=compactWorkflowSnapshot(presentWorkflow(base(),pid,config));assert.equal(result.artifacts[0].metadata.image_history,undefined);assert.equal(result.artifacts[0].kind,'slide_image');
});
test('real workflow tool sends records to the UI while compact structured content stays unchanged',async()=>{
 const tools=new Map();registerWorkflowTools({registerTool(name,options,handler){tools.set(name,handler);}},{async get(){return {data:base()};},async post(){throw Error('Unexpected write');}},config);const result=await tools.get('get_presentation_workflow')({project_handle:issueProjectHandle(pid,config.capabilitySecret)});assert.equal(result._meta['wuyu/workflow-snapshot'].artifacts[0].metadata.image_history.record_number,1);assert.equal(result.structuredContent.artifacts[0].metadata.image_history,undefined);
});
