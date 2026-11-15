import test from 'node:test';
import assert from 'node:assert/strict';
import {renderTaskResults} from './task-results.mjs';

function task(mode,heading,lead,sections=[],riskLabel=null){
 const presentation={enabled:true,workflow_mode:mode,heading,lead,sections,risk_label:riskLabel};
 return {consumer_conclusion:{text:lead,presentation},fusion:{level:'not_applicable',screening_applicable:false}};
}

test('risk renders final grade and a source pathway receptor table despite screening being inapplicable',()=>{
 const html=renderTaskResults(task('environmental_risk','环境风险结论','环境风险筛查等级：高。优先核查岸边排水。',[
  {id:'pathways',heading:'污染源 迁移路径与环境受体',paragraphs:[],columns:['潜在源','路径','受体'],records:[['露天堆存','地表径流','邻近水体']]},
  {id:'compliance',heading:'国家及地方管理标准对照',paragraphs:['仍缺现场排水资料。'],columns:[],records:[]}
 ],'高'));
 assert.match(html,/data-workflow-mode="environmental_risk"/);
 assert.match(html,/环境风险：高/);assert.match(html,/<td>地表径流<\/td>/);assert.match(html,/国家及地方管理标准对照/);
 assert.doesNotMatch(html,/本次核查结论|疑点位置与核查重点|项核查目标/);
 assert.match(html,/<summary>风险归因、复核与标准对照/);assert.doesNotMatch(html,/<details[^>]*\sopen(?:\s|=|>)/);
});

test('temporal output has time anchors and is not a generic screening card',()=>{
 const html=renderTaskResults(task('temporal_change','时序变化结论','最早于2025-02-18保留堆存线索。',[
  {id:'timeline',heading:'过去至最近的逐期证据',paragraphs:[],columns:['实际日期','可见状态'],records:[['2025-12-07','保留堆存线索']]}
 ]));
 assert.match(html,/时序变化结论/);assert.match(html,/过去至最近的逐期证据/);assert.match(html,/2025-12-07/);
 assert.doesNotMatch(html,/本次核查结论/);
});

test('contextual question keeps its adaptive sections and direct full answer',()=>{
 const html=renderTaskResults(task('contextual_qa','答复','先查排水。\n\n再核物料来源。',[
  {id:'answer-0',heading:'为什么先看排水',paragraphs:['临水环境需要核实实际迁移路径。'],columns:[],records:[]}
 ]));
 assert.match(html,/data-workflow-mode="contextual_qa"/);assert.match(html,/再核物料来源/);assert.match(html,/为什么先看排水/);
 assert.doesNotMatch(html,/本次核查结论|影像识别线索|疑点位置与核查重点/);
 assert.doesNotMatch(html,/<details/);
 assert.match(html,/<p>先查排水。<\/p>[\s\S]*<p>再核物料来源。<\/p>/);
});

test('task-specific headings, cells and markdown are escaped',()=>{
 const html=renderTaskResults(task('contextual_qa','答复 <script>bad</script>','<img src=x onerror=bad()>',[
  {id:'"><svg onload=bad()>',heading:'<svg onload=bad()>',paragraphs:['[链接](javascript:alert(1)) <script>bad</script>'],
   columns:['<img src=x>'],records:[['<iframe src=bad>']]}
 ]));
 assert.doesNotMatch(html,/<script|<img|<svg|<iframe|href=["']javascript:|\son\w+=["']/);
 assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;iframe/);
});

test('site output merges per-figure conclusions with source facts and folds short sections into the conclusion',()=>{
 const images=[{id:'a',source:'user_upload',url:'/a.png'},{id:'b',source:'user_upload',url:'/b.png'}];
 const base=task('site_investigation','点位核查结论','本次发现疑似固废堆存线索。',[
  {id:'focus',heading:'疑点位置与核查重点',paragraphs:['图内存在待核查风险点，地理位置与物料类型仍需核验'],columns:[],records:[]},
  {id:'figures',heading:'逐图结论',paragraphs:['图1：同图复核：红线区域位于场地边缘，判定为存疑。','图1：标注区域的初筛候选类别为建筑垃圾。','图2：细节不足以确认。'],columns:[],records:[]}
 ]);
 const html=renderTaskResults({...base,id:'site',images,evidence_summary:[{asset_id:'a',source:'user_upload',observation:'同图复核：红线区域位于场地边缘'}]});
 const visible=html.split('<details class="supporting-details')[0];
 assert.match(visible,/class="result-keypoints"[\s\S]*疑点位置与核查重点[\s\S]*地理位置与物料类型仍需核验/);
 assert.match(visible,/依据影像与逐图结论/);assert.equal((html.match(/class="evi-card"/g)||[]).length,2);
 assert.match(html,/候选类型<b>建筑垃圾<\/b>/);assert.match(html,/时间、分辨率与位置校验/);
 assert.doesNotMatch(html,/逐源观测记录|<summary>点位证据、候选类型与范围/);
});

test('gallery columns keep six and eight images in balanced rows',async()=>{
 const {galleryColumns}=await import('./task-results.mjs');
 assert.deepEqual([1,2,3,4,5,6,7,8,9,12].map(galleryColumns),[1,2,3,2,3,3,4,4,3,4]);
});
