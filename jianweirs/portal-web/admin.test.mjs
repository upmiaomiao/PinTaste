import test from 'node:test';
import assert from 'node:assert/strict';
import {renderAdmin,handleAdminAction,handleAdminSubmit,handleAdminModelInput,modelInputLimits} from './admin.mjs';

const icon=name=>`<svg data-icon="${name}"></svg>`;
const user={id:'admin',role:'admin',username:'administrator',display_name:'管理员'};
function state(){return {user,adminTab:'overview',config:{models:{},data:{base_url:'http://127.0.0.1:8765'},task_policy:{}},users:[user],tasks:[],draft:{},error:'',adminOverview:{updated_at:1,counts:{registered_users:13,active_users:11,running:2,planning:3,queued:4,completed:163,total_tasks:180,failed:5,interrupted:3},models:[{role:'main',model:'actual-model',configured:true,max_tokens:4096,input_modalities:['text','image']}],recent_tasks:[{id:'private-task',user_display_name:'<img onerror="bad">',objective:'真实任务 <script>bad</script>',status:'planning',created_at:1}]}};}
test('administrator overview uses platform snapshot and safe real task summaries',()=>{
  const html=renderAdmin(state(),icon);
  assert.match(html,/注册用户/);assert.match(html,/>13</);assert.match(html,/>163</);assert.match(html,/actual-model/);
  assert.match(html,/任务规划中/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|onerror="bad"/);
  assert.match(html,/尚未检查连接/);assert.doesNotMatch(html,/131,072|16,384 上下文|语音服务|容量与额度/);
});
test('missing overview never substitutes current-user tasks or zero platform counts',()=>{
  const value=state();value.adminOverview=null;
  const html=renderAdmin(value,icon);
  assert.match(html,/读取运行概览/);assert.doesNotMatch(html,/class="admin-stat"/);
});
test('official DeepSeek alias shows the verified version while custom provider names remain unchanged',()=>{
  const value=state();value.config.models.main={model:'deepseek-flash',base_url:'https://api.deepseek.com/v1'};
  value.adminOverview.models[0].model='deepseek-flash';
  assert.match(renderAdmin(value,icon),/DeepSeek V4\.1 Flash 1017/);
  value.adminTab='models';assert.match(renderAdmin(value,icon),/DeepSeek V4\.1 Flash 1017/);
  value.config.models.main.base_url='https://custom.example/v1';
  assert.doesNotMatch(renderAdmin(value,icon),/DeepSeek V4\.1 Flash 1017/);
});
test('all existing administrator configuration roles and policies remain editable',()=>{
  const value=state();value.adminTab='models';let html=renderAdmin(value,icon);
  for(const role of ['main','selector','recognizer','science_analyst','reasoner'])for(const field of ['model','base_url','api_key','input_modalities','timeout','max_tokens','trust_env','enable_thinking'])assert.ok(html.includes(`name="models.${role}.${field}"`),role+'.'+field);
  assert.match(html,/Gemini 视觉复核与科学影像/);assert.match(html,/DMX Gemini/);
  value.adminTab='policy';html=renderAdmin(value,icon);
  for(const field of ['selector_mode','adaptive_review','search_policy','consult_shensi','max_policy_queries','max_assets','concurrency','rs_output_format'])assert.ok(html.includes(`name="task_policy.${field}"`),field);
  for(const [tab,field] of [['data','data.base_url'],['search','metaso.api_key']]){value.adminTab=tab;assert.ok(renderAdmin(value,icon).includes(`name="${field}"`));}
});
test('official model controls allow the documented output ceiling and long timeout',()=>{
  const value=state();value.adminTab='models';value.config.models.main={model:'deepseek-flash',base_url:'https://api.deepseek.com/v1',max_tokens:393216,timeout:3600};
  const html=renderAdmin(value,icon);
  assert.match(html,/name="models\.main\.max_tokens"[^>]*min="1" max="393216"/);
  assert.match(html,/name="models\.main\.timeout"[^>]*min="5" max="3600"/);
  assert.match(html,/name="models\.recognizer\.max_tokens"[^>]*min="128" max="32768"/);
  assert.deepEqual(modelInputLimits({model:'deepseek-flash',base_url:'https://custom.example/v1'}),{minTokens:128,maxTokens:32768,maxTimeout:600});
});
test('editing provider details updates browser validation without changing the entered budget',()=>{
  const controls={model:{value:'deepseek-flash'},base_url:{value:'https://api.deepseek.com/v1'},max_tokens:{value:'393216'},timeout:{value:'3600'}};
  const form={dataset:{section:'models'},querySelector:selector=>controls[/models\.main\.([a-z_]+)/.exec(selector)[1]]};
  const target={name:'models.main.base_url',form};handleAdminModelInput({target});
  assert.equal(controls.max_tokens.max,'393216');assert.equal(controls.max_tokens.min,'1');assert.equal(controls.timeout.max,'3600');
  controls.base_url.value='https://custom.example/v1';handleAdminModelInput({target});
  assert.equal(controls.max_tokens.max,'32768');assert.equal(controls.timeout.max,'600');assert.equal(controls.max_tokens.value,'393216');
});
test('refresh reads overview and a health check only happens on explicit action',async()=>{
  const value=state(),calls=[];const ctx={api:async path=>{calls.push(path);return path==='/admin/overview'?value.adminOverview:path==='/config'?value.config:path==='/users'?{items:[user]}:{status:'connected',checked_at:1};},render(){},toast(){}};
  await handleAdminAction('admin-refresh',{},value,ctx);
  assert.deepEqual(calls,['/config','/users','/admin/overview']);
  await handleAdminAction('admin-data-health',{},value,ctx);
  assert.equal(calls.at(-1),'/admin/data-health');assert.equal(value.adminDataHealth.status,'connected');
});
test('saving model configuration omits blank secret and preserves explicit switches',async()=>{
  const value=state(),calls=[],button={disabled:false};const controls=[{name:'models.main.api_key',type:'password',value:''},{name:'models.main.model',type:'text',value:'updated-model'},{name:'models.main.trust_env',type:'checkbox',checked:false},{name:'models.main.input_modalities',type:'select-one',value:'text,image'}];
  const form={id:'config-form',dataset:{section:'models'},querySelector:()=>button,querySelectorAll:()=>controls};
  await handleAdminSubmit({target:form},value,{api:async(path,options)=>{calls.push({path,...options});return value.config;},render(){},toast(){}});
  assert.deepEqual(calls[0].body,{models:{main:{model:'updated-model',trust_env:false,input_modalities:['text','image']}}});
});

