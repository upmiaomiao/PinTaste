import {t,getUiLanguage} from './i18n.mjs';
import React,{useEffect,useRef,useState} from 'react';
import {MessageSquareText,Save,RefreshCw,ChevronDown,ChevronRight,LoaderCircle,X} from 'lucide-react';
import {api} from './api';
import VoiceInput from './VoiceInput';
import {Reasoning,ToolHistory} from './ChatParts';
import {DocumentSources} from './DocumentRag';
import './expert-feedback.css';

const verdicts={acceptable:'可以采用',revision_required:'需要修改',unusable:'不可采用'};
const severities={none:'无问题',minor:'一般问题',major:'重要问题',critical:'严重问题'};
const blank={verdict:'revision_required',severity:'major',issues:[],comment:'',suggestion:'',corrected_answer:'',voice_call_ids:[]};
const eventLabels={model_request:'模型实际输入',model_response:'模型实际返回',model_response_error:'模型接口错误',tool_plan:'工具规划',tool_start:'工具调用',tool_result:'工具返回',document_sources:'附件原文依据',execution_end:'执行结果',warning:'提示',error:'错误'};

export function ExpertFeedbackButton({turn,view,onOpen}){
 const active=view?.turnId===turn.id&&view.kind==='expert';
 return <button type="button" className={`source-pill expert-feedback-pill ${active?'active':''}`} aria-label={t("专家反馈")} aria-expanded={active} aria-controls="expert-feedback-panel" data-source-trigger={`expert:${turn.id}`} onClick={event=>onOpen(turn,'expert',null,event.currentTarget)}><MessageSquareText size={17}/><span>{t("专家反馈")}</span><ChevronRight size={14}/></button>;
}

