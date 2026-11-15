import test from 'node:test';
import assert from 'node:assert/strict';
import {validateScanBBox,scanGrid,renderScanCoverage,createComplaintController} from './complaints.mjs';
const icon=()=>'';
const plan={status:'ready',region_confirmed:true,region_bbox:[104.05,30.66,104.06,30.67],required_windows:1,max_windows:16,area_m2_approx:1000000,windows:[{window_id:'r0001c0001',bbox:[104.05,30.66,104.06,30.67],center:[104.055,30.665]}]};

test('vague complaint cannot become a confirmed county footprint',()=>{
 assert.throws(()=>validateScanBBox('某县周边'),/WGS84/);
 assert.throws(()=>validateScanBBox([104,31,103,32]),/WGS84/);
 assert.deepEqual(validateScanBBox('104.05, 30.66, 104.06, 30.67'),[104.05,30.66,104.06,30.67]);
});

test('coverage keeps downloaded, reviewed and date-qualified footprints separate and exposes blind spots',()=>{
 const html=renderScanCoverage({scan_id:'s',execution_status:'running',plan,items:[{id:'t',status:'succeeded'}],summary:{conclusion:'仍有盲区，不能给出全区未发现结论',planned_coverage:1,downloaded_requested_coverage:.5,visual_reviewed_coverage:0,time_qualified_coverage:0,planned_windows:1,visual_reviewed_windows:0,time_qualified_windows:0,missing_visual_window_ids:['r0001c0001'],time_unqualified_window_ids:['r0001c0001']}},'s',icon);
 assert.match(html,/审阅盲区/);assert.match(html,/时相待补/);assert.match(html,/下载完成不等于已经审阅/);
 assert.match(html,/停止整片区域核查/);
 assert.match(scanGrid(plan),/仅作范围与窗口示意/);assert.equal(scanGrid({region_bbox:['bad']}),'');
});

test('the controller no longer carries a form: no open/submit/changed entry points remain',()=>{
 const controller=createComplaintController({state:{},api:async()=>({}),toast(){},loadLists:async()=>{},openTask:async()=>{}});
 assert.deepEqual(Object.keys(controller).sort(),['action','planConversation','refresh','running','startConversation']);
});

test('conversation plan never starts paid work; start is a separate explicit call',async()=>{
 const calls=[],opened=[],state={draft:{},tasks:[{id:'t'}],user:{id:'u'}};
 const controller=createComplaintController({state,api:async(path,options)=>{calls.push({path,options});if(path==='/complaints/plan')return {scan_id:'s',plan};if(path.endsWith('/start'))return {items:[{id:'t'}]};return {scan_id:'s',execution_status:'running'};},toast(){},loadLists:async()=>{},openTask:async id=>opened.push(id)});
 const planned=await controller.planConversation({objective:'核查',location_id:'x',parent_task_id:'p',complaint_scan:{text:'举报'}});
 assert.equal(planned.scan_id,'s');assert.deepEqual(calls.map(row=>row.path),['/complaints/plan']);assert.equal(opened.length,0);
 assert.deepEqual(Object.keys(calls[0].options.body.task).sort(),['objective']);assert.equal(calls[0].options.body.scan.text,'举报');
 await controller.startConversation('s');
 assert.deepEqual(calls.map(row=>row.path),['/complaints/plan','/complaints/scans/s/start','/complaints/scans/s']);assert.deepEqual(opened,['t']);
 await assert.rejects(controller.startConversation(''),/核查计划不存在/);
});

test('stop-all targets only the persisted scan and the scan stays tracked until it finishes',async()=>{
 const state={scanCache:{s:{execution_status:'running'}},user:{id:'u'}},calls=[];
 const controller=createComplaintController({state,api:async path=>{calls.push(path);return {scan_id:'s',execution_status:'completed'};},toast(){},loadLists:async()=>calls.push('lists'),openTask:async()=>{},refreshTask:async()=>calls.push('refresh-window')});
 assert.equal(controller.running('s'),true);
 await controller.action('scan-cancel',{dataset:{scanId:'s'},disabled:false});
 assert.deepEqual(calls,['/complaints/scans/s/cancel','/complaints/scans/s','lists','refresh-window']);
 assert.equal(controller.running('s'),false);
 assert.equal(await controller.action('scan-open',{}),false);
});
