import test from 'node:test';
import assert from 'node:assert/strict';
import {portalBase,fontSize} from './ui-config.mjs';
import {workbookBatchModal,workbookBatchPayload,applyWorkbookProposal} from './workbook.mjs';
import {renderAdmin,userSettingsModal,handleAdminAction,handleAdminSubmit} from './admin.mjs';
const icon=()=>'';
test('root and independent /jianweirs deployment resolve consistently',()=>{
 assert.equal(portalBase('/'),'');assert.equal(portalBase('/jianweirs/'),'/jianweirs');assert.equal(portalBase('/jianweirs'),'/jianweirs');assert.equal(portalBase('/jianweirs-extra/'),'');
 assert.equal(fontSize(18),18);assert.equal(fontSize('invalid'),16);assert.equal(fontSize(100),16);
});
test('batch selection never preselects the sheet and requires known coordinate basis',()=>{
 const html=workbookBatchModal({id:'sheet',records:[{record_id:'a',site_id:'<script>bad</script>'},{record_id:'b',site_id:'B'}]},{});
 assert.equal((html.match(/name="record_id"/g)||[]).length,2);assert.doesNotMatch(html,/name="record_id"[^>]*checked/);assert.match(html,/name="coordinate_system" required/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 const fields=new FormData();fields.set('objective','核查变化');fields.append('record_id','a');
 assert.throws(()=>workbookBatchPayload(fields),/坐标依据/);fields.set('coordinate_system','wgs84');fields.append('record_id','a');fields.set('task_type','stockpile_change');
 assert.deepEqual(workbookBatchPayload(fields,['century']),{record_ids:['a'],objective:'核查变化',coordinate_system:'wgs84',task_types:['stockpile_change'],search_policy:false,selected_sources:['century']});
 fields.set('date_start','2025-01-01');fields.set('date_end','2024-01-01');assert.throws(()=>workbookBatchPayload(fields),/起始日期/);
});
test('shared account administration only offers independent portal preferences and quotas',()=>{
 const value={user:{id:'a',username:'admin',role:'admin'},auth:{mode:'wuyu'},adminTab:'users',config:{},users:[{id:'u',username:'member',active:true,role:'user'}]};
 const html=renderAdmin(value,icon);assert.match(html,/字体与额度/);assert.match(html,/账号来自无隅 AI 平台/);assert.doesNotMatch(html,/data-action="user-add"|name="password"/);
 const form=userSettingsModal(value.users[0],{font_size:16,imagery_quota:0,qa_quota:null,usage:{imagery:2,qa:3}},icon);
 assert.match(form,/name="imagery_quota"[^>]*value="0"/);assert.match(form,/name="qa_quota"[^>]*value=""/);assert.match(form,/本月已用：影像 2 次 · 问答 3 次/);assert.doesNotMatch(form,/name="password"/);
});

test('proposal fills only its point subset and preserves the multi-task meaning without submitting',()=>{
 const proposal={id:'increase',label:'面积增加记录复核',point_count:2,objective:'核查表内面积增加记录',record_ids:['1','3'],task_types:['stockpile_extent','stockpile_change'],date_start:'2021-01-01'};
 const boxes=['1','2','3'].map(value=>({value,checked:false})),form={elements:Object.fromEntries(['objective','task_type','date_start','date_end','coordinate_system'].map(key=>[key,{value:''}])),dataset:{},querySelectorAll(selector){return selector.includes('record_id')?boxes:[];}};
 applyWorkbookProposal(form,proposal);
 assert.deepEqual(boxes.map(x=>x.checked),[true,false,true]);assert.equal(form.elements.objective.value,proposal.objective);assert.equal(form.elements.coordinate_system.value,'');assert.equal(form.elements.date_start.value,'2021-01-01');
 const fields=new FormData();fields.set('objective',form.elements.objective.value);fields.set('coordinate_system','wgs84');for(const box of boxes)if(box.checked)fields.append('record_id',box.value);
 const payload=workbookBatchPayload(fields,[],JSON.parse(form.dataset.proposalTaskTypes));assert.deepEqual(payload.task_types,proposal.task_types);assert.equal(payload.search_policy,false);
 const html=workbookBatchModal({id:'sheet',records:[],proposals:[proposal]},{});assert.match(html,/确认提交前不会取图/);assert.match(html,/面积增加记录复核 · 2 个点位/);
});
test('two-user settings dialogs read individual quotas and changing another user does not resize this session',async()=>{
 const users=[{id:'u1',username:'one',role:'admin'},{id:'u2',username:'two',role:'user'}],settings={u1:{font_size:14,imagery_quota:10,qa_quota:20,usage:{imagery:1,qa:2}},u2:{font_size:18,imagery_quota:30,qa_quota:40,usage:{imagery:3,qa:4}}};
 const state={user:users[0],users,preferences:{font_size:14},config:{appearance:{default_font_size:14}}},calls=[],modals=[];
 const ctx={api:async(path,options)=>{calls.push({path,options});return settings[path.split('/')[2]];},showModal:html=>modals.push(html),render(){},toast(){},closeModal(){}};
 await handleAdminAction('user-settings',{dataset:{id:'u1'}},state,ctx);await handleAdminAction('user-settings',{dataset:{id:'u2'}},state,ctx);
 assert.deepEqual(calls.map(x=>x.path),['/users/u1/settings','/users/u2/settings']);assert.match(modals[0],/影像 1 次 · 问答 2 次/);assert.match(modals[1],/影像 3 次 · 问答 4 次/);
 const saved=globalThis.FormData;globalThis.FormData=class{constructor(form){this.fields=form.fields;}get(key){return this.fields[key]??null;}};
 try{const form={id:'user-settings-form',dataset:{id:'u2'},fields:{font_size:'18',imagery_quota:'0',qa_quota:''},querySelector:()=>({disabled:false})};await handleAdminSubmit({target:form},state,ctx);assert.deepEqual(calls.at(-1).options.body,{font_size:18,imagery_quota:0,qa_quota:null});assert.equal(state.preferences.font_size,14);}finally{globalThis.FormData=saved;}
});