test('unified PNG optical credentials stay separate from the scientific data worker',()=>{
 const value=state();value.adminTab='data';value.config.unified_data={enabled:true,base_url:'https://example.ngrok-free.dev',api_key_configured:true,timeout_seconds:120,max_concurrency:2};
 const html=renderAdmin(value,icon);
 for(const key of ['enabled','base_url','api_key','timeout_seconds','max_concurrency'])assert.ok(html.includes(`name="unified_data.${key}"`));
 assert.match(html,/name="unified_data\.timeout_seconds"[^>]*min="30" max="120"/);assert.match(html,/name="unified_data\.max_concurrency"[^>]*min="1" max="2"/);assert.match(html,/name="data\.base_url"/);assert.match(html,/年度版次不代替实际拍摄日期/);
});

test('admin health separates scientific and PNG reachability without promising provider coverage',()=>{
 const value=state();value.config.unified_data={enabled:true};value.adminDataHealth={status:'partial',services:{science:{status:'unavailable',message:'科学服务暂未连通'},unified:{status:'connected',message:'PNG 服务可达',source_count:6}}};
 const html=renderAdmin(value,icon);assert.match(html,/多光谱与 SAR 数据服务/);assert.match(html,/高清与时序 PNG 服务/);assert.match(html,/科学服务暂未连通/);assert.match(html,/PNG 服务可达/);assert.match(html,/6 项目录记录；覆盖和取图能力逐次核验/);
});
