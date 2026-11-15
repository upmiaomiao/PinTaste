import test from 'node:test';
import assert from 'node:assert/strict';
import {workEntries,renderWorkEntry,renderProcessingBlock} from './worklog.mjs';

test('public work record uses actual results without stale started spinners',()=>{
 const rows=workEntries({activity_entries:[{id:'1',title:'正在取图',status:'completed',superseded_by:'2'},{id:'2',title:'已取得腾讯影像',summary:'级别19；日期未核实',status:'completed'}]});
 assert.equal(rows.length,1);assert.match(renderWorkEntry(rows[0]),/日期未核实/);assert.doesNotMatch(renderWorkEntry(rows[0]),/spin/);
 assert.deepEqual(workEntries({workflow_stages:[{id:'imagery',status:'running'}]}),[]);
});
test('progress treats model and document descriptions as untrusted text',()=>{
 const html=renderWorkEntry({title:'<img onerror=x>',summary:'<script>secret</script>',status:'completed'});
 assert.doesNotMatch(html,/<img|<script>/);assert.match(html,/&lt;script&gt;/);
});
test('consumer review naming stays uniform while the actual audit value remains intact',()=>{
 const entry={id:'review-1',title:'我再核对一下这个疑点',summary:'我仍不能确定它的性质。',source:'arcgis',status:'unknown',facts:{model:'gemini-3.8-flash'}};
 const html=renderWorkEntry(entry);
 assert.match(html,/<strong>我再核对一下这个疑点<\/strong>/);assert.match(html,/待核实/);
 assert.match(html,/<details[^>]*>[\s\S]*独立复核[\s\S]*<\/details>/);
 assert.doesNotMatch(html,/gemini|DeepSeek/i);
 assert.equal(entry.facts.model,'gemini-3.8-flash');
});

test('processing stays above the answer with the same disclosure identity after completion',()=>{
 const task={id:'stable',activity_entries:[{id:'real',step:'acquisition',title:'我拿到了影像',summary:'我已取得影像，拍摄日期待核实。',status:'completed'}]};
 const live=renderProcessingBlock({...task,status:'running'}),done=renderProcessingBlock({...task,status:'succeeded'});
 for(const html of [live,done]){assert.match(html,/data-ui-state="worklog-stable"/);assert.match(html,/data-workflow-position="before-answer" open/);assert.match(html,/见微处理流程/);assert.match(html,/<strong>遥感影像获取<\/strong>/);assert.match(html,/我已取得影像，拍摄日期待核实/);}
 assert.doesNotMatch(done,/worklog-stable-done|spin/);
});

test('terminal tasks keep stale starts unknown without spinner or claimed success',()=>{
 const rows=workEntries({status:'succeeded',activity_entries:[{id:'stale',step:'acquisition',title:'我正在请求影像',summary:'我正在请求世纪影像',status:'running',source:'century'}]});
 assert.equal(rows[0].status,'unknown');assert.match(rows[0].summary,/没有记录完成结果/);
 const html=renderWorkEntry(rows[0]);assert.doesNotMatch(html,/spin|正在请求|已完成/);assert.match(html,/待核实/);
});

test('failure details retain numeric code and named current optical source',()=>{
 const html=renderWorkEntry({id:'failed',step:'acquisition',title:'影像获取',status:'failed',source:'gaofen2026',summary:'图源服务响应超时',facts:{error_code:'504'}});
 assert.match(html,/2026 高清/);assert.match(html,/错误代码/);assert.match(html,/504/);assert.match(html,/未完成/);
});

test('coverage and history plans show bounded scalar facts without a raw payload',()=>{
 const task={id:'plans',status:'running',activity_entries:[
  {id:'grid',step:'coverage_plan',title:'我划分了范围',summary:'计划4窗20级，只是工作范围，不是村界。尚未取图。',source:'gaofen2026',status:'completed',facts:{planned_windows:4,requested_zoom:20,window_pixels:2048,working_bbox:[104,30,105,31]}},
  {id:'dates',step:'temporal_catalog',title:'我查到了历史日期',summary:'本次目录16个日期，不等于已经分析。',status:'completed',source:'history246',facts:{catalog_dates:16}}]};
 const rows=workEntries(task),grid=renderWorkEntry(rows[0]),dates=renderWorkEntry(rows[1]);
 assert.match(grid,/周边范围与窗口规划/);assert.match(grid,/计划窗口/);assert.match(grid,/请求级别/);
 assert.match(grid,/不是村界/);assert.doesNotMatch(grid,/working_bbox|104,30,105/);
 assert.match(dates,/历史日期查询/);assert.match(dates,/目录日期数/);assert.match(dates,/16/);
});
