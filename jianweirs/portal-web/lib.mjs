import {portalBase} from './ui-config.mjs';
/** Pure presentation helpers shared by the portal and its keyless tests. */
export const TASK_TYPES = [
  ['suspected_site_discovery', '疑似点位发现', '倾倒核查', 'scan'],
  ['waste_type_screening', '固废类型识别', '倾倒核查', 'layers'],
  ['stockpile_extent', '堆存范围判定', '持续堆存', 'frame'],
  ['stockpile_change', '堆存变化监测', '持续堆存', 'compare'],
  ['cleanup_progress', '清理进度核查', '清理整改', 'check'],
  ['residual_stockpile', '残留堆体识别', '清理整改', 'scan'],
  ['repeated_dumping', '重复倾倒排查', '后续复查', 'repeat'],
  ['site_tracking', '点位持续跟踪', '后续复查', 'pin'],
  ['environmental_context', '环境判读', '贯穿全程', 'leaf'],
  ['surrounding_relationship', '周边关系判读', '贯穿全程', 'network'],
];
export const STAGES = [
  { id: 'outline', title: '任务大纲', detail: '明确区域、目标与证据需求', steps: ['task_analysis', 'planning', 'outline', 'task_outline', 'catalog'] },
  { id: 'imagery', title: '选星与取图', detail: '选择可用图源，核对时相与质量', steps: ['source_selection', 'selection', 'acquisition', 'data', 'quality', 'fetching', 'imagery'] },
  { id: 'recognition', title: '见微识别与复核', detail: '专业识别、证据标注与追加核查', steps: ['recognition_review', 'recognition', 'recognizer', 'review', 'specialist_review', 'recheck'] },
  { id: 'report', title: '最终研判与资料', detail: '综合影像证据、政策和用户资料', steps: ['synthesis', 'policy', 'documents', 'report', 'summary', 'completed'] },
];
export const STATUS_LABELS = { cancelled:'已停止', needs_input:'请补充关键信息', planning:'任务规划中', planning_failed:'规划失败', complete:'分析完成', error:'执行失败', queued: '等待执行', pending: '等待执行', running: '分析中', succeeded: '分析完成', completed: '分析完成', failed: '执行失败', interrupted: '任务中断', needs_more_data: '需补充资料', needs_configuration: '需配置服务', draft_pending_human_review: '待人工复核', no_reviewable_finding: '未形成可审查疑点',research_completed:'研究完成' };
export const STEP_LABELS = { planning: '正在明确任务与证据需求', outline: '任务大纲已生成', catalog: '已读取可用数据目录', selection: '正在选择数据来源', acquisition: '正在获取影像', quality: '正在核验影像质量', recognition: '见微模型正在判读影像', review: '正在进行独立复核', specialist_review: '正在核对专业模型结果', policy: '正在检索适用政策与资料', report: '正在整理最终研判与报告', workbook: '正在关联工作簿记录与影像', data: '正在获取数据', documents: '正在核对用户资料' };
export function escapeHTML(value = '') { return String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]); }
export function safeURL(value) {
  if (typeof value !== 'string' || !value || value.includes('\\')) return '';
  try { const decoded=value.replace(/&#(?:x([0-9a-f]+)|(\d+));?/gi,(_,hex,decimal)=>String.fromCodePoint(Math.min(0x10ffff,parseInt(hex||decimal,hex?16:10)))).replace(/&colon;/gi,':'); const parsed = new URL(decoded, 'http://portal.local'); return ['http:', 'https:'].includes(parsed.protocol) ? (/^\/portal\//.test(value)?portalBase()+value:value) : ''; } catch { return ''; }
}
export function readableText(value = '') {
  if (Array.isArray(value)) value = value.map(item => readableText(item)).filter(Boolean).join('；');
  else if (typeof value === 'object' && value !== null) value = value.summary || value.assessment || value.conclusion || value.statement || '';
  return String(value).replace(/<thinking\b[^>]*>[\s\S]*?(?:<\/thinking\s*>|$)/gi, '').replace(/<think\b[^>]*>[\s\S]*?(?:<\/think\s*>|$)/gi, '').trim();
}
export function normaliseRecommendations(value,limit=100) {
  const plain=value=>{if(typeof value!=='string')return '';const text=readableText(value).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,'').trim();return /^(?:null|none|undefined|\[\]|\{\})$/i.test(text)||/^[\s\-—–•\d.、)）]*$/.test(text)?'':text.slice(0,2000);};
  const result=[];
  for(const item of Array.isArray(value)?value:[value]){
    let text='';
    if(item&&typeof item==='object'&&!Array.isArray(item)){
      const action=['action','recommendation','suggestion','text'].map(key=>plain(item[key])).find(Boolean)||'',reason=plain(item.reason);
      text=action+(action&&reason&&reason!==action?'；'+reason:'');
    }else text=plain(item);
    if(text&&!result.includes(text))result.push(text);
    if(result.length>=limit)break;
  }
  return result;
}
export function formatDate(value, full = false) {
  if (!value) return '时间未记录'; const date = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value);
  if (!Number.isFinite(date.valueOf())) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', ...(full ? { hour: '2-digit', minute: '2-digit' } : {}) }).format(date);
}
export function fileSize(bytes) { return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`; }
export function parseCenter(text) {
  const pair = String(text).trim().match(/(-?\d{1,3}(?:\.\d+)?)\s*[,，\s]\s*(-?\d{1,2}(?:\.\d+)?)/);
  if (!pair) return null; const values = [Number(pair[1]), Number(pair[2])];
  return Math.abs(values[0]) <= 180 && Math.abs(values[1]) <= 85 ? values : null;
}
export function normaliseList(data) { return Array.isArray(data) ? data : data?.items || data?.tasks || data?.users || []; }
export function taskTitle(task) { return task?.title || task?.objective || task?.task?.objective || task?.request?.objective || '未命名监管任务'; }
export function isRunning(task) { return ['planning', 'running', 'queued', 'pending'].includes(task?.status); }
export function stageState(task, index) {
  const rows = Array.isArray(task?.workflow_stages) ? task.workflow_stages : Array.isArray(task?.stages) ? task.stages : [];
  const matches = rows.filter(row => STAGES[index].steps.some(step => String(row.step || row.id || '').startsWith(step)));
  if (matches.some(row => ['failed', 'error'].includes(row.status))) return 'failed';
  if (matches.some(row => ['running', 'started'].includes(row.status))) return 'running';
  if (matches.some(row => ['completed', 'succeeded', 'done', 'skipped'].includes(row.status))) return 'done';
  if (rows.some(row => ['running','started','completed','succeeded','done'].includes(row.status) && STAGES.slice(index + 1).some(stage => stage.steps.some(step => String(row.step || row.id || '').startsWith(step))))) return 'done';
  return 'waiting';
}
export function statusLabel(status) { return status==='cancelled'?'已停止':STATUS_LABELS[status] || '状态待确认'; }
export function workflowStageTitle(task,index) {
  const rows=Array.isArray(task?.workflow_stages)?task.workflow_stages:Array.isArray(task?.stages)?task.stages:[];
  const row=rows.find(row=>STAGES[index]?.steps.includes(row.id||row.step));
  return readableText(row?.label||row?.title||STAGES[index]?.title||'处理记录');
}
export function screeningVerdictVisible(fusion) { return !!fusion&&fusion.screening_applicable!==false&&fusion.level!=='not_applicable'; }
export function textParagraphs(text) {
  return readableText(text).split(/\n\s*\n/).filter(Boolean).map(part => `<p>${escapeHTML(part).replace(/\n/g, '<br>')}</p>`).join('');
}
/** Render a small Markdown subset; HTML is always escaped and links are allowlisted. */
export function renderMarkdown(value, depth=0) {
  const source=readableText(value).replace(/\r\n?/g,'\n');
  if(depth>6)return `<p>${escapeHTML(source)}</p>`;
  function inline(text,level=0){
    if(level>5)return escapeHTML(text);
    let out='',index=0;
    while(index<text.length){
      if(text[index]==='\\'&&index+1<text.length){out+=escapeHTML(text[index+1]);index+=2;continue;}
      if(text[index]==='`'){const end=text.indexOf('`',index+1);if(end>index+1){out+='<code>'+escapeHTML(text.slice(index+1,end))+'</code>';index=end+1;continue;}}
      const strong=text.startsWith('**',index)?'**':text.startsWith('__',index)?'__':null;
      if(strong){const end=text.indexOf(strong,index+2);if(end>index+2){out+='<strong>'+inline(text.slice(index+2,end),level+1)+'</strong>';index=end+2;continue;}}
      if(text[index]==='['){const match=text.slice(index).match(/^\[([^\]\n]+)\]\(\s*(<?[^\s)>]+>?)\s*\)/);if(match){const url=safeURL(match[2].replace(/^<|>$/g,''));out+=url?`<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${inline(match[1],level+1)}</a>`:escapeHTML(match[0]);index+=match[0].length;continue;}}
      if(text[index]==='*'||text[index]==='_'){const marker=text[index],end=text.indexOf(marker,index+1);if(end>index+1){out+='<em>'+inline(text.slice(index+1,end),level+1)+'</em>';index=end+1;continue;}}
      out+=escapeHTML(text[index]);index++;
    }
    return out;
  }
  const rows=source.split('\n'),output=[];
  const listMatch=line=>line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
  const boldHeading=line=>line?.match(/^\s*(?:\*\*([^*\n]+)\*\*|__([^_\n]+)__)\s*$/);
  const tableCells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());
  const tableRule=line=>/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'');
  const special=index=>/^(\s*#{1,6}\s|\s*```|\s*>|\s*(?:---+|\*\*\*+)\s*$)/.test(rows[index]||'')||!!boldHeading(rows[index])||!!listMatch(rows[index]||'')||tableRule(rows[index+1]);
  function listBlock(start,indent){
    const first=listMatch(rows[start]),ordered=/\d/.test(first[2]),tag=ordered?'ol':'ul';
    let html=`<${tag}${ordered&&parseInt(first[2],10)!==1?` start="${parseInt(first[2],10)}"`:''}>`,index=start;
    while(index<rows.length){const item=listMatch(rows[index]);if(!item||item[1].length!==indent||/\d/.test(item[2])!==ordered)break;
      html+='<li>'+inline(item[3]);index++;
      while(index<rows.length){const child=listMatch(rows[index]);if(child&&child[1].length>indent){const result=listBlock(index,child[1].length);html+=result.html;index=result.index;}
        else if(rows[index].trim()&&!child&&/^\s+/.test(rows[index])&&!special(index)){html+=' '+inline(rows[index].trim());index++;}else break;}
      html+='</li>';
    }
    return {html:html+`</${tag}>`,index};
  }
  for(let index=0;index<rows.length;){const line=rows[index];if(!line.trim()){index++;continue;}
    if(/^\s*```/.test(line)){const code=[];index++;while(index<rows.length&&!/^\s*```/.test(rows[index]))code.push(rows[index++]);if(index<rows.length)index++;output.push('<pre><code>'+escapeHTML(code.join('\n'))+'</code></pre>');continue;}
    const heading=line.match(/^\s*(#{1,6})\s+(.+?)\s*#*$/);if(heading){const level=Math.min(heading[1].length+1,5);output.push(`<h${level}>${inline(heading[2])}</h${level}>`);index++;continue;}
    const bold=boldHeading(line);if(bold){output.push('<h3>'+inline(bold[1]||bold[2])+'</h3>');index++;continue;}
    if(/^\s*(?:---+|\*\*\*+)\s*$/.test(line)){output.push('<hr>');index++;continue;}
    if(/^\s*>/.test(line)){const quoted=[];while(index<rows.length&&/^\s*>/.test(rows[index]))quoted.push(rows[index++].replace(/^\s*>\s?/,''));output.push('<blockquote>'+renderMarkdown(quoted.join('\n'),depth+1)+'</blockquote>');continue;}
    if(listMatch(line)){const result=listBlock(index,listMatch(line)[1].length);output.push(result.html);index=result.index;continue;}
    if(tableRule(rows[index+1])){const headers=tableCells(line);index+=2;const body=[];while(index<rows.length&&rows[index].trim()&&rows[index].includes('|'))body.push(tableCells(rows[index++]));output.push('<div class="table-scroll"><table><thead><tr>'+headers.map(cell=>'<th>'+inline(cell)+'</th>').join('')+'</tr></thead><tbody>'+body.map(row=>'<tr>'+headers.map((_,i)=>'<td>'+inline(row[i]||'')+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>');continue;}
    const paragraph=[line];index++;while(index<rows.length&&rows[index].trim()&&!special(index))paragraph.push(rows[index++]);output.push('<p>'+inline(paragraph.join('\n'))+'</p>');
  }
  return output.join('\n');
}
export function evidencePanelWidth(value,available){const max=Math.max(280,Math.min(800,Math.round(available*.6)));return Math.max(280,Math.min(max,Number(value)||380));}
export function imageZoom(value){return Math.max(.05,Math.min(16,Number(value)||1));}
export function imageFit(width,height,viewWidth,viewHeight){return imageZoom(Math.min((viewWidth-32)/Math.max(1,width),(viewHeight-32)/Math.max(1,height),1));}
export function scientificWindows(item={}) {
  const windows=[...(Array.isArray(item.subsets)?item.subsets:[]),...(item.subset?[item.subset]:[])],seen=new Set();
  return windows.filter(window=>{if(!window||typeof window!=='object')return false;const key=window.artifact||window.preview_asset_id||window.scene_id||JSON.stringify(window);if(seen.has(key))return false;seen.add(key);return true;});
}
export function scientificStatus(item){
  const downloaded=scientificWindows(item).filter(window=>window.status==='science_window_downloaded').length;
  if(downloaded)return downloaded>1?`已取得 ${downloaded} 个科学数据窗口`:'已取得科学数据窗口';
  if(item?.subset?.status==='unavailable')return '本次科学数据窗口未能取得或读取';
  return {catalog_only:Number(item?.scene_count)>0?`已检索到 ${Number(item.scene_count)} 景候选数据`:'已完成目录检索，尚无可分析数据窗口',catalog_found_no_usable_window:`已检索到 ${Number(item?.scene_count)||0} 景；本次未取得可分析窗口`,analyzed:'已取得科学影像分析结果',downloaded_unanalyzed:'已取得数据窗口，科学分析尚未完成',query_unavailable:'本次目录请求未完整成功，不能解释为没有数据',unsupported_source:'当前服务未配置该科学图源',no_results:'本次未检索到候选场景',unavailable:'本次未取得可用科学数据',failed:'本次科学数据检索未完成',not_queried:'本次未执行科学数据检索'}[item?.status]||'科学数据获取情况待核实';
}
export function scientificMonthlyRows(item={}) {
  return (Array.isArray(item.monthly_availability)?item.monthly_availability:[]).slice(0,120).map(row=>({
    period:readableText(row.period||row.time_range||'时间未记录'),
    count:Number.isFinite(row.scene_count)?row.scene_count:null,
    dates:(Array.isArray(row.acquisition_dates)?row.acquisition_dates:[]).filter(value=>/^\d{4}-\d{2}-\d{2}$/.test(value)).slice(0,50),
    complete:row.catalog_complete===true,
    status:row.catalog_complete===true?(Number(row.scene_count)>0?'目录有景':'目录无景'):'目录未查全',
    checked:Number(row.quality_checked_scenes)||0,usable:Number(row.usable_windows)||0,
  }));
}
export function scientificReadSummary(item={}) {
  const attempts=Array.isArray(item.download_attempts)?item.download_attempts:[],probes=Array.isArray(item.quality_probes)?item.quality_probes:[],windows=scientificWindows(item);
  const parts=[];
  if(probes.length)parts.push(`已检查 ${probes.length} 景点位质量`);
  const downloaded=windows.filter(window=>window.status==='science_window_downloaded').length;
  if(downloaded)parts.push(`取得 ${downloaded} 个科学窗口`);
  const failed=attempts.filter(row=>row.status==='read_failed').length,obscured=attempts.filter(row=>row.status==='quality_rejected').length;
  if(failed)parts.push(`读取失败 ${failed} 次`);
  if(obscured)parts.push(`云影或无效像元拦截 ${obscured} 景`);
  if(item.download_status==='provider_authorization_required')parts.push('科学文件需要提供方授权');
  else if(item.download_status==='native_window_adapter_not_available')parts.push('该图源尚未接通科学窗口读取');
  return parts.join('；');
}
export function renderScienceAvailability(item={}) {
  const rows=scientificMonthlyRows(item),summary=scientificReadSummary(item);
  return (summary?`<p>${escapeHTML(summary)}。</p>`:'')+(rows.length?`<div class="science-monthly"><table><thead><tr><th>月份</th><th>目录景数</th><th>实际成像日期</th><th>检查／可用窗口</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escapeHTML(row.period)}</td><td>${row.count??'未记录'}${row.complete?'':`<small>（${escapeHTML(row.status)}）</small>`}</td><td>${escapeHTML(row.dates.slice(0,12).join('、')||'未记录')}${row.dates.length>12?`等 ${row.dates.length} 个日期`:''}</td><td>${row.checked}／${row.usable}</td></tr>`).join('')}</tbody></table><p>目录有景、点位质量和实际取得窗口分别记录；目录完整不代表每景均已分析。</p></div>`:'');
}
const SOURCE_LABELS = {gaofen2026:'2026 高清图源组',century:'世纪空间影像',jilin2024:'吉林一号 2024 版',siwei2025:'四维地球 2025 版',history246:'Google 历史时序影像',arcgis:'ArcGIS 世界影像',bing:'Bing 影像',mapquest:'MapQuest 影像',sentinel2:'Sentinel-2 多光谱',landsat:'Landsat 多光谱','sentinel1-rtc':'Sentinel-1 雷达','sentinel1-grd':'Sentinel-1 雷达','sentinel1-slc':'Sentinel-1 雷达',hls_s30:'HLS 多光谱',emit:'EMIT 高光谱',enmap:'EnMAP 高光谱',user_upload:'用户上传影像',user_workbook_embedded:'工作簿影像',optical:'光学影像',sar:'雷达辅助影像',multispectral:'多光谱影像',hyperspectral:'高光谱影像',amap:'高德影像',tencent:'腾讯影像',google:'Google 影像',mapbox:'Mapbox 影像',tianditu:'天地图影像',siwei:'四维影像',siwei_daily:'四维每日影像'};
export function sourceLabel(value) { const text=String(value||''); return SOURCE_LABELS[text.toLowerCase()]||(/^[a-f0-9]{32,64}$/i.test(text)?'来源待核验':text||'影像来源'); }
export function activityProgress(task){
  if(!isRunning(task))return '';
  const latest=new Map();
  (Array.isArray(task.activity)?task.activity:[]).forEach((row,index)=>{if(['science_query','science_quality_probe','science_subset','science_analysis','research_replan','image_recognition'].includes(row.step))latest.set(row.step+':'+(row.source||''),{...row,index});});
  const row=[...latest.values()].filter(item=>['running','started'].includes(item.status)).sort((a,b)=>b.index-a.index)[0];
  if(!row)return '';
  const source=row.source?sourceLabel(row.source):'卫星';
  return row.step==='science_query'?`正在检索 ${source} 场景`:row.step==='science_quality_probe'?`正在检查 ${source} 点位云影与有效性`:row.step==='science_subset'?`正在获取 ${source} 数据窗口`:row.step==='science_analysis'?`正在分析 ${source} 科学影像`:row.step==='research_replan'?'正在根据已有结果安排下一步检索':'正在逐图分析';
}
export function businessText(value, images=[]) {
  let text=readableText(value);
  text=text.replace(/；?像素框由整图\s*(?:normalized_1000\s*)?坐标换算[。.]?/gi,'。').replace(/原生模型标注候选\s*R\d+[，,]\s*模型类别为/g,'标注区域的初筛候选类别为');
  images.forEach((image,index)=>{if(!image.id)return;const id=String(image.id),label=`图${index+1}`;text=text.replaceAll(id,label);if(/^[a-f0-9]{32}$/i.test(id))text=text.replace(new RegExp(`\\b${id.slice(0,8)}\\b`,'gi'),label);});
  text=text.replace(/\b[a-f0-9]{32,64}\b/gi,'相关记录');
  text=text.replace(/(?:像素(?:窗|窗口)|归一化(?:框|坐标)|候选框|框)?\s*\[\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*\]/g,'');
  text=text.replace(/\bnormalized_1000\b[，,\s]*/gi,'').replace(/[（(]\s*[，,；;]?\s*[）)]/g,'');
  const phrases={'任务数据合同要求':'根据任务证据需求选择','按已核验能力与可见特征适配排序':'结合可用性与目标细节确定顺序','原生模型最终判读（待核验）：':'影像初筛线索（待核查）：','independent_evidence=false':'不构成独立来源佐证','esri_mosaic':'ArcGIS 影像来源组','bing_mosaic':'Bing 影像来源组'};
  for(const [raw,label] of Object.entries(phrases))text=text.replaceAll(raw,label);
  text=text.replace(/\b(optical|sar|multispectral|hyperspectral)\b/gi,word=>sourceLabel(word)).replace(/\bauto\b/gi,'自动选源');
  return text.trim();
}
export function candidateImageReferences(candidate, recognition, images=[]) {
  const numbers=[];
  for(const index of candidate.evidence_indices||[]){const id=recognition?.observations?.[index]?.asset_id,number=images.findIndex(image=>image.id===id)+1;if(number>0&&!numbers.includes(number))numbers.push(number);}
  return numbers.map(number=>({number,label:`图${number}`,anchor:`#evidence-image-${number}`}));
}

const BASEMAP_SOURCES=new Set(['arcgis','bing','mapquest','amap','tencent','google','mapbox','tianditu','siwei','siwei_daily']);
export const EVIDENCE_GROUP_LABELS={key:'关键证据',basemap:'高分辨率底图',multispectral:'多光谱',sar:'SAR',hyperspectral:'高光谱',optical:'光学与上传影像',other:'其他影像'};
export function evidenceGroup(item={}) {
  const meta=item.metadata||{},source=String(item.source||meta.source||'').toLowerCase(),group=String(item.group||meta.modality||'').toLowerCase();
  if(BASEMAP_SOURCES.has(source))return 'basemap';
  if(group==='sar'||/^sentinel1|^alos|^nisar|^jaxa-palsar/.test(source))return 'sar';
  if(group==='multispectral'||['sentinel2','landsat','hls_s30','hls_l30'].includes(source))return 'multispectral';
  if(group==='hyperspectral'||['emit','enmap'].includes(source))return 'hyperspectral';
  return ['optical','user_upload','user_workbook_embedded'].includes(group)||['user_upload','user_workbook_embedded'].includes(source)?'optical':'other';
}
export function evidenceTime(value,withTime=true) {
  if(value==null||value==='unknown'||value==='')return '未知';
  if(typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value))return value;
  if(typeof value==='string'&&!/^\d{4}-\d{2}-\d{2}T/.test(value))return readableText(value);
  const date=new Date(typeof value==='number'&&value<1e11?value*1000:value);
  if(!Number.isFinite(date.valueOf()))return '未知';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',...(withTime?{hour:'2-digit',minute:'2-digit',second:'2-digit'}:{})}).format(date)+(withTime?'（北京时间）':'');
}
function simpleValue(value) {
  if(value==null||value===''||value==='unknown')return '';
  if(Array.isArray(value))return value.filter(item=>['string','number'].includes(typeof item)).map(item=>readableText(item)).join(' / ');
  return ['string','number'].includes(typeof value)?readableText(value):'';
}
export function evidenceParameters(parameters={}) {
  if(!parameters||typeof parameters!=='object'||Array.isArray(parameters))return [];
  const pick=keys=>keys.map(key=>simpleValue(parameters[key])).find(Boolean);
  return [
    ['实际波段',pick(['bands','source_bands','assets'])],['显示波段组合',pick(['display_bands','rgb_bands','band_combination'])],
    ['极化',pick(['polarizations','polarization'])],['产品',pick(['product','product_type'])],
    ['显示窗口像素尺寸',pick(['dimensions'])],['辐射量类型',pick(['radiometry'])],
    ['源像元单位',pick(['input_units'])],['显示单位',pick(['display_units'])],
    ['原生像元间距（米）',pick(['native_pixel_spacing_m'])],['有效空间分辨率（米）',pick(['effective_resolution_m'])],
    ['成像模式',pick(['instrument_mode','mode'])],['轨道方向',pick(['orbit_direction'])],['相对轨道',pick(['relative_orbit'])],
    ['请求范围',pick(['bbox','requested_bbox'])],['用途范围',pick(['scope'])],
    ['配准误差记录（米）',pick(['registration_error_m'])],
    ['空间校验方法',pick(['registration_method'])],['空间校验适用范围',pick(['registration_scope'])],
  ].filter(([,value])=>value);
}
function sourceParameterRows(parameters,group) {
  const rows=evidenceParameters(parameters);
  if(['multispectral','hyperspectral'].includes(group)&&!rows.some(([label])=>label==='实际波段'))rows.unshift(['实际波段','未知']);
  if(group==='sar'&&!rows.some(([label])=>label==='极化'))rows.unshift(['极化','未知']);
  return rows;
}
export function spatialEvidenceText(value) {
  const record=value&&typeof value==='object'?value:{status:value};
  const status={verified:'已核验空间对应',corrected:'已执行相对位置校正',registered:'已执行空间配准',passed:'空间核验通过',failed:'空间核验未通过',misaligned:'位置存在偏差',unverified:'未核验空间对应',unknown:'空间对应未知',not_checked:'尚未核验空间对应',reference_only:'仅作位置参考',background_only:'仅作区域背景',user_supplied:'用户提供，位置待核实'}[record.status];
  return [status||simpleValue(record.status)||'空间对应未知',simpleValue(record.reason||record.summary)].filter(Boolean).join('；');
}
export function sourceObservationLedger(task={}) {
  const pkg=task.package||task.result||task.report||task,images=task.images||pkg.images||[],raw=task.evidence_summary||pkg.evidence_summary;
  const actual=Array.isArray(raw)?raw:[];
  const documented=new Set(actual.map(row=>row.asset_id));
  const candidates=[...actual,...images.filter(image=>!documented.has(image.id)).map(image=>({asset_id:image.id,source:image.source||image.metadata?.source,observation:(task.image_assessments||pkg.image_assessments||[]).find(row=>row.asset_id===image.id)?.analysis_summary}))];
  const records=candidates.map((row,index)=>{
    const image=images.find(item=>item.id===row.asset_id)||{},meta=image.metadata||{},parameters=row.parameters&&typeof row.parameters==='object'?row.parameters:{};
    const role=row.role||image.role||meta.role;
    const roles={target:'目标识别',current:'当前时相',baseline:'基准时相',target_crop:'目标局部',local_context:'环境与位置参考',full_scene:'完整场景',background_only:'区域背景参考',scientific_auxiliary:'科学辅助',science_auxiliary:'科学辅助',local_crop:'局部复核',sar:'SAR辅助'};
    const group=evidenceGroup({...image,...row});
    const spatial=row.spatial_registration||row.spatial_status||meta.spatial_registration;
    const resolution=row.native_resolution_m??meta.native_resolution_m??parameters.native_resolution_m;
    return {asset_id:row.asset_id||null,source:row.source||image.source||meta.source||'unknown',group,figure:images.findIndex(item=>item.id===row.asset_id)+1,
      acquisition_time:row.acquisition_time||meta.acquisition_time||image.acquisition_time||null,
      period_label:meta.period_label||null,retrieved_at:row.retrieved_at||meta.retrieved_at||image.retrieved_at||null,
      registered_at:row.registered_at||row.created_at||image.created_at||null,resolution:simpleValue(resolution)||'未知',
      parameter_rows:sourceParameterRows(parameters,group),observation:readableText(row.observation)||'本源尚无可公开的观测摘要',
      role:roles[role]||simpleValue(role)||(group==='sar'||group==='multispectral'||group==='hyperspectral'?'科学辅助':'用途未记录'),
      spatial:spatialEvidenceText(spatial),same_point_eligible:row.same_point_eligible??(spatial&&typeof spatial==='object'?spatial.strict_same_point_eligible:undefined),selected:row.selected===true,key:row.asset_id||'source-'+index};
  });
  // Catalog-only and failed scientific requests remain visible as acquisition
  // facts; neither an absent preview nor an empty catalog is a negative finding.
  for(const item of task.scientific_evidence||pkg.scientific_evidence||[]){
    if(records.some(row=>row.source===item.source))continue;
    records.push({key:'science-'+records.length,asset_id:null,source:item.source,group:evidenceGroup(item),figure:0,acquisition_time:item.subset?.scene_acquired_at||null,
      period_label:item.time_range||null,retrieved_at:item.retrieved_at||null,registered_at:null,resolution:'未知',parameter_rows:sourceParameterRows(item.parameters||{},evidenceGroup(item)),
      observation:scientificStatus(item),role:'科学辅助',spatial:'未核验空间对应',selected:false});
  }
  return records;
}
export function evidencePresentation(task={}) {
  const pkg=task.package||task.result||task.report||task,images=task.images||pkg.images||[],ledger=sourceObservationLedger(task),spec=task.evidence_presentation||pkg.evidence_presentation||{};
  const blocked=new Set(ledger.filter(row=>row.same_point_eligible===false).map(row=>row.asset_id));
  const available=new Set(images.filter(image=>safeURL(image.url||image.image_url)&&!blocked.has(image.id)).map(image=>image.id));
  const known=values=>[...new Set((Array.isArray(values)?values:[]).filter(id=>available.has(id)))];
  let primary=known(spec.primary_asset_ids);
  const comparisons=(Array.isArray(spec.comparison_groups)?spec.comparison_groups:[]).map(group=>({title:readableText(group.title)||'证据对照',asset_ids:known(group.asset_ids),reason:readableText(group.reason)})).filter(group=>group.asset_ids.length>=2);
  if(Object.hasOwn(spec,'primary_asset_ids')&&!primary.length)return {mode:'single',primary_asset_ids:[],supporting_asset_ids:known(spec.supporting_asset_ids),comparison_groups:comparisons,reason:readableText(spec.reason),groups:[...new Set(images.filter(image=>available.has(image.id)).map(image=>evidenceGroup({...image,...ledger.find(row=>row.asset_id===image.id)})))]};
  if(!primary.length&&spec.mode==='comparison'&&comparisons.length)primary=comparisons[0].asset_ids;
  if(!primary.length)primary=known(ledger.filter(row=>row.selected).map(row=>row.asset_id)).slice(0,2);
  if(!primary.length)primary=known((task.fusion||pkg.fusion)?.positive_asset_ids).slice(0,2);
  if(!primary.length){
    const target=images.find(image=>available.has(image.id)&&['target_crop','current'].includes(image.role||image.metadata?.role));
    primary=target?[target.id]:known(images.map(image=>image.id)).slice(0,1);
  }
  const supporting=known([...(spec.supporting_asset_ids||[]),...images.map(image=>image.id)]).filter(id=>!primary.includes(id));
  return {mode:spec.mode!=='single'&&primary.length>1?'comparison':'single',
    primary_asset_ids:spec.mode==='single'?primary.slice(0,1):primary,supporting_asset_ids:supporting,
    comparison_groups:comparisons,reason:readableText(spec.reason),groups:[...new Set(images.filter(image=>available.has(image.id)).map(image=>evidenceGroup({...image,...ledger.find(row=>row.asset_id===image.id)})))]};
}
export function evidenceSelection(task,tab='key',comparisonIndex=0) {
  const pkg=task.package||task.result||task.report||task,images=task.images||pkg.images||[],ledger=sourceObservationLedger(task),presentation=evidencePresentation(task);
  const rows=images.map((image,index)=>({image,index,group:evidenceGroup({...image,...ledger.find(row=>row.asset_id===image.id)})})).filter(row=>safeURL(row.image.url||row.image.image_url));
  const active=tab==='key'||presentation.groups.includes(tab)?tab:'key';
  const groupIndex=Math.max(0,Math.min(presentation.comparison_groups.length-1,Number(comparisonIndex)||0));
  const activeComparison=active==='key'&&presentation.mode==='comparison'?presentation.comparison_groups[groupIndex]:null;
  const chosen=activeComparison?.asset_ids||presentation.primary_asset_ids;
  const scientificTab=['multispectral','sar','hyperspectral'].includes(active);
  const groupIds=new Set(rows.filter(row=>row.group===active).map(row=>row.image.id));
  const groupComparisonIds=presentation.comparison_groups.flatMap(group=>group.asset_ids).filter(id=>groupIds.has(id));
  const preferred=scientificTab?[...new Set([...groupComparisonIds,...presentation.primary_asset_ids,
    ...ledger.filter(row=>row.selected).map(row=>row.asset_id),...presentation.supporting_asset_ids])]:presentation.primary_asset_ids;
  const rank=id=>{const index=preferred.indexOf(id);return index<0?Number.MAX_SAFE_INTEGER:index;};
  const visible=(active==='key'?rows.filter(row=>chosen.includes(row.image.id)).sort((a,b)=>chosen.indexOf(a.image.id)-chosen.indexOf(b.image.id)):
    rows.filter(row=>row.group===active).sort((a,b)=>rank(a.image.id)-rank(b.image.id))).slice(0,2);
  return {presentation,tab:active,activeComparison,comparisonIndex:groupIndex,visible,additional:rows.filter(row=>!visible.some(item=>item.image.id===row.image.id)&&(active==='key'||row.group===active))};
}
export function selectWorkbookPoint(draft, documentId, record, assets = []) {
  const previous = draft.workbook_record;
  const previousIds = new Set(draft.workbook_asset_ids || []);
  const files = (draft.files || []).filter(file => {
    const provenance = file.workbook_attachment || file.provenance;
    const oldRecord = previous && provenance?.document_id === previous.document_id && provenance?.record_id === previous.record_id;
    return !previousIds.has(file.id || file.asset_id) && !oldRecord;
  });
  for (const asset of assets) {
    if (!files.some(file => (file.id || file.asset_id) === asset.id)) files.push({
      ...asset, filename: asset.name || '点位关联影像', asset_id: asset.id,
      workbook_attachment: {document_id: documentId, record_id: record.record_id},
    });
  }
  const defaultObjective = point => `核查工作簿点位 ${point.site_id || point.record_id}，结合关联影像识别疑似固废堆存或倾倒线索、变化情况与周边环境，形成专业报告。`;
  const previousObjective = draft.workbook_auto_objective || (previous ? defaultObjective(previous) : '');
  const autoObjective = defaultObjective(record);
  const objective = !draft.objective || draft.objective === previousObjective ? autoObjective : draft.objective;
  const coordinates = record.coordinates;
  const usableCenter = coordinates?.numeric_valid && ['wgs84','unknown'].includes(coordinates.coordinate_system) ? `${coordinates.longitude}, ${coordinates.latitude}` : '';
  const replaceCenter = !draft.center || draft.center === draft.workbook_auto_center;
  return {...draft, files, objective, center: replaceCenter ? usableCenter : draft.center,
    workbook_record: {document_id: documentId, record_id: record.record_id, site_id: record.site_id},
    workbook_asset_ids: assets.map(asset => asset.id), workbook_auto_objective: autoObjective,
    workbook_auto_center: replaceCenter ? usableCenter : '',
    workbook_auto_coordinate_system: replaceCenter && usableCenter ? coordinates.coordinate_system : null,
  };
}
export function taskPayload(draft, parentTaskId = null) {
  const body = { objective: String(draft.objective || '').trim() };
  if (draft.task_types?.length) body.task_types = [...draft.task_types];
  if (draft.search_touched) body.search_policy = !!draft.search_policy;
  if (parentTaskId) body.parent_task_id = parentTaskId;
  const center = draft.center ? parseCenter(draft.center) : parseCenter(body.objective);
  if (draft.center && !center) throw new Error('经纬度格式未识别，请按“经度, 纬度”填写，例如 104.06, 30.67。');
  if (center) {
    // Coordinates define scope. The planner chooses which instruments to use.
    body.center = center;
    if (draft.center && draft.center === draft.workbook_auto_center && ['wgs84','unknown'].includes(draft.workbook_auto_coordinate_system)) {
      body.coordinate_system = draft.workbook_auto_coordinate_system;
    }
  }
  if (draft.workflow_mode) body.workflow_mode = draft.workflow_mode;
  if (draft.waste_status) body.waste_status = draft.waste_status;
  if (draft.research && typeof draft.research === 'object') body.research = {...draft.research};
  if (draft.selected_sources?.length) body.selected_sources = [...draft.selected_sources];
  if (draft.online_supplement) body.online_supplement = draft.online_supplement;
  if (draft.location_id) body.location_id = draft.location_id;
  if (draft.coordinate_system) body.coordinate_system = draft.coordinate_system;
  if (draft.jurisdiction) body.jurisdiction = draft.jurisdiction;
  if (draft.date_start) body.date_start = draft.date_start;
  if (draft.date_end) body.date_end = draft.date_end;
  const assetIds = [], documentIds = [];
  for (const file of draft.files || []) {
    if (file.asset_id) assetIds.push(file.asset_id);
    if (file.document_id) documentIds.push(file.document_id);
    if (Array.isArray(file.assets)) assetIds.push(...file.assets.map(a => a.id || a.asset_id).filter(Boolean));
    if (file.id && !file.asset_id && !file.document_id) {
      const image = file.kind === 'image' || /\.(png|jpg|jpeg|webp|tiff?)$/i.test(file.filename || file.name || '');
      (image ? assetIds : documentIds).push(file.id);
    }
  }
  if (assetIds.length) body.asset_ids = [...new Set(assetIds)];
  if (documentIds.length) body.document_ids = [...new Set(documentIds)];
  if (draft.workbook_record) {
    const record = draft.workbook_record;
    body.prompt = `本次仅分析工作簿中的点位 ${record.site_id}（唯一记录 ${record.record_id}），原表文字为用户提供的未独立验证资料。`;
  }
  return body;
}