export function ExpertFeedbackPanel({turn,user,config,onClose,onVoiceActive,draftCache}){
 const [compact,setCompact]=useState(()=>window.matchMedia('(max-width: 1080px)').matches);
 const panel=useRef(null),closeButton=useRef(null);
 useEffect(()=>{const media=window.matchMedia('(max-width: 1080px)');const update=()=>setCompact(media.matches);media.addEventListener('change',update);return()=>media.removeEventListener('change',update);},[]);
 useEffect(()=>{closeButton.current?.focus({preventScroll:true});},[]);
 function keyboard(event){
  if(event.key==='Escape'){event.stopPropagation();onClose();return;}
  if(!compact||event.key!=='Tab')return;
  const elements=[...panel.current.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,[tabindex]')].filter(element=>element.tabIndex>=0&&element.getClientRects().length);
  const first=elements[0],last=elements.at(-1);
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
  if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
 }
 return <>{compact&&<button className="source-panel-scrim" aria-label={t("关闭专家反馈面板遮罩")} tabIndex={-1} onClick={onClose}/>}<aside className="source-panel expert-feedback-panel" id="expert-feedback-panel" ref={panel} role={compact?'dialog':'complementary'} aria-modal={compact||undefined} aria-labelledby="expert-feedback-panel-title" onKeyDown={keyboard}><header className="source-panel-header"><h2 id="expert-feedback-panel-title">{t("专家反馈")}</h2><button type="button" className="icon-button" ref={closeButton} aria-label={t("关闭专家反馈面板")} title={t("关闭")} onClick={onClose}><X size={20}/></button></header><div className="expert-feedback-panel-question"><span>{t("正在评审的问题")}</span><p title={turn.user}>{turn.user}</p></div><div className="expert-feedback-panel-body"><ExpertFeedback drawer turn={turn} user={user} config={config} onVoiceActive={onVoiceActive} draftCache={draftCache}/></div></aside></>;
}

export function ExpertFeedback({turn,user,config,onVoiceActive=()=>{},readOnly=false,drawer=false,draftCache}){
 const [open,setOpen]=useState(drawer),[data,setData]=useState(null),[draft,setDraft]=useState({...blank}),[loading,setLoading]=useState(false),[saving,setSaving]=useState(false),[voiceField,setVoiceField]=useState(''),[note,setNote]=useState(''),[error,setError]=useState('');
 const loaded=useRef(''),epoch=useRef(0),voiceOwner=useRef('');
 const [trace,setTrace]=useState(null),[traceOpen,setTraceOpen]=useState(false),[traceLoading,setTraceLoading]=useState(false);
 const enabled=!!config?.expert_feedback?.enabled||(readOnly&&user.role==='admin');
 async function load(force=false,preserve=false){
  const key=`${user.id}:${turn.id}`;
  if(!force&&loaded.current===key)return;
  const current=++epoch.current;setLoading(true);setError('');
  try{const value=await api(`/jobs/${turn.id}/expert-feedback`);if(epoch.current!==current)return;
   setData(value);loaded.current=key;
   const mine=value.items.filter(row=>row.reviewer_id===user.id).sort((a,b)=>b.revision-a.revision)[0];
   if(!preserve)setDraft(draftCache?.current.get(key)||(mine?{...blank,...mine.content}:{...blank,issues:[],voice_call_ids:[]}));
  }catch(e){if(epoch.current===current)setError(e.message);}finally{if(epoch.current===current)setLoading(false);}
 }
 useEffect(()=>{if(open&&enabled)void load();return()=>{epoch.current++;};},[open,enabled,turn.id,user.id]);
 useEffect(()=>{if(data&&draftCache)draftCache.current.set(`${user.id}:${turn.id}`,draft);},[draft,data,turn.id,user.id,draftCache]);
 const mine=data?.items.filter(row=>row.reviewer_id===user.id).sort((a,b)=>b.revision-a.revision)[0];
 async function save(event){event.preventDefault();if(voiceField)return;setSaving(true);setError('');setNote('');
  try{const payload={verdict:draft.verdict,severity:draft.severity,issues:draft.issues,comment:draft.comment,suggestion:draft.suggestion,corrected_answer:draft.corrected_answer,voice_call_ids:draft.voice_call_ids,
      job_revision:data.job_revision,rule_version:data.rules.version,expected_feedback_revision:mine?.revision||0};
   const result=await api(`/jobs/${turn.id}/expert-feedback`,{method:'POST',body:JSON.stringify(payload)});
   draftCache?.current.delete(`${user.id}:${turn.id}`);
   await load(true);setNote(t("意见已保存为第 {v0} 版，原始问答保持不变。", {v0: result.revision}));
  }catch(e){setError(e.message);}finally{setSaving(false);}
 }
 function recording(field,active){if(active){voiceOwner.current=field;setVoiceField(field);onVoiceActive(true);}else if(voiceOwner.current===field){voiceOwner.current='';setVoiceField('');onVoiceActive(false);}}
 async function openTrace(event){event.preventDefault();setTraceOpen(!traceOpen);if(trace||traceOpen||traceLoading)return;setTraceLoading(true);try{setTrace(await api(`/jobs/${turn.id}/expert-feedback/trace`));}catch(e){setError(e.message);}finally{setTraceLoading(false);}}
 function input(field,label,rows){return <div className="expert-input" key={field}><div className="expert-input-label"><label htmlFor={`expert-${turn.id}-${field}`}>{label}</label>{config.expert_feedback.voice_enabled&&<VoiceInput config={config.voice} disabled={saving||loading||!!voiceField} contextKey={`${user.id}:${turn.id}:${open}:${enabled}`} purpose="expert_feedback" jobId={turn.id} field={field} label={t("语音录入{v0}", {v0: label})} onActive={active=>recording(field,active)} onNotice={setError} onText={(text,meta)=>setDraft(value=>({...value,[field]:(value[field]+(value[field]?'\n':'')+text).slice(0,20000),voice_call_ids:meta.call_id?[...new Set([...value.voice_call_ids,meta.call_id])]:value.voice_call_ids}))}/>}</div><textarea id={`expert-${turn.id}-${field}`} aria-label={label} rows={rows} maxLength={20000} disabled={saving||loading} required={field==='comment'||(field==='suggestion'&&draft.verdict!=='acceptable')} value={draft[field]} onChange={e=>setDraft({...draft,[field]:e.target.value})}/></div>;}
 if(!enabled||['queued','running','streaming'].includes(turn.status))return null;
 return <details className={`expert-feedback ${drawer?'expert-drawer-editor':''}`} open={open}><summary onClick={event=>{event.preventDefault();setOpen(!open);}}><MessageSquareText size={16}/><strong>{t("专家反馈")}</strong><span>{data?.items.length?t("{v0} 条记录", {v0: data.items.length}):t("评审本轮问答")}</span><ChevronDown size={15}/></summary>{open&&<div className="expert-feedback-body">
  {loading?<p role="status">{t("正在加载评审记录…")}</p>:data&&<><details className="expert-rules"><summary>{data.rules.title} {t(" · 规则版本 ")}{data.rules.version}</summary><p>{data.rules.rules}</p><ul>{data.rules.criteria.map(item=><li key={item.id}><strong>{item.label}</strong>：{item.description}</li>)}</ul></details>
   <p className="expert-hint">{t("可对照正文、思考过程、工具记录和原文依据填写意见。录音会转成文字，核对后点击保存。")}</p>
   <button type="button" className="text-button" disabled={saving||loading||!!voiceField} onClick={()=>load(true,true)}>{t("刷新规则与记录")}</button>
   <details className="expert-trace" open={traceOpen}><summary onClick={openTrace}>{t("查看逐轮模型与工具记录")}</summary>{traceOpen&&(traceLoading?<p>{t("正在加载记录…")}</p>:trace&&<>{trace.availability==='legacy_partial'?<p className="expert-hint">{t("这是历史问答，没有逐轮轨迹；现存思考、回答和工具记录仍会保留并导出。")}</p>:trace.events.filter(item=>eventLabels[item.event.type]).map(item=><details key={item.sequence}><summary>{item.event.round?t("第 {v0} 轮 · ", {v0: item.event.round}):''}{t(eventLabels[item.event.type])}</summary>{item.event.type==='model_response'?<><strong>{t("模型返回的思考内容")}</strong><pre>{item.event.reasoning||t("接口未返回思考内容。")}</pre><strong>{t("模型返回的内容")}</strong><pre>{item.event.content}</pre>{item.event.tool_calls?.length>0&&<pre>{JSON.stringify(item.event.tool_calls,null,2)}</pre>}</>:<pre>{JSON.stringify(item.event,null,2)}</pre>}</details>)}</>)}</details>
   {data.can_submit&&<form onSubmit={save}><div className="expert-selects"><label>{t("评审结论")}<select aria-label={t("评审结论")} value={draft.verdict} disabled={saving} onChange={e=>setDraft({...draft,verdict:e.target.value,severity:e.target.value==='acceptable'?'none':draft.severity==='none'?'major':draft.severity})}>{Object.entries(verdicts).map(([value,label])=><option key={value} value={value}>{t(label)}</option>)}</select></label><label>{t("问题程度")}<select aria-label={t("问题程度")} value={draft.severity} disabled={saving||draft.verdict==='acceptable'} onChange={e=>setDraft({...draft,severity:e.target.value})}>{Object.entries(severities).map(([value,label])=><option key={value} value={value}>{t(label)}</option>)}</select></label></div>
    <fieldset className="expert-issues"><legend>{t("问题位置")}{draft.verdict!=='acceptable'?t("（至少选一项）"):''}</legend>{data.rules.criteria.map(item=><label key={item.id}><input type="checkbox" checked={draft.issues.includes(item.id)} disabled={saving} onChange={e=>setDraft({...draft,issues:e.target.checked?[...draft.issues,item.id]:draft.issues.filter(x=>x!==item.id)})}/>{item.label}</label>)}</fieldset>
    {input('comment',t("问题与不足说明"),4)}{input('suggestion',t("修改建议"),4)}{input('corrected_answer',t("建议的完整回答（可选）"),4)}
    {draft.voice_call_ids.length>0&&<p className="expert-hint">{t("已关联 ")}{draft.voice_call_ids.length} {t(" 段原始转写，修改输入框后仍会保留转写记录。")}</p>}
    <button className="primary" type="submit" disabled={saving||loading||!!voiceField||!data.can_submit}>{saving?<LoaderCircle className="spin" size={16}/>:<Save size={16}/>}{t("保存意见")}{mine?t("为新版本"):''}</button>{!data.can_submit&&<p className="expert-hint">{t("当前不能提交，请确认管理员已开启专家反馈。")}</p>}
   </form>}{!data.can_submit&&<p className="expert-hint">{t("反馈提交入口已关闭，已保存的意见仍可查看和导出。")}</p>}
   {data.items.length>0&&<section className="expert-history"><h3>{t("已保存的意见与版本")}</h3>{[...data.items].reverse().map(row=><details key={row.id}><summary>{row.reviewer.name} {t(" · 第 ")}{row.revision} {t(" 版 · ")}{t(verdicts[row.content.verdict])} · {new Date(row.created*1000).toLocaleString(getUiLanguage())}</summary><p>{row.content.comment}</p>{row.content.suggestion&&<><strong>{t("修改建议")}</strong><p>{row.content.suggestion}</p></>}{row.content.corrected_answer&&<><strong>{t("建议回答")}</strong><p>{row.content.corrected_answer}</p></>}{row.content.voice_transcripts?.length>0&&<details><summary>{t("原始语音转写")}</summary>{row.content.voice_transcripts.map(voice=><p key={voice.id}>{voice.transcript}</p>)}</details>}<details><summary>{t("提交时的评审规则 · 版本 ")}{row.rule_snapshot.version}</summary><p>{row.rule_snapshot.rules}</p></details></details>)}</section>}
  </>}{note&&<p className="form-note" role="status">{t(note)}</p>}{error&&<p className="form-error" role="alert">{t(error)}</p>}
 </div>}</details>;
}

export default function ExpertFeedbackSettings({user,onSaved,onExport}){
 const [cfg,setCfg]=useState(null),[detail,setDetail]=useState(null),[filter,setFilter]=useState(''),[reviewed,setReviewed]=useState(false),[offset,setOffset]=useState(0),[saving,setSaving]=useState(false),[loadingDetail,setLoadingDetail]=useState(false),[note,setNote]=useState(''),[error,setError]=useState('');
 async function loadConfig(){const value=await api('/admin/expert-feedback');setCfg(value);return value;}
 // Both the query identity and request generation must match: abort alone cannot
 // prevent an already fulfilled response from overwriting a newer selection.
 const listKey=JSON.stringify([user.id,offset,filter,reviewed]);
 const currentListKey=useRef(listKey),listRequest=useRef({id:0,abort:null});currentListKey.current=listKey;
 const [list,setList]=useState({key:null,status:'loading',items:[],hasMore:false,error:''});
 const listCurrent=list.key===listKey,loadingTurns=!listCurrent||list.status==='loading';
 const items=listCurrent?list.items:[],hasMore=listCurrent&&list.status==='ready'&&list.hasMore;
 const listError=listCurrent?list.error:'';
 async function loadTurns(){
  const key=listKey,id=++listRequest.current.id;listRequest.current.abort?.abort();
  const abort=new AbortController();listRequest.current.abort=abort;
  const current=()=>listRequest.current.id===id&&currentListKey.current===key&&!abort.signal.aborted;
  setList({key,status:'loading',items:[],hasMore:false,error:''});
  try{const value=await api(`/admin/expert-feedback/turns?offset=${offset}&user_id=${encodeURIComponent(filter)}&reviewed=${reviewed}`,{signal:abort.signal});
   if(current())setList({key,status:'ready',items:value.items,hasMore:value.has_more,error:''});
  }catch(e){if(current())setList({key,status:'error',items:[],hasMore:false,error:e.message});}
 }
 useEffect(()=>{loadConfig().catch(e=>setError(e.message));},[]);
 useEffect(()=>{void loadTurns();return()=>{++listRequest.current.id;listRequest.current.abort?.abort();};},[user.id,offset,filter,reviewed]);
 async function save(event){event.preventDefault();setSaving(true);setError('');setNote('');try{await api('/admin/expert-feedback',{method:'PUT',body:JSON.stringify({enabled:cfg.enabled,voice_enabled:cfg.voice_enabled,title:cfg.title,rules:cfg.rules})});await loadConfig();await onSaved();setNote(t("配置已保存。关闭时问答界面隐藏反馈入口，已有意见与轨迹继续保留。"));}catch(e){setError(e.message);}finally{setSaving(false);}}
 async function grant(row,enabled){setSaving(true);setError('');try{await api('/admin/expert-feedback/reviewers/'+row.id,{method:'PUT',body:JSON.stringify({enabled})});await loadConfig();await onSaved();}catch(e){setError(e.message);}finally{setSaving(false);}}
 async function inspect(jid){setLoadingDetail(true);setError('');try{setDetail(await api('/admin/expert-feedback/turns/'+jid));}catch(e){setError(e.message);}finally{setLoadingDetail(false);}}
 if(!cfg)return <p role="status">{error||t("正在加载专家反馈设置…")}</p>;
 return <><form className="admin-form" onSubmit={save}><section className="admin-panel"><h2>{t("专家反馈开关")}</h2><p className="admin-hint">{t("管理员及指定专家可以评审问答。专家评审自己的对话，管理员可查看全平台记录。反馈不会自动修改模型回答。")}</p><label className="admin-switch"><input type="checkbox" checked={cfg.enabled} disabled={saving} onChange={e=>setCfg({...cfg,enabled:e.target.checked})}/>{t("在问答界面开启专家反馈")}</label><label className="admin-switch"><input type="checkbox" checked={cfg.voice_enabled} disabled={saving} onChange={e=>setCfg({...cfg,voice_enabled:e.target.checked})}/>{t("允许专家语音录入意见")}</label><p className="admin-hint">{t("语音沿用平台已配置的语音识别服务。完成录音后核对文字再保存，原始转写与修改后的意见一并归档。")}</p><label className="admin-wide">{t("评审规则名称")}<input value={cfg.title} maxLength={100} required onChange={e=>setCfg({...cfg,title:e.target.value})}/></label><label className="admin-wide">{t("专家反馈规则")}<textarea aria-label={t("专家反馈规则")} value={cfg.rules} rows={5} maxLength={10000} required onChange={e=>setCfg({...cfg,rules:e.target.value})}/></label><p className="admin-hint">{t("修改规则会产生新版本，每条意见保存提交时的规则快照。评审维度：")}{cfg.criteria.map(item=>item.label).join('、')}。</p><button className="primary" disabled={saving}><Save size={16}/>{saving?t("正在保存…"):t("保存专家反馈设置")}</button></section></form>
 <section className="admin-panel"><h2>{t("指定专家账号")}</h2><p className="admin-hint">{t("专家资格只增加自己问答的评审能力，不授予系统管理或其他用户对话的访问权限。")}</p><div className="table-wrap"><table><thead><tr><th>{t("姓名 / 邮箱")}</th><th>{t("机构")}</th><th>{t("状态")}</th><th>{t("专家权限")}</th></tr></thead><tbody>{cfg.reviewers.map(row=><tr key={row.id}><td>{row.name}<small>{row.email}</small></td><td>{row.organization}</td><td>{row.active?t("正常"):t("已停用")}</td><td><label><input type="checkbox" aria-label={t("指定专家 ")+row.email} checked={!!row.expert} disabled={saving||row.role==='admin'} onChange={e=>grant(row,e.target.checked)}/>{row.role==='admin'?t("管理员自带"):t("允许评审")}</label></td></tr>)}</tbody></table></div></section>
 <section className="admin-panel"><div className="admin-panel-title"><h2>{t("问答与专家意见")}</h2><button className="secondary" disabled={loadingTurns} onClick={loadTurns}><RefreshCw size={14}/>{t("刷新")}</button></div><p className="admin-hint">{t("完整导出包含问题、实际返回的思考内容、逐轮模型请求与返回、工具参数及结果、原文依据、最终回答和专家意见的全部版本及规则。")}</p><div className="expert-review-filters"><select aria-label={t("筛选评审用户")} value={filter} onChange={e=>{setFilter(e.target.value);setOffset(0);}}><option value="">{t("全部用户")}</option>{cfg.reviewers.map(row=><option key={row.id} value={row.id}>{row.name} · {row.email}</option>)}</select><label><input type="checkbox" checked={reviewed} onChange={e=>{setReviewed(e.target.checked);setOffset(0);}}/>{t("只看有专家意见的问答")}</label><button className="secondary" onClick={onExport}>{t("导出完整数据")}</button></div>{listError&&<p className="form-error" role="alert">{t(listError)} <button className="text-button" type="button" onClick={loadTurns}>{t("重试")}</button></p>}<div className="table-wrap" aria-busy={loadingTurns}><table><thead><tr><th>{t("问题 / 用户")}</th><th>{t("意见版本数")}</th><th>{t("操作")}</th></tr></thead><tbody>{items.map(row=><tr key={row.id}><td>{row.question}<small>{row.name} · {new Date(row.created*1000).toLocaleString(getUiLanguage())}</small></td><td>{row.feedback_versions}</td><td><button className="text-button" disabled={loadingDetail} onClick={()=>inspect(row.id)}>{t("查看与评审")}</button></td></tr>)}{loadingTurns&&<tr><td colSpan={3} role="status">{t("正在加载…")}</td></tr>}{!loadingTurns&&!listError&&!items.length&&<tr><td colSpan={3}>{t("暂无匹配的问答")}</td></tr>}</tbody></table></div><div className="export-actions"><button className="secondary" disabled={loadingTurns||offset===0} onClick={()=>setOffset(Math.max(0,offset-50))}>{t("上一页")}</button><button className="secondary" disabled={loadingTurns||!hasMore} onClick={()=>setOffset(offset+50)}>{t("下一页")}</button></div></section>
 {detail&&<section className="admin-panel expert-review-detail"><div className="admin-panel-title"><h2>{t("问答评审 · ")}{detail.user.name}</h2><button className="icon-button" aria-label={t("关闭问答评审")} onClick={()=>setDetail(null)}><X size={18}/></button></div><h3>{t("问题")}</h3><p className="expert-original">{detail.turn.user}</p>{detail.turn.reasoning&&<Reasoning turn={detail.turn}/>}<ToolHistory turn={detail.turn}/><DocumentSources turn={detail.turn}/><h3>{t("模型原回答")}</h3><p className="expert-original">{detail.turn.answer||detail.turn.error||t("没有收到回答内容。")}</p><ExpertFeedback readOnly key={detail.turn.id} turn={detail.turn} user={user} config={{expert_feedback:{enabled:cfg.enabled,voice_enabled:cfg.voice_enabled},voice:cfg.voice}}/>{!cfg.enabled&&<p className="admin-hint">{t("专家反馈入口已关闭。已有意见可继续查看，开启后可提交新意见；完整导出始终保留已归档的反馈。")}</p>}</section>}
 {note&&<p className="form-note" role="status">{t(note)}</p>}{error&&<p className="form-error" role="alert">{t(error)}</p>}</>;
}
