import test from 'node:test';
import assert from 'node:assert/strict';
import {evidenceGroup,evidenceTime,evidenceParameters,sourceObservationLedger,evidencePresentation,evidenceSelection,spatialEvidenceText,scientificWindows} from './lib.mjs';
import {workflowStageTitle,screeningVerdictVisible,scientificMonthlyRows,scientificReadSummary,renderScienceAvailability} from './lib.mjs';
import { escapeHTML, parseCenter, readableText, safeURL, stageState, taskPayload, statusLabel, selectWorkbookPoint, businessText, sourceLabel, candidateImageReferences, renderMarkdown, evidencePanelWidth, imageZoom, imageFit, scientificStatus, activityProgress } from './lib.mjs';
test('model content cannot inject HTML or expose thinking blocks', () => { assert.equal(escapeHTML('<img onerror="bad">'), '&lt;img onerror=&quot;bad&quot;&gt;'); assert.equal(readableText('<thinking>private reasoning</thinking>事实摘要'), '事实摘要'); assert.equal(readableText('摘要<think>truncated'), '摘要'); });
test('evidence links reject script and local-file protocols', () => { assert.equal(safeURL('javascript:alert(1)'), ''); assert.equal(safeURL('file:///secret'), ''); assert.equal(safeURL('/portal/api/tasks/id/images/a.png'), '/portal/api/tasks/id/images/a.png'); });
test('coordinate input accepts Chinese and whitespace delimiters and rejects out-of-range values', () => { assert.deepEqual(parseCenter('104.504329，30.507354'), [104.504329,30.507354]); assert.deepEqual(parseCenter('104.5 30.5'), [104.5,30.5]); assert.equal(parseCenter('200,90'), null); });
test('progress only marks stages from actual stage records', () => { assert.equal(stageState({status:'running',stages:[]}, 0), 'waiting'); assert.equal(stageState({stages:[{step:'recognition',status:'running'}]}, 1), 'done'); assert.equal(stageState({stages:[{step:'recognition',status:'failed'}]}, 2), 'failed'); });
test('pending future stages do not claim execution or completion', () => { const task={workflow_stages:[{id:'task_analysis',status:'running'},{id:'source_selection',status:'pending'},{id:'recognition_review',status:'pending'},{id:'synthesis',status:'pending'}]}; assert.equal(stageState(task,0),'running'); assert.equal(stageState(task,1),'waiting'); assert.equal(stageState(task,2),'waiting'); assert.equal(statusLabel('planning'),'任务规划中'); });
test('natural-language planning preserves automatic task inference and administrator defaults', () => { const value=taskPayload({objective:'核查清理进度',task_types:[],search_policy:true,files:[]}); assert.deepEqual(value,{objective:'核查清理进度'}); });
test('explicit scope and search choice are retained without exposing model controls', () => { const value=taskPayload({objective:'查固废',task_types:['cleanup_progress'],search_policy:false,search_touched:true,consult_shensi:false,files:[]}); assert.deepEqual(value,{objective:'查固废',task_types:['cleanup_progress'],search_policy:false}); });
test('follow-up inherits parent resources when new resources are absent', () => { assert.deepEqual(taskPayload({objective:'进一步分析周边',files:[]},'task-1'),{objective:'进一步分析周边',parent_task_id:'task-1'}); });
test('coordinate-bearing scientific requests leave instrument selection to the planner', () => {
  const result=taskPayload({objective:'104.097927 30.914227 查2026年1月Sentinel数据',files:[]});
  assert.deepEqual(result.center,[104.097927,30.914227]); assert.equal(result.acquire_optical,undefined);
  assert.equal(result.research,undefined);
  const explicit=taskPayload({objective:'查询目录',research:{mode:'availability',sources:['sentinel2']},files:[]});
  assert.deepEqual(explicit.research,{mode:'availability',sources:['sentinel2']});
});
test('uploaded image kind, worksheet selection, and new coordinates are passed precisely', () => { const value=taskPayload({objective:'核查记录',center:'104.5,30.5',files:[{id:'i1',kind:'image',name:'map'},{id:'i1',asset_id:'i1'},{id:'d1',kind:'document',name:'points.xlsx'}],workbook_record:{site_id:'CD-1',record_id:'S01-R00002'}}); assert.deepEqual(value.asset_ids,['i1']); assert.deepEqual(value.document_ids,['d1']); assert.deepEqual(value.center,[104.5,30.5]); assert.equal(value.acquire_optical,undefined); assert.match(value.prompt,/S01-R00002/); assert.throws(()=>taskPayload({objective:'查',center:'200,90'}),/经纬度/); });
test('reasoning filtering applies to object and array summaries too',()=>{assert.equal(readableText({summary:'<think>private</think>可见摘要'}),'可见摘要'); assert.equal(readableText(['证据一','<thinking>private</thinking>证据二']),'证据一；证据二');});
test('switching workbook points replaces previous point images and generated scope without removing independent uploads', () => {
  const a = {record_id:'row-A',site_id:'A',coordinates:{numeric_valid:true,coordinate_system:'wgs84',longitude:104,latitude:30}};
  // Matches the first Chengdu workbook record's actual coordinate/provenance shape.
  const b = {record_id:'S01-R00003',site_id:'DY-GTFW-10',coordinates:{longitude:103.740338,latitude:30.534669,numeric_valid:true,coordinate_system:'unknown',coordinate_system_status:'unknown',axis_mapping_status:'inferred_duplicate_longitude_header',longitude_cell:'G3',latitude_cell:'H3',verified:false}};
  const initial = {objective:'',center:'',files:[{id:'workbook',kind:'document'},{id:'independent',kind:'image'}]};
  const first = selectWorkbookPoint(initial,'workbook',a,[{id:'a1'},{id:'a2'},{id:'a3'},{id:'a4'}]);
  const second = selectWorkbookPoint(first,'workbook',b,[{id:'b1'},{id:'b2'},{id:'b3'},{id:'b4'}]);
  const body = taskPayload(second);
  assert.deepEqual(body.asset_ids,['independent','b1','b2','b3','b4']);
  assert.deepEqual(body.document_ids,['workbook']);
  assert.match(body.objective,/点位 DY-GTFW-10/); assert.doesNotMatch(body.objective,/点位 A/);
  assert.match(body.prompt,/S01-R00003/); assert.deepEqual(body.center,[103.740338,30.534669]);
  assert.equal(body.coordinate_system,'unknown'); assert.equal(body.acquire_optical,undefined);
  assert.equal(taskPayload(first).coordinate_system,'wgs84');
  for (const coordinate_system of ['gcj02','bd09']) {
    const other=selectWorkbookPoint(second,'workbook',{record_id:'other',site_id:'other',coordinates:{...b.coordinates,coordinate_system}},[]);
    assert.equal(other.center,''); assert.equal(taskPayload(other).center,undefined);
  }
  assert.deepEqual(initial.files.map(file=>file.id),['workbook','independent']);
});
test('workbook selection preserves a custom objective and explicitly entered coordinates', () => {
  const value=selectWorkbookPoint({objective:'只核查清理进度',center:'103, 29',files:[]},'book',{record_id:'r',site_id:'B',coordinates:{numeric_valid:true,coordinate_system:'unknown',longitude:104,latitude:30}},[{id:'b'}]);
  assert.equal(value.objective,'只核查清理进度'); assert.equal(value.center,'103, 29');
  assert.deepEqual(taskPayload(value).asset_ids,['b']);
  assert.deepEqual(taskPayload(value).center,[103,29]); assert.equal(taskPayload(value).coordinate_system,undefined);
});
test('business evidence removes coordinate conversions and substitutes stable figure numbers without dropping uncertainty',()=>{
  const id='0123456789abcdef0123456789abcdef';
  const value=businessText(`影像 ${id} 在铁路西侧[200,180,370,280]存在疑似线索（normalized_1000，像素窗[204,184,379,287]），需排除正常物料堆场。`,[{id}]);
  assert.match(value,/图1/);assert.match(value,/需排除正常物料堆场/);assert.doesNotMatch(value,/normalized_1000|204|01234567/);
  assert.equal(businessText('原生模型标注候选 R1，模型类别为建筑垃圾；像素框由整图 normalized_1000 坐标换算。'),'标注区域的初筛候选类别为建筑垃圾。');
});
test('source names and selection terminology are presented as business evidence needs',()=>{
  assert.equal(sourceLabel('sentinel1-rtc'),'Sentinel-1 雷达');assert.equal(sourceLabel('arcgis'),'ArcGIS 世界影像');
  const value=businessText('auto：任务数据合同要求optical；按已核验能力与可见特征适配排序。SAR用于辅助。');
  assert.match(value,/自动选源/);assert.match(value,/光学影像/);assert.match(value,/雷达辅助影像/);assert.doesNotMatch(value,/合同|\boptical\b|\bauto\b/i);
});
test('candidate image references resolve actual observations and preserve report image order',()=>{
  assert.deepEqual(candidateImageReferences({evidence_indices:[1,0,1,9]},{observations:[{asset_id:'a'},{asset_id:'b'}]},[{id:'a'},{id:'b'}]),[{number:2,label:'图2',anchor:'#evidence-image-2'},{number:1,label:'图1',anchor:'#evidence-image-1'}]);
  assert.deepEqual(candidateImageReferences({evidence_indices:[0]},{observations:[{asset_id:'missing'}]},[{id:'a'}]),[]);
});
test('Markdown renders headings, nested lists, quotes, tables and citation links',()=>{
  const result=renderMarkdown('# 研判结果\n\n**疑似线索**，需复核。\n\n1. 第一项\n   - 子项\n2. 第二项\n\n> 引用资料\n\n| 图像 | 说明 |\n| --- | --- |\n| 图1 | 需核查 |\n\n[官方来源](https://example.test/policy)');
  assert.match(result,/<h2>研判结果<\/h2>/);assert.match(result,/<strong>疑似线索<\/strong>/);assert.match(result,/<ol><li>第一项<ul><li>子项<\/li><\/ul><\/li><li>第二项/);assert.match(result,/<blockquote>/);assert.match(result,/<table>/);assert.match(result,/<td>图1<\/td>/);assert.match(result,/href="https:\/\/example.test\/policy"/);
});
test('standalone bold headings remain separate from the following paragraph without a blank line',()=>{
  assert.equal(renderMarkdown('前一段。\n**同点位多来源证据与可比性**\n本次实际取得多来源影像。'),'<p>前一段。</p>\n<h3>同点位多来源证据与可比性</h3>\n<p>本次实际取得多来源影像。</p>');
});
test('Markdown escapes all HTML and rejects executable links without exposing reasoning',()=>{
  const result=renderMarkdown('<thinking>private reasoning</thinking>\n# 标题<script>alert(1)</script>\n\n[执行](javascript:alert(1)) [数据](data:text/html;base64,PHNjcmlwdD4=) ![图](file:///private)\n\n| x | y |\n| --- | --- |\n| <img src=x onerror=alert(1)> | **文本** |\n\n```html\n<svg onload=alert(1)>\n```');
  assert.doesNotMatch(result,/<script|<img|<svg|href="(?:javascript|data|file):|private reasoning/);assert.match(result,/&lt;script&gt;/);assert.match(result,/&lt;img/);assert.match(result,/<pre><code>&lt;svg/);
});
test('Markdown cannot inject attributes through link labels or URLs',()=>{
  const result=renderMarkdown('[<img onerror="bad">](https://example.test/a?x="bad") [x](javascript&#58;alert)');
  assert.doesNotMatch(result,/<img|<[^>]+ onerror=|href="javascript/);assert.match(result,/&lt;img/);assert.match(result,/&quot;/);assert.match(result,/javascript&amp;#58;/);
});
test('evidence width and image zoom remain bounded across screen sizes',()=>{
  assert.equal(evidencePanelWidth(9999,1200),720);assert.equal(evidencePanelWidth(-5,1200),280);assert.equal(evidencePanelWidth(380,500),300);assert.equal(imageZoom(100),16);assert.equal(imageZoom(.001),.05);assert.equal(imageFit(1000,1000,532,532),.5);assert.equal(imageFit(200,100,1000,700),1);
});
test('science states distinguish downloaded windows, empty catalog and failed retrieval',()=>{
  assert.equal(scientificStatus({status:'catalog_only',subset:{status:'science_window_downloaded'}}),'已取得科学数据窗口');
  assert.equal(scientificStatus({status:'no_results'}),'本次未检索到候选场景');
  assert.equal(scientificStatus({status:'catalog_only',scene_count:2}),'已检索到 2 景候选数据');
  assert.match(scientificStatus({status:'catalog_only',subset:{status:'unavailable'}}),/未能取得或读取/);
});
test('actual running activity selects the latest scene search, window retrieval or image analysis',()=>{
  const query={step:'science_query',status:'running',source:'sentinel2'};
  assert.equal(activityProgress({status:'running',activity:[query]}),'正在检索 Sentinel-2 多光谱 场景');
  assert.equal(activityProgress({status:'running',activity:[query,{step:'science_subset',status:'running',source:'sentinel1-rtc'}]}),'正在获取 Sentinel-1 雷达 数据窗口');
  assert.equal(activityProgress({status:'running',activity:[{step:'image_recognition',status:'running'}]}),'正在逐图分析');
});
test('completed or failed activity does not remain labeled as running',()=>{
  const activity=[{step:'science_query',status:'running',source:'sentinel2'},{step:'science_query',status:'completed',source:'sentinel2'}];
  assert.equal(activityProgress({status:'running',activity}),'');
  assert.equal(activityProgress({status:'running',activity:[{step:'science_subset',status:'running',source:'sentinel2'},{step:'science_subset',status:'failed',source:'sentinel2'}]}),'');
  assert.equal(activityProgress({status:'succeeded',activity:[{step:'image_recognition',status:'running'}]}),'');
  assert.equal(activityProgress({status:'running',activity:[{step:'other_step',status:'running'}]}),'');
});
test('scientific research uses backend stages and never prepends a waste-screening verdict',()=>{
  assert.equal(workflowStageTitle({workflow_stages:[{id:'recognition_review',label:'逐景质量检查与科学分析'}]},2),'逐景质量检查与科学分析');
  assert.equal(screeningVerdictVisible({screening_applicable:false,judgment:'证据不足'}),false);
  assert.equal(screeningVerdictVisible({level:'not_applicable',judgment:'证据不足'}),false);
  assert.equal(screeningVerdictVisible({level:'medium'}),true);
  assert.equal(statusLabel('research_completed'),'研究完成');
  assert.match(activityProgress({status:'running',activity:[{step:'research_replan',status:'running'}]}),/下一步检索/);
});
test('monthly archive facts distinguish catalog dates from unread or obscured image windows',()=>{
  const item={status:'catalog_found_no_usable_window',scene_count:4,
    monthly_availability:[{period:'2026-01',scene_count:4,acquisition_dates:['2026-01-10','not-a-date'],catalog_complete:true,quality_checked_scenes:2,usable_windows:0},
      {period:'<script>bad</script>',scene_count:0,acquisition_dates:[],catalog_complete:false}],
    quality_probes:[{status:'read_failed'},{status:'obscured_or_unusable'}],
    download_attempts:[{status:'read_failed'},{status:'quality_rejected'}]};
  assert.match(scientificStatus(item),/4 景.*未取得/);
  const rows=scientificMonthlyRows(item);
  assert.deepEqual(rows[0].dates,['2026-01-10']); assert.equal(rows[1].status,'目录未查全');
  assert.match(scientificReadSummary(item),/读取失败 1 次/); assert.match(scientificReadSummary(item),/云影或无效像元拦截 1 景/);
  const html=renderScienceAvailability(item);
  assert.match(html,/2026-01-10/); assert.match(html,/目录完整不代表每景均已分析/);
  assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/<script>/);
});
test('source ledger separates capture, retrieval and registration times and preserves unknown native detail',()=>{
  const task={images:[{id:'base',created_at:1792210000,metadata:{source:'arcgis',period_label:'2026.03—2026.04'}}],evidence_summary:[{asset_id:'base',source:'arcgis',group:'optical',acquisition_time:null,retrieved_at:'2026-10-17T09:00:00Z',native_resolution_m:null,parameters:{bbox:[104,30,105,31],access_token:'never-display',raw_thinking:'never-display'},observation:'<think>private</think>可见浅色区域，需复核',role:'local_context',spatial_registration:{status:'unverified',reason:'未完成配准'},selected:true}]};
  const row=sourceObservationLedger(task)[0];
  assert.equal(row.group,'basemap');assert.equal(row.resolution,'未知');assert.equal(row.acquisition_time,null);assert.equal(row.period_label,'2026.03—2026.04');
  assert.equal(row.registered_at,1792210000);assert.equal(row.retrieved_at,'2026-10-17T09:00:00Z');
  assert.equal(row.observation,'可见浅色区域，需复核');assert.match(row.spatial,/未核验空间对应/);assert.equal(row.figure,1);
  assert.equal(evidenceTime(null),'未知');assert.equal(evidenceTime('2026-03-11'),'2026-03-11');assert.match(evidenceTime(row.retrieved_at),/2026\/10\/17 17:00:00/);
  assert.doesNotMatch(JSON.stringify(row),/never-display|private/);
});
test('scientific parameter display only includes actual allowlisted bands and polarizations',()=>{
  const rows=evidenceParameters({bands:['B04','B03','B02'],polarizations:['VV','VH'],instrument_mode:'IW',relative_orbit:128,registration_error_m:2.1,scope:'背景核查',Authorization:'secret',file_url:'https://signed.test?sig=secret'});
  assert.deepEqual(rows.find(([key])=>key==='实际波段'),['实际波段','B04 / B03 / B02']);
  assert.deepEqual(rows.find(([key])=>key==='极化'),['极化','VV / VH']);assert.match(JSON.stringify(rows),/配准误差记录/);assert.doesNotMatch(JSON.stringify(rows),/secret|file_url|Authorization/);
  assert.deepEqual(evidenceParameters(null),[]);assert.equal(evidenceGroup({source:'sentinel1-rtc'}),'sar');assert.equal(evidenceGroup({source:'landsat'}),'multispectral');
  assert.match(spatialEvidenceText({status:'failed',reason:'位置不一致'}),/空间核验未通过/);
});
test('new presentation selects known key images without renumbering or inventing absent evidence',()=>{
  const images=[{id:'base',url:'/assets/base',metadata:{source:'arcgis'}},{id:'s2',url:'/assets/s2',metadata:{source:'sentinel2'}},{id:'sar',url:'/assets/sar',metadata:{source:'sentinel1-rtc'}}];
  const task={images,evidence_presentation:{mode:'comparison',primary_asset_ids:['s2','base','absent'],supporting_asset_ids:['sar'],reason:'比较同一位置证据',comparison_groups:[{title:'底图与多光谱',asset_ids:['base','s2'],reason:'空间位置复核'},{title:'多光谱与雷达',asset_ids:['s2','sar'],reason:'科学辅助'}]}};
  const first=evidenceSelection(task);assert.deepEqual(first.visible.map(row=>row.index),[0,1]);assert.deepEqual(first.additional.map(row=>row.image.id),['sar']);
  const second=evidenceSelection(task,'key',1);assert.deepEqual(second.visible.map(row=>row.index),[1,2]);assert.equal(second.activeComparison.title,'多光谱与雷达');
  assert.deepEqual(evidencePresentation(task).primary_asset_ids,['s2','base']);assert.deepEqual(evidenceSelection(task,'sar').visible.map(row=>row.image.id),['sar']);
  assert.equal(evidenceSelection(task,'missing').tab,'key');
});
test('single evidence mode stays single and legacy tasks avoid a wall of every image',()=>{
  const images=Array.from({length:6},(_,index)=>({id:'im'+index,url:'/assets/'+index,role:index===2?'target_crop':'full_scene',metadata:{source:index%2?'bing':'arcgis'}}));
  assert.deepEqual(evidenceSelection({images}).visible.map(row=>row.image.id),['im2']);assert.equal(evidenceSelection({images}).additional.length,5);
  assert.equal(evidenceSelection({images,evidence_presentation:{mode:'single',primary_asset_ids:['im1','im2']}}).visible.length,1);
  assert.deepEqual(evidenceSelection({images:[]}).visible,[]);
  assert.equal(evidenceSelection({images:[{id:'bad',url:'javascript:alert(1)'}]}).visible.length,0);
});
test('catalog-only or failed scientific sources stay acquisition facts instead of invented observations',()=>{
  const task={images:[],scientific_evidence:[{source:'sentinel2',status:'catalog_only',scene_count:2,time_range:'2026-01-01/2026-04-30'},{source:'sentinel1-rtc',status:'failed'}]};
  const rows=sourceObservationLedger(task);assert.equal(rows.length,2);assert.equal(rows[0].figure,0);assert.equal(rows[0].acquisition_time,null);assert.match(rows[0].observation,/2 景候选/);assert.match(rows[1].observation,/未完成/);
  assert.doesNotMatch(JSON.stringify(rows),/没有固废|不存在堆存|无云|unknown传感器/);
});

test('partial source summaries retain newly ready images without inventing an analysis',()=>{
  const task={images:[{id:'first',url:'/assets/first',metadata:{source:'arcgis'}},{id:'new',url:'/assets/new',metadata:{source:'sentinel2'}}],evidence_summary:[{asset_id:'first',observation:'已有可公开摘要'}]};
  const rows=sourceObservationLedger(task);assert.equal(rows.length,2);assert.equal(rows[1].figure,2);assert.equal(rows[1].group,'multispectral');assert.match(rows[1].observation,/尚无可公开/);assert.equal(rows[1].acquisition_time,null);assert.deepEqual(rows[1].parameter_rows,[['实际波段','未知']]);
});

test('an explicit comparison group works without a redundant primary list and legacy selected flags stay bounded',()=>{
  const images=Array.from({length:5},(_,index)=>({id:'i'+index,url:'/assets/'+index,metadata:{source:'arcgis'}}));
  const task={images,evidence_presentation:{mode:'comparison',comparison_groups:[{title:'前后对照',asset_ids:['i3','i1']}]}};
  assert.equal(evidencePresentation(task).mode,'comparison');assert.deepEqual(evidenceSelection(task).visible.map(row=>row.index),[3,1]);
  assert.equal(evidenceSelection({images,evidence_summary:images.map(image=>({asset_id:image.id,selected:true}))}).visible.length,2);
});

test('a new explicit empty primary does not promote unverified context images',()=>{
  const task={images:[{id:'unknown',url:'/assets/unknown',metadata:{source:'bing'}}],evidence_summary:[{asset_id:'unknown',spatial_status:'unknown',selected:false,registered_at:'2026-10-17T04:00:00Z'}],evidence_presentation:{mode:'single',primary_asset_ids:[],supporting_asset_ids:[],reason:'位置未核验'}};
  assert.deepEqual(evidenceSelection(task).visible,[]);assert.equal(evidenceSelection(task).additional.length,1);
  assert.equal(sourceObservationLedger(task)[0].registered_at,'2026-10-17T04:00:00Z');
});

test('quality probe progress and SAR sampling parameters use actual bounded fields',()=>{
  assert.match(activityProgress({status:'running',activity:[{step:'science_quality_probe',source:'sentinel2',status:'running'}]}),/点位云影与有效性/);
  assert.equal(activityProgress({status:'running',activity:[{step:'science_quality_probe',source:'sentinel2',status:'running'},{step:'science_quality_probe',source:'sentinel2',status:'unavailable'}]}),'');
  const rows=evidenceParameters({native_pixel_spacing_m:10,effective_resolution_m:20,input_units:'linear gamma0 power',display_units:'dB',registration_scope:'relative_at_this_aoi',dimensions:[128,128],api_key:'SECRET'});
  assert.deepEqual(rows.find(([key])=>key==='原生像元间距（米）'),['原生像元间距（米）','10']);assert.deepEqual(rows.find(([key])=>key==='有效空间分辨率（米）'),['有效空间分辨率（米）','20']);assert.match(JSON.stringify(rows),/dB/);assert.doesNotMatch(JSON.stringify(rows),/SECRET/);
});

test('scientific windows keep both dates and deduplicate the legacy first-window alias',()=>{
  const first={status:'science_window_downloaded',artifact:'science-first.tif'},second={status:'science_window_downloaded',artifact:'science-second.tif'};
  assert.equal(scientificWindows({subset:first,subsets:[first,second]}).length,2);
  assert.equal(scientificStatus({subset:{status:'unavailable'},subsets:[first,second]}),'已取得 2 个科学数据窗口');
});

test('scientific tabs prefer analysed key dates over earlier downloaded obscured scenes',()=>{
  const dates=['2025-11-02','2025-12-12','2026-01-08','2026-02-12'];
  const images=dates.map((date,index)=>({id:'scene-'+index,url:'/assets/'+index,metadata:{source:'sentinel2',acquisition_time:date}}));
  const task={images,evidence_presentation:{mode:'comparison',primary_asset_ids:[],supporting_asset_ids:['scene-2','scene-3'],
    comparison_groups:[{title:'清晰前后时相',asset_ids:['scene-2','scene-3']}]}};
  const result=evidenceSelection(task,'multispectral');
  assert.deepEqual(result.visible.map(row=>row.image.metadata.acquisition_time),['2026-01-08','2026-02-12']);
  assert.deepEqual(result.visible.map(row=>row.index),[2,3]);
  assert.deepEqual(result.additional.map(row=>row.index),[0,1]);
  task.evidence_presentation.comparison_groups=[];
  assert.deepEqual(evidenceSelection(task,'multispectral').visible.map(row=>row.index),[2,3]);
});

test('scientific comparison selections stay bounded to a pair and retain other dates',()=>{
  for(const [tab,source] of [['sar','sentinel1-rtc'],['hyperspectral','enmap']]){
    const images=Array.from({length:5},(_,i)=>({id:'science-'+i,url:'/assets/'+i,metadata:{source}}));
    const task={images,evidence_presentation:{mode:'comparison',primary_asset_ids:['science-4','science-3'],
      comparison_groups:[{title:'关键科学时相',asset_ids:['science-4','science-3','science-2','science-1']}]}};
    assert.deepEqual(evidenceSelection(task,tab).visible.map(row=>row.index),[4,3]);
    assert.equal(evidenceSelection(task,tab).additional.length,3);
    assert.equal(evidenceSelection(task,'key').visible.length,2);
  }
});

test('earlier schema 0.3 packages cannot promote explicitly ineligible images through primary or comparison ids',()=>{
  const images=[{id:'unknown',url:'/assets/unknown',metadata:{source:'bing'}},{id:'verified',url:'/assets/verified',metadata:{source:'arcgis'}}];
  const task={schema_version:'0.3.0',images,evidence_summary:[{asset_id:'unknown',same_point_eligible:false,selected:true},{asset_id:'verified',same_point_eligible:true}],evidence_presentation:{mode:'comparison',primary_asset_ids:['unknown','verified'],supporting_asset_ids:['unknown'],comparison_groups:[{title:'旧版错误对照',asset_ids:['unknown','verified']}]}};
  const selection=evidenceSelection(task);assert.deepEqual(selection.visible.map(row=>row.image.id),['verified']);assert.deepEqual(selection.presentation.comparison_groups,[]);assert.deepEqual(selection.presentation.supporting_asset_ids,[]);assert.equal(selection.additional[0].image.id,'unknown');
  assert.equal(sourceObservationLedger(task)[0].same_point_eligible,false);
  task.evidence_presentation.primary_asset_ids=['unknown'];assert.deepEqual(evidenceSelection(task).visible,[]);
  assert.deepEqual(evidenceSelection(task,'basemap').visible.map(row=>row.image.id),['unknown','verified']);
});
