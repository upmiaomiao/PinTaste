import {escapeHTML as esc,readableText,sourceLabel,businessText,renderMarkdown,safeURL,sourceObservationLedger,evidenceTime,EVIDENCE_GROUP_LABELS} from './lib.mjs';
import {reviewText,readingText,renderReadableAnswer} from './answer-layout.mjs';

export function taskResults(task={}) { const pkg=task.package||task.result||task.report||{};return task.task_results||pkg.task_results||{}; }
const asList=value=>Array.isArray(value)?value:[];
const knownStatuses={needs_more_data:'尚需补证',qualitative_only:'仅作定性判读',qualitative_comparison_ready:'具备定性比较条件',evidence_available:'已形成任务证据'};

function conclusionParagraphs(value) {
 if(typeof value!=='string')return [];
 const standalone=line=>{const plain=line.replace(/^\*\*(.*?)\*\*$/,'$1').trim();return /^#{1,6}\s+/.test(plain)||/^(?:∞\s*)?无隅(?:\s*AI(?:平台)?|见微(?:遥感(?:Harness系统)?)?)?\s*[：:]?$/i.test(plain)||/^(?:本次核查结论|综合筛查判断|综合判读结论|最终结论|判读结论|核查结论)\s*[：:]?$/.test(plain)||/^(?:模型|模型署名|分析模型|主模型|判读模型|复核模型|生成模型|署名|作者)\s*[：:]\s*[^。！？\n]{1,100}$/.test(plain)||/^(?:Gemini|DeepSeek|无隅见微)[\s\dA-Za-z.v-]*(?:[·｜|—-]\s*)?(?:模型|判读|复核|场景与误报复核|场景复核|判读结果|复核结果)?\s*[：:]?$/i.test(plain);};
 let fenced=false;
 return readableText(value).replace(/\r\n?/g,'\n').split(/\n\s*\n/).map(block=>block.split('\n').filter(line=>{if(/^\s*```/.test(line)){fenced=!fenced;return false;}return !fenced&&line.trim()&&!standalone(line.trim());}).map(line=>line.trim()).join(' ').replace(/\*\*([^*\n]+)\*\*/g,'$1').trim()).filter(Boolean);
}

/** A concise authored conclusion wins; historical prose is never cut mid-sentence. */
export function consumerConclusion(task={}) {
 const pkg=task.package||task.result||task.report||{},result=taskResults(task);
 // A server-projected task conclusion is authoritative over an old nested copy.
 for(const entry of [task.consumer_conclusion,pkg.consumer_conclusion,result.consumer_conclusion]){
  const text=conclusionParagraphs(typeof entry==='string'?entry:entry?.text).join(' ');
  if(text)return reviewText(businessText(text,task.images||[]));
 }
 for(const legacy of [task.summary,pkg.narrative?.summary]){
  const paragraph=conclusionParagraphs(legacy)[0];
  if(paragraph)return reviewText(businessText(paragraph,task.images||[]));
 }
 return '';
}
export function temporalConditions(evidence={},images=[]) {
 if(!evidence.required)return '';
 const required=Number.isInteger(evidence.required_distinct_dates)?evidence.required_distinct_dates:null,observed=Number.isInteger(evidence.observed_distinct_dates)?evidence.observed_distinct_dates:null;
 const missing=[];
 if(required!=null&&observed!=null&&observed<required)missing.push(`需要至少 ${required} 个不同的实际拍摄日期，目前可用 ${observed} 个。`);
 if(evidence.registration_verified!==true)missing.push('同点空间对应尚未核实，当前不能把位置偏移解释为变化。');
 const unknown=asList(evidence.date_unknown_asset_ids).map(id=>images.findIndex(image=>image.id===id)+1).filter(n=>n>0);
 if(unknown.length)missing.push(unknown.map(n=>'图'+n).join('、')+'的实际拍摄日期或日期适用范围尚未核实。');
 if(!evidence.ready&&!missing.length)missing.push(readableText(evidence.reason)||'实际时相或空间可比性尚需补证。');
 return `<section class="temporal-conditions ${evidence.ready?'ready':'incomplete'}" aria-label="时序比较条件"><header><strong>时序比较条件</strong><span>${evidence.ready?'具备定性比较条件':'暂不能确认变化'}</span></header>${missing.length?`<ul>${missing.map(value=>`<li>${esc(value)}</li>`).join('')}</ul>`:`<p>已记录 ${observed??'若干'} 个不同实际时相及配准声明，仍需结合逐期可见证据复核。</p>`}<p class="task-result-note">年度版次、图源组名和获取时间均不代替实际拍摄日期。</p></section>`;
}
export function renderTaskResults(task={},icon=()=> '') {
 const pkg=task.package||task.result||task.report||{};
 const current=taskResults(task), projected=task.consumer_conclusion?.presentation||current.consumer_conclusion?.presentation||pkg.consumer_conclusion?.presentation||current.presentation;
 if(projected?.enabled)return renderTaskPresentation(task,projected,icon);
 // Research and information answers keep their full authored response in the
 // conversation. The compact screening conclusion is not their output type.
 if(task.task?.search_only||pkg.task?.search_only||task.research_result||pkg.research_result||
    [task.fusion,pkg.fusion].some(fusion=>fusion?.screening_applicable===false||fusion?.level==='not_applicable'))return '';
 const result=taskResults(task),rows=Array.isArray(result.results)?result.results.filter(row=>row&&typeof row==='object'):[];
 const conclusion=consumerConclusion(task);
 const images=task.images||[],business=value=>reviewText(businessText(value,images)),id=String(task.id||'task');
 const temporal=rows.find(row=>row.temporal_evidence?.required)?.temporal_evidence;
 const timeline=Array.isArray(result.timeline)?result.timeline:[];
 if(!conclusion&&!rows.length&&!timeline.length)return '';
 const records=rows.map(row=>{const alternatives=asList(row.alternatives).filter(value=>typeof value==='string'),steps=asList(row.next_steps).filter(value=>typeof value==='string'),evidence=asList(row.evidence).filter(value=>typeof value==='string');return `<section class="task-specific-result" data-result-task="${esc(row.task_type||'')}"><header><h3>${esc(row.heading||row.label||'任务结论')}</h3><span class="result-state">${esc(knownStatuses[row.status]||'证据待复核')}</span></header>${row.conclusion?`<p class="task-result-conclusion">${esc(business(row.conclusion))}</p>`:''}${evidence.length?`<ul>${evidence.map(value=>`<li>${esc(business(value))}</li>`).join('')}</ul>`:''}${alternatives.length?`<p><strong>需排除的解释</strong></p><ul>${alternatives.map(value=>`<li>${esc(business(value))}</li>`).join('')}</ul>`:''}${steps.length?`<p><strong>核查建议</strong></p><ul>${steps.map(value=>`<li>${esc(business(value))}</li>`).join('')}</ul>`:''}</section>`;}).join('');
 const dates=timeline.length?`<section class="task-timeline"><h3>逐期观测与日期记录</h3><div class="table-wrap"><table><thead><tr><th>影像</th><th>实际拍摄日期</th><th>提供方日期或版次</th><th>本图状态</th></tr></thead><tbody>${timeline.filter(row=>row&&typeof row==='object').map(row=>{const providerDates=asList(row.reported_actual_dates).map(String),providerLabel=providerDates.length?providerDates.join('、')+'（'+(row.date_scope||'适用范围未核实')+'）':row.product_year?String(row.product_year)+' 版；不是拍摄日期':row.period_label||'未记录';return `<tr><td>${esc([row.figure,sourceLabel(row.source)].filter(Boolean).join(' · '))}</td><td>${esc(row.actual_capture_date||'尚未核实')}</td><td>${esc(providerLabel)}</td><td>${esc(({positive:'保留疑点',negative:'本图未检出',unknown:'暂不可判',background_only:'背景参考'})[row.decision]||'暂不可判')}</td></tr>`;}).join('')}</tbody></table></div></section>`:'';
 return `<section class="task-result-panel consumer-conclusion-panel" aria-label="本次核查结论"><header class="task-result-heading"><h2>${icon('scan')}本次核查结论</h2></header>${conclusion?`<p class="consumer-conclusion">${esc(conclusion)}</p>`:''}${rows.length||timeline.length?`<details class="supporting-details task-result-records" data-ui-state="task-result-records-${esc(id)}"><summary>判读依据与核查记录${icon('chevron')}</summary><div class="task-result-records-body">${temporal?temporalConditions(temporal,images):''}${records}${dates}</div></details>`:''}</section>`;
}

/** Four investigation paradigms and ad-hoc answers have their own body structure. */

const FIGURE_LINE=/^图\s*(\d+)\s*[：:]\s*([\s\S]*)$/;
const CATEGORY_LINE=/标注区域的初筛候选类别为([^。；;.]+)[。.]?/;
/** Lines such as “图1：…” are regrouped per figure so each image sits beside its own conclusion. */
function groupFigureLines(sections){
 const map=new Map();
 const rest=sections.map(section=>{
  const keep=[];
  for(const value of asList(section.paragraphs)){
   const match=typeof value==='string'?FIGURE_LINE.exec(value.trim()):null;
   if(!match){keep.push(value);continue;}
   const entry=map.get(+match[1])||{n:+match[1],notes:[],category:''};
   const text=match[2].replace(/^同图复核[：:]\s*/,'').trim();
   const category=/^标注区域的初筛候选类别为(.+?)[。.]?$/.exec(text);
   if(category)entry.category=category[1];else if(text&&!entry.notes.includes(text))entry.notes.push(text);
   map.set(entry.n,entry);
  }
  return {...section,paragraphs:keep};
 }).filter(section=>section.paragraphs.length||(asList(section.columns).length&&asList(section.records).length));
 return {rest,figures:[...map.values()].sort((a,b)=>a.n-b.n)};
}
/** Columns follow the image count (6 → 3×2, 8 → 4×2) and still collapse on narrow screens. */
export function galleryColumns(count){return count<=3?Math.max(1,count):count===4?2:count<=6||count===9?3:4;}
/**
 * One gallery replaces the former “逐图结论” and “逐源观测记录”: each image carries
 * its own conclusion, candidate type and collapsible acquisition facts.
 */
export function evidenceGallery(task={},figures=[],icon=()=> ''){
 const images=task.images||[],business=value=>reviewText(businessText(value,images));
 const tidy=value=>business(value).replace(/^同图复核[：:]\s*/,'').replace(/[。.]\s*[；;]\s*/g,'。').trim();
 const byFigure=new Map(figures.map(figure=>[figure.n,figure]));
 const ledger=sourceObservationLedger(task);
 const rows=ledger.filter(row=>row.figure>0);
 for(const figure of figures)if(!rows.some(row=>row.figure===figure.n))rows.push({figure:figure.n,source:images[figure.n-1]?.source||'',group:'',key:'figure-'+figure.n,parameter_rows:[]});
 rows.sort((a,b)=>a.figure-b.figure);
 rows.push(...ledger.filter(row=>!(row.figure>0)));
 if(!rows.length)return '';
 const cards=rows.map(row=>{
  const n=row.figure,image=n?images[n-1]||{}:{},url=n?safeURL(image.annotated_url||image.url||image.image_url):'';
  const figure=byFigure.get(n)||{notes:[],category:''};
  let observation=row.observation&&!/尚无可公开的观测摘要/.test(row.observation)?tidy(row.observation):'';
  const category=figure.category||CATEGORY_LINE.exec(observation)?.[1]||'';
  observation=observation.replace(CATEGORY_LINE,'').replace(/[；;。\s]+$/,'');
  const notes=figure.notes.length?figure.notes.map(business):observation?[observation+'。']:[];
  const thumb=url?`<button type="button" class="evi-thumb" data-action="image" data-index="${n-1}" aria-label="放大查看图${n}"><img src="${esc(url)}" loading="lazy" alt="图${n}"><span>${icon('expand')}</span></button>`:`<div class="evi-thumb evi-thumb-empty">${icon('layers')}<small>无影像预览</small></div>`;
  const meta=row.retrieved_at!==undefined?[['拍摄时间',evidenceTime(row.acquisition_time)+(!row.acquisition_time&&row.period_label?`（资料期间：${row.period_label}，非精确拍摄日期）`:'')],['获取时间',evidenceTime(row.retrieved_at)],...(row.registered_at?[['本机登记',evidenceTime(row.registered_at)]]:[]),['原生分辨率',row.resolution+(row.resolution!=='未知'?' 米':'')],...row.parameter_rows,['用途',row.role],['位置校验',row.spatial]]:[];
  const caution=row.parameter_rows.some(([key])=>key.startsWith('配准误差'))?'<p class="evi-caution">配准误差记录不等同于绝对定位精度。</p>':'';
  return `<article class="evi-card"><div class="evi-media">${thumb}${n?`<span class="evi-no">图${n}</span>`:''}</div><div class="evi-body"><header><strong>${esc(sourceLabel(row.source)||'影像')}</strong>${row.group?`<span class="evi-tag">${esc(EVIDENCE_GROUP_LABELS[row.group]||'影像来源')}</span>`:''}</header>${category?`<p class="evi-badge">候选类型<b>${esc(business(category))}</b></p>`:''}<div class="evi-text">${notes.map(note=>`<p>${esc(note)}</p>`).join('')||'<p class="evi-muted">本图暂无单独结论，见上方综合结论。</p>'}</div>${meta.length?`<details class="evi-meta"><summary>${icon('chevron')}时间、分辨率与位置校验</summary><dl>${meta.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>${caution}</details>`:''}</div></article>`;
 }).join('');
 return `<section class="evi-cards" aria-label="依据影像与逐图结论"><h3>${icon('layers')}依据影像与逐图结论<span>${rows.length}</span></h3><div class="evi-grid" data-cols="${galleryColumns(rows.length)}" style="--evi-cols:${galleryColumns(rows.length)}">${cards}</div></section>`;
}
const isKeyPoint=section=>!(asList(section.columns).length&&asList(section.records).length)&&asList(section.paragraphs).filter(value=>typeof value==='string').join('').length<=160;

export function renderTaskPresentation(task, presentation, icon=()=> '') {
 const mode=presentation.workflow_mode||'site_investigation', images=task.images||[], contextual=mode==='contextual_qa';
 const lead=contextual?(presentation.lead||consumerConclusion(task)):consumerConclusion(task)||presentation.lead||'';
 const grouped=groupFigureLines(asList(presentation.sections));
 // Short findings belong to the conclusion itself; only tables and long notes stay folded.
 const keyPoints=contextual?[]:grouped.rest.filter(isKeyPoint),folded=contextual?grouped.rest:grouped.rest.filter(section=>!isKeyPoint(section));
 const points=keyPoints.length?`<dl class="result-keypoints">${keyPoints.map(section=>`<div><dt>${esc(reviewText(section.heading||'补充说明'))}</dt><dd>${asList(section.paragraphs).filter(value=>typeof value==='string').map(value=>esc(reviewText(businessText(value,images)))).join('<br>')}</dd></div>`).join('')}</dl>`:'';
 const sections=folded.map((section,index)=>{
  const paragraphs=asList(section.paragraphs).filter(value=>typeof value==='string');
  const columns=asList(section.columns),records=asList(section.records).filter(Array.isArray);
  const table=columns.length&&records.length?`<div class="table-wrap"><table><thead><tr>${columns.map(value=>`<th>${esc(reviewText(String(value)))}</th>`).join('')}</tr></thead><tbody>${records.map(row=>`<tr>${row.map(value=>`<td>${esc(reviewText(businessText(String(value??''),images)))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'';
  return `<section class="task-specific-result" data-result-section="${esc(section.id||String(index))}"><h3>${esc(reviewText(section.heading||'补充说明'))}</h3>${paragraphs.map(value=>`<div class="markdown-body">${renderMarkdown(readingText(businessText(value,images)))}</div>`).join('')}${table}</section>`;
 }).join('');
 const gallery=contextual&&!grouped.figures.length?'':evidenceGallery(task,grouped.figures,icon);
 const detailLabel=({site_investigation:'点位证据、候选类型与范围',complaint_investigation:'举报对照、覆盖与盲区',environmental_risk:'风险归因、复核与标准对照',temporal_change:'时间锚点、逐期证据与缺口'})[mode]||'任务依据与分析';
 const longAnswer=contextual && (asList(presentation.sections).length>3 || asList(presentation.sections).reduce((length,section)=>length+asList(section.paragraphs).join('').length,0)>620);
 const body=contextual&&!longAnswer?sections:sections?`<details class="supporting-details task-result-records" data-ui-state="task-result-records-${esc(task.id||'task')}"><summary>${longAnswer?'详细依据与核查清单':detailLabel}${icon('chevron')}</summary><div class="task-result-records-body">${sections}</div></details>`:'';
 return `<section class="task-result-panel consumer-conclusion-panel paradigm-output" data-workflow-mode="${esc(mode)}" aria-label="${esc(reviewText(presentation.heading||'任务答复'))}"><header class="task-result-heading"><h2>${icon(mode==='environmental_risk'?'shield':mode==='temporal_change'?'compare':contextual?'chat':'scan')}${esc(reviewText(presentation.heading||'任务答复'))}</h2>${presentation.risk_label?`<span class="result-state">环境风险：${esc(presentation.risk_label)}</span>`:''}${modelBadge(presentation.answer_model)}</header>${thinkingBlock(presentation.reasoning,task.id,icon)}${lead?`<div class="consumer-conclusion">${renderReadableAnswer(businessText(lead,images),{id:'lead-'+(task.id||'task'),detailLabel:'展开结论依据',limit:230})}</div>`:''}${points}${gallery}${body}</section>`;
}

/** Which model wrote this answer; only shown when 慎思 answered. */
export function modelBadge(model) {
 return model==='shensi'?'<span class="answer-model-badge" title="本轮由无隅慎思结合证据、地点背景与资料作答">无隅慎思</span>':'';
}

/** The model's own thinking, folded by default; it is reasoning, not evidence. */
export function thinkingBlock(reasoning,id,icon=()=> '') {
 const value=typeof reasoning==='string'?reasoning.trim():'';
 if(!value)return '';
 return `<details class="answer-thinking" data-ui-state="answer-thinking-${esc(id||'task')}"><summary>${icon('spark')}慎思的思考过程<small>模型推理，不是已核实的证据</small>${icon('chevron')}</summary><div class="answer-thinking-body">${value.split(/\n{2,}/).map(part=>`<p>${esc(part).replace(/\n/g,'<br>')}</p>`).join('')}</div></details>`;
}
