import test from 'node:test';
import assert from 'node:assert/strict';
import {questionCard,sourcePicker,locationPicker,nextQuestions,createWorkspaceController} from './workspace.mjs';
import {taskPayload} from './lib.mjs';

test('question cards ask one question and keep free text available',()=>{
 const html=questionCard({body:{objective:'检查<img src=x onerror=alert(1)>'},question:{question:'在哪里？',options:[{value:'a',label:'地名',description:'搜索地址'}]}});
 assert.match(html,/在哪里/);assert.match(html,/直接回答/);assert.doesNotMatch(html,/<img src=x/);
});
test('source picker cannot select an unconfigured source',()=>{
 const html=sourcePicker([{source:'bing',group:'optical',selectable:true},{source:'mapbox',group:'optical',selectable:false,reason:'upstream_not_configured'}]);
 assert.match(html,/value="mapbox"[^>]*disabled/);assert.match(html,/不等于独立卫星/);
});
test('locations are candidates, not independently verified waste locations',()=>{
 const html=locationPicker({candidates:[{id:'a',name:'<script>x</script>',address:'同名园区'}]});
 assert.doesNotMatch(html,/<script>/);assert.match(html,/近似 WGS84/);assert.match(html,/不证明/);
});
test('current source choices and permissions are explicit task fields',()=>{
 const body=taskPayload({objective:'这次用多个来源',files:[],selected_sources:['bing','arcgis'],online_supplement:'allowed',location_id:'selected-place',coordinate_system:'wgs84'},'previous');
 assert.deepEqual(body.selected_sources,['bing','arcgis']);assert.equal(body.online_supplement,'allowed');assert.equal(body.location_id,'selected-place');
});
test('preflight never submits a task while a question is pending',async()=>{
 const state={draft:{}},submitted=[],controller=createWorkspaceController({state,api:async()=>({ready:false,question:{kind:'location'}}),render(){},showModal(){},closeModal(){},toast(){},submitBody:b=>submitted.push(b),updateComposer(){}});
 await controller.start({objective:'看这个园区'});assert.equal(submitted.length,0);assert.equal(state.guide.question.kind,'location');
});
test('next-step suggestions do not execute tools',()=>{
 const suggestions=nextQuestions({status:'succeeded',images:[{metadata:{modality:'optical'}}],narrative:{recommendations:['核对北侧候选堆体的现场照片']}});
 assert.match(suggestions[0],/现场照片/);assert.deepEqual(nextQuestions({status:'succeeded',images:[{}]}),[]);assert.deepEqual(nextQuestions({status:'running',images:[{}]}),[]);
});
test('risk confirmation accepts a free reply and checks negation before confirmation',async()=>{
 for(const [text,value] of [['我已经确认存在固废堆存','user_confirmed'],['尚未确认，先评估可能风险','unverified']]){
  const requests=[],submitted=[],state={guide:{body:{objective:'评估这张图的环境风险'},question:{kind:'waste_confirmation'}},draft:{}};
  const controller=createWorkspaceController({state,render(){},updateComposer(){},api:async(_path,options)=>{
   requests.push(options.body);return {ready:true,task:{objective:'评估这张图的环境风险',workflow_mode:'environmental_risk'}};
  },submitBody:body=>submitted.push(body)});
  assert.equal(await controller.free(text),true);assert.equal(requests[0]._reply.value,value);assert.equal(submitted[0].workflow_mode,'environmental_risk');
 }
});
test('prepared task retains server-confirmed location when submitted',async()=>{
 const sent=[],controller=createWorkspaceController({state:{draft:{}},api:async()=>({ready:true,task:{objective:'核查园区',center:[116,40],coordinate_system:'wgs84',location_id:'owned-location'}}),submitBody:b=>sent.push(b)});
 await controller.start({objective:'核查园区'});assert.equal(sent[0].location_id,'owned-location');assert.deepEqual(sent[0].center,[116,40]);
});
test('selecting an answer waits for explicit confirmation',async()=>{
 const state={guide:{body:{objective:'核查园区'},question:{kind:'online_supplement'}},draft:{}},sent=[],controller=createWorkspaceController({state,render(){},api:async()=>({ready:true}),submitBody:b=>sent.push(b)});
 await controller.action('guide-select',{dataset:{value:'uploaded_only'}});assert.equal(state.guide.selected,'uploaded_only');assert.equal(sent.length,0);
});
