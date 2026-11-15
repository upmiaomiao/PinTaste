import {escapeHTML as esc,readableText,sourceLabel,formatDate} from './lib.mjs';
import {reviewText} from './answer-layout.mjs';

const STEP_TITLES={task_planning:'任务理解与规划',archive_inspection:'压缩包解析',document_reading:'资料与点位读取',catalog:'图源能力核验',selection:'按任务选星',coverage_plan:'周边范围与窗口规划',acquisition:'遥感影像获取',quality:'影像质量与空间核验',image_recognition:'疑似堆存识别',image_assessment:'逐图证据核查',optical_false_positive_review:'场景与误报复核',science_query:'科学影像目录查询',science_quality_probe:'科学影像质量筛查',science_subset:'科学数据窗口提取',research_optical_context:'高分场景补充',science_analysis:'科学观测证据分析',adaptive_review:'疑点追加复核',visual_cross_review:'跨图分歧核查',spatial_registration_refresh:'影像配准核查',policy:'政策与公开资料检索',report_synthesis:'证据综合与结论整理',synthesis:'证据综合与结论整理',report_export:'报告生成',temporal_catalog:'历史日期查询',temporal_acquisition:'时序影像获取',temporal_comparison:'时序变化核查',temporal_analysis:'时序变化核查',batch_planning:'批量点位任务规划',batch_site:'逐点核查'};
export function workEntryTitle(entry={}) { return STEP_TITLES[entry.step]||readableText(entry.title||entry.label||'')||'处理记录'; }

/** Render only events that the server actually recorded; never invent a workflow. */
export function workEntries(task={}) {
  const events=task.activity_entries||(Array.isArray(task.activity)?task.activity:task.activity?.entries)||[];
  const terminal=!!task.status&&!['planning','queued','pending','running'].includes(task.status);
  return events.filter(e=>e&&typeof e==='object'&&!e.superseded_by).map((e,i)=>{const stale=terminal&&['running','started','queued','pending'].includes(e.status);return {
    id:String(e.id||i),step:e.step||'',status:stale?(['cancelled','interrupted'].includes(task.status)?'cancelled':'unknown'):(e.status||'completed'),
    title:workEntryTitle(e),summary:stale?'这项操作没有记录完成结果，不能计为已获取或已分析；需要核对来源记录。':readableText(e.summary||e.message||''),
    time:e.time||e.created_at||'',source:e.source,facts:e.facts||{},
  };}).filter(e=>e.title||e.summary).filter((e,i,rows)=>i===0||e.title!==rows[i-1].title||e.summary!==rows[i-1].summary||e.status!==rows[i-1].status);
}

export function renderWorkEntry(entry,icon=()=>'',index=0) {
  const pending=['running','started','queued'].includes(entry.status);
  const failed=['failed','error','unavailable'].includes(entry.status);
  const label=pending?'进行中':entry.status==='partial'?'部分完成':failed?'未完成':['cancelled','interrupted'].includes(entry.status)?'已停止':['unknown','background_only'].includes(entry.status)?'待核实':'已完成';
  const facts=entry.facts||{},details=[entry.source?['影像来源',sourceLabel(entry.source)]:null,...Object.entries({model:'分析服务',planned_windows:'计划窗口',downloaded_windows:'已取窗口',catalog_dates:'目录日期数',window_pixels:'窗口边长像素',scene_count:'候选景数',zoom:'取图级别',requested_zoom:'请求级别',source_label:'图源标识',error_code:'错误代码',excluded_candidate_count:'已排除候选',uncertain_candidate_count:'待核实候选',candidate_reference:'候选标记',elapsed_seconds:'本步耗时（秒）',timeout_seconds:'最长等待（秒）'}).filter(([key])=>['string','number'].includes(typeof facts[key])).map(([key,label])=>[label,facts[key]])].filter(Boolean);
  return `<section class="work-entry ${pending?'working':failed?'work-warning':''}" data-event-id="${esc(entry.id||index)}"><div class="work-entry-marker">${pending?'<span class="spin"></span>':icon(failed?'alert':'check')}</div><div class="work-entry-content"><header><strong>${esc(reviewText(entry.title||'我正在核对这一步的结果'))}</strong><small>${esc(label)}${entry.time?' · '+formatDate(entry.time,true):''}</small></header>${entry.summary?`<p>${esc(reviewText(entry.summary))}</p>`:''}${details.length?`<details class="work-entry-details" data-ui-state="event-${esc(entry.id||index)}"><summary>查看来源与记录</summary><dl>${details.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(reviewText(String(value)))}</dd>`).join('')}</dl></details>`:''}</div></section>`;
}

export function localMaterialLog(rows=[],icon=()=> '') {
  if(!rows.length)return '';
  return `<section class="material-log" aria-label="材料处理记录">${rows.map((e,i)=>renderWorkEntry(e,icon,i)).join('')}</section>`;
}

/** Stable identity/order keeps the process above the answer as task state changes. */
export function renderProcessingBlock(task={},icon=()=>'',local=[],progress='') {
  const entries=workEntries(task),running=['planning','queued','pending','running'].includes(task.status);
  if(!entries.length&&!running&&!local.length)return '';
  const id=esc(String(task.id||'current'));
  return `<details class="expert-worklog" data-ui-state="worklog-${id}" data-workflow-position="before-answer" open><summary><span>${icon('layers')}见微处理流程${entries.length?' · '+entries.length+' 条':''}</span><small>${running?'进行中':'查看每一步与返回结果'}</small>${icon('chevron')}</summary><div class="worklog-entries">${localMaterialLog(local,icon)}${entries.map((entry,i)=>renderWorkEntry(entry,icon,i)).join('')}${!entries.length&&running?`<div class="work-entry"><span class="spin"></span><p>我先梳理你的问题，再核对需要哪些证据。<button class="text-button" data-action="modify-task">停止并修改</button></p></div>`:''}</div></details>`;
}
