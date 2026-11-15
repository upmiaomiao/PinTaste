import {escapeHTML as esc,safeURL} from './lib.mjs';

const TYPES={webpage:'网页资料',academic:'学术资料',wechat:'微信公众号'};
const STATES={completed:'已检索',partial:'部分完成',failed:'暂不可用',cooldown:'服务冷却中',not_configured:'未配置',disabled:'未启用',not_required:'本题无需检索',no_results:'未找到结果'};
const rowsOf=policy=>Array.isArray(policy)?policy:policy?.sources||policy?.items||policy?.results||[];
const anchor=(taskId,id)=>`source-${taskId||'current'}-${id}`;
export function sourceCitations(text,policy,taskId=''){
  const ids=new Set(rowsOf(policy).map(row=>row.id));
  return String(text).replace(/\[(P\d{3,6})\](?!\()/g,(all,id)=>ids.has(id)?`[${id}](#${anchor(taskId,id)})`:all);
}
function dateText(value){const date=new Date(value);return value&&Number.isFinite(date.valueOf())?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date):'';}
export function renderSearchSources(policy,citedIds=[],taskId=''){
  const items=rowsOf(policy).filter(row=>safeURL(row.url||row.link)),cited=new Set(citedIds);
  const channels=Object.entries(policy?.channels||{});
  if(!items.length&&!channels.length)return '';
  const ordered=[...items].sort((a,b)=>Number(cited.has(b.id))-Number(cited.has(a.id)));
  const render=row=>{const kind=row.source_type||'webpage',date=dateText(row.published_at||row.date),snapshot=safeURL(row.snapshot_url),url=safeURL(row.url||row.link),read=row.content_status==='read'?'已读正文':row.content_status==='partial'?'已读正文片段':row.content_status==='failed'?'正文未取得':kind==='wechat'?'仅检索摘要':'';
    return `<li id="${esc(anchor(taskId,row.id||''))}"><div class="source-meta"><span class="source-kind">${esc(TYPES[kind]||'联网资料')}</span>${row.id?`<span>[${esc(row.id)}]</span>`:''}${cited.has(row.id)?'<span class="source-cited">用于回答</span>':''}${read?`<span>${esc(read)}</span>`:''}</div><a class="source-title" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(row.title||row.name||'参考资料')}</a>${row.account||date?`<p class="source-account">${esc([row.account,date].filter(Boolean).join(' · '))}</p>`:''}${snapshot?`<a class="source-snapshot" href="${esc(snapshot)}" target="_blank" rel="noopener noreferrer">查看已保存内容</a>`:''}${row.metadata_conflicts?.length?'<p class="source-note">索引与正文时间不同，显示正文日期。</p>':''}</li>`;};
  const visible=[];
  for(const kind of Object.keys(TYPES)){const row=ordered.find(row=>(row.source_type||'webpage')===kind);if(row&&!visible.includes(row))visible.push(row);}
  for(const row of ordered.filter(row=>cited.has(row.id))){if(visible.length>=12)break;if(!visible.includes(row))visible.push(row);}
  for(const row of ordered){if(visible.length>=6)break;if(!visible.includes(row))visible.push(row);}
  const rest=ordered.filter(row=>!visible.includes(row));
  return `<section class="answer-sources" aria-label="联网来源"><header><h3>联网来源</h3><span>${items.length} 条</span></header>${channels.length?`<div class="source-channel-status">${channels.map(([kind,state])=>`<span>${esc(TYPES[kind]||kind)}：${esc(STATES[state.status]||'状态待确认')}${state.source_count?` · ${Number(state.source_count)}条`:''}</span>`).join('')}</div>`:''}${visible.length?`<ol class="source-list">${visible.map(render).join('')}</ol>`:'<p class="source-note">本次未取得可引用资料。</p>'}${rest.length?`<details data-ui-state="sources-${esc(taskId)}"><summary>查看其余 ${rest.length} 条来源</summary><ol class="source-list" start="${visible.length+1}">${rest.map(render).join('')}</ol></details>`:''}</section>`;
}
