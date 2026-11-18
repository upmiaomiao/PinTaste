import {t} from './i18n.mjs';
import React,{useState,useEffect,useRef} from 'react';
import {Plus,Pencil,Pause,Play,Pin,PinOff,Trash2,Check,Download,RotateCcw,RefreshCw,Save,X,LoaderCircle} from 'lucide-react';
import {api} from './api';
import './design-habits.css';

const RULE_MAX=300;
const categories=[['integration','内容整合','大纲、要点和页面内容怎样组织。'],['layout','内容排版','配色、字体、留白和图文比例。'],['expression','内容表达','数据、流程、图片和动效用什么形式呈现。']];
const origins={explicit:'我设置的',inferred:'从修改中学到的',onboarding:'初始设置时选择的'};
const statuses={candidate:'待确认',active:'使用中',dormant:'已搁置'};
const scopeLabels={purpose:'用途',page_role:'页面',content_type:'内容'};
const failures={FEATURE_DISABLED:'你的账号尚未开通工作功能。',WORK_BRIDGE_REJECTED:'工作服务暂时没有响应，请稍后重试。',WORK_BRIDGE_UNAVAILABLE:'工作服务暂时没有响应，请稍后重试。'};
// The API helper falls back to the bare error code when the work service sent no readable reason.
const readable=e=>e.message&&e.message!==e.code?e.message:t(failures[e.code]||'请求未成功，请稍后重试。');

function HabitEditor({catalogue,purposes,draft,busy,onChange,onSubmit,onCancel}){
 const slots=catalogue[draft.category]||[],hint=slots.find(item=>item.slot===draft.slot)?.hint;
 return <form className="profile-form habit-form" onSubmit={onSubmit}>
  <h3>{draft.id?t("修改设计习惯"):t("添加设计习惯")}</h3>
  {draft.id?null:<><label>{t("类别")}<select aria-label={t("习惯类别")} value={draft.category} disabled={busy} onChange={e=>onChange({...draft,category:e.target.value,slot:catalogue[e.target.value]?.[0]?.slot||''})}>{categories.map(([id,label])=><option key={id} value={id}>{t(label)}</option>)}</select></label>
  <label>{t("项目")}<select aria-label={t("习惯项目")} required value={draft.slot} disabled={busy} onChange={e=>onChange({...draft,slot:e.target.value})}>{slots.map(item=><option key={item.slot} value={item.slot}>{t(item.label)}</option>)}</select></label></>}
  {hint?<p className="settings-note-text">{t(hint)}</p>:null}
  <label>{t("习惯描述")}<textarea aria-label={t("习惯描述")} required rows={3} maxLength={RULE_MAX} value={draft.rule} disabled={busy} placeholder={t("例如：每页要点不超过 4 条。")} onChange={e=>onChange({...draft,rule:e.target.value})}/><small className="habit-count">{draft.rule.length} / {RULE_MAX}</small></label>
  {/* A fixed list served by the work service: generation compares these codes, so free text could never match. */}
  <label>{t("适用用途（可选）")}<select aria-label={t("适用用途")} value={draft.purpose} disabled={busy} onChange={e=>onChange({...draft,purpose:e.target.value})}><option value="">{t("所有场景")}</option>{purposes.map(item=><option key={item.value} value={item.value}>{t(item.label)}</option>)}</select></label>
  <div className="integration-actions"><button className="primary" disabled={busy||!draft.rule.trim()||!draft.slot}>{busy?<LoaderCircle size={16} className="spin"/>:<Save size={16}/>}{t("保存")}</button><button type="button" className="secondary" disabled={busy} onClick={onCancel}><X size={16}/>{t("取消")}</button></div>
 </form>;
}

function HabitItem({item,label,purposes,busy,removing,onPatch,onEdit,onAskRemove,onRemove}){
 const scope=Object.entries(item.scope||{}),stopped=item.paused||item.status==='dormant';
 return <li className="habit-item" data-inactive={stopped||undefined}>
  <p className="habit-rule">{item.pinned?<Pin size={14} aria-label={t("已置顶")}/>:null}{item.rule}</p>
  <div className="habit-meta">
   <span className="role-badge">{t(label)}</span>
   {scope.length?scope.map(([key,value])=><span className="role-badge" key={key}>{t(scopeLabels[key]||key)}{t("：")}{key==='purpose'?t(purposes.find(entry=>entry.value===value)?.label||value):value}</span>):<span className="role-badge">{t("所有场景")}</span>}
   <span className="role-badge">{t(origins[item.origin]||item.origin)}</span>
   <span className="role-badge">{item.paused?t("已暂停"):t(statuses[item.status]||item.status)}</span>
   {item.evidence_count?<span className="habit-evidence">{t("依据 {v0} 次修改", {v0:item.evidence_count})}</span>:null}
   <span className="habit-actions">
    {item.status==='candidate'?<button className="secondary" disabled={busy} onClick={()=>onPatch(item,{status:'active'},"已确认，之后生成时会参考这条习惯。")}><Check size={15}/>{t("确认使用")}</button>:null}
    {item.status==='dormant'?<button className="secondary" disabled={busy} onClick={()=>onPatch(item,{status:'active'},"已重新启用。")}><RotateCcw size={15}/>{t("重新启用")}</button>:null}
    <button className="icon-button" disabled={busy} aria-label={t("修改")} title={t("修改")} onClick={()=>onEdit(item)}><Pencil size={16}/></button>
    <button className="icon-button" disabled={busy} aria-label={item.paused?t("恢复使用"):t("暂停使用")} title={item.paused?t("恢复使用"):t("暂停使用")} onClick={()=>onPatch(item,{paused:!item.paused})}>{item.paused?<Play size={16}/>:<Pause size={16}/>}</button>
    <button className="icon-button" disabled={busy} aria-label={item.pinned?t("取消置顶"):t("置顶")} title={item.pinned?t("取消置顶"):t("置顶")} aria-pressed={item.pinned} onClick={()=>onPatch(item,{pinned:!item.pinned})}>{item.pinned?<PinOff size={16}/>:<Pin size={16}/>}</button>
    <button className="icon-button" disabled={busy} aria-label={t("删除")} title={t("删除")} onClick={()=>onAskRemove(item.id)}><Trash2 size={16}/></button>
   </span>
  </div>
  {removing?<div className="habit-confirm" role="alertdialog" aria-label={t("删除这条设计习惯")}><span>{t("删除后无法恢复，确定删除这条设计习惯吗？")}</span><button className="secondary" disabled={busy} onClick={()=>onRemove(item)}><Trash2 size={15}/>{t("确认删除")}</button><button className="text-button" disabled={busy} onClick={()=>onAskRemove(null)}>{t("取消")}</button></div>:null}
 </li>;
}

/** Everything the work service remembers about this account's design habits, under the user's control. */
export default function DesignHabits(){
 const [state,setState]=useState({status:'loading',data:null}),[busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState('');
 const [draft,setDraft]=useState(null),[removing,setRemoving]=useState(null),[resetting,setResetting]=useState(false),[resetText,setResetText]=useState(''),[shelved,setShelved]=useState(false);
 const live=useRef(false);
 async function load(){
  try{const data=await api('/work/design-habits');if(live.current)setState({status:'ready',data});return data;}
  catch(e){if(!live.current)return;
   // 503 covers both a work service that is restarting and one whose habit tables are not migrated yet.
   if(e.status===503)setState({status:'unavailable',data:null});else{setState(previous=>previous.data?previous:{status:'error',data:null});setError(readable(e));}}
 }
 useEffect(()=>{live.current=true;load();return()=>{live.current=false;};},[]);
 async function perform(action,done){
  setBusy(true);setError('');setNote('');setShelved(false);
  // In a single-choice item a new or re-enabled habit replaces the one in use; say so instead of doing it silently.
  const inUse=new Set((state.data?.items||[]).filter(item=>item.status==='active').map(item=>item.id));
  try{await action();const data=await load();if(live.current&&done){setNote(done);setShelved((data?.items||[]).some(item=>item.status==='dormant'&&inUse.has(item.id)));}return true;}
  catch(e){if(live.current){setError(readable(e));if(e.status===409)await load();}return false;}
  finally{if(live.current)setBusy(false);}
 }
 const patch=(item,changes,done)=>perform(()=>api('/work/design-habits/'+item.id,{method:'PATCH',body:JSON.stringify({version:item.version,...changes})}),done);
 const remove=item=>perform(()=>api('/work/design-habits/'+item.id,{method:'DELETE'}),"已删除。").then(()=>{if(live.current)setRemoving(null);});
 async function save(e){
  e.preventDefault();const rule=draft.rule.trim(),purpose=draft.purpose.trim(),scope={...draft.scope};
  if(purpose)scope.purpose=purpose;else delete scope.purpose;
  const saved=await perform(()=>draft.id
   ?api('/work/design-habits/'+draft.id,{method:'PATCH',body:JSON.stringify({version:draft.version,rule,scope})})
   :api('/work/design-habits',{method:'POST',body:JSON.stringify({category:draft.category,slot:draft.slot,rule,scope})}),draft.id?"已保存修改。":"已添加，之后生成时会参考这条习惯。");
  if(saved&&live.current)setDraft(null);
 }
 async function exportAll(){
  setBusy(true);setError('');setNote('');setShelved(false);
  try{
   // The export arrives in pages; "next" is the position of the following one.
   const data={habits:[],events:[]};let cursor='';
   for(;;){const page=await api('/work/design-habits/export'+(cursor?'?cursor='+encodeURIComponent(cursor):''));
    data.habits.push(...(page.habits||[]));data.events.push(...(page.events||[]));
    if(!page.next||page.next===cursor)break;cursor=page.next;}
   const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),link=document.createElement('a');
   link.href=url;link.download='design-habits.json';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  catch(e){if(live.current)setError(readable(e));}finally{if(live.current)setBusy(false);}
 }
 const retry=()=>{setState({status:'loading',data:null});setError('');load();};
 if(state.status==='unavailable')return <><h3>{t("我的设计习惯")}</h3><p className="settings-note-text" role="status">{t("设计习惯功能暂未开启，或工作服务正在更新。现有的制作功能不受影响，请稍后再来查看。")}</p><button className="secondary" onClick={retry}><RefreshCw size={16}/>{t("重试")}</button></>;
 if(!state.data)return <><h3>{t("我的设计习惯")}</h3>{error?<><p className="form-error" role="alert">{error}</p><button className="secondary" onClick={retry}><RefreshCw size={16}/>{t("重试")}</button></>:<p className="settings-note-text" role="status">{t("正在读取…")}</p>}</>;
 const {items=[],catalogue={},purposes=[],paused,truncated}=state.data,confirmWord=t("重置");
 const labelOf=item=>(catalogue[item.category]||[]).find(entry=>entry.slot===item.slot)?.label||item.slot;
 const startAdd=()=>{setRemoving(null);setDraft({category:'integration',slot:catalogue.integration?.[0]?.slot||'',rule:'',purpose:'',scope:{}});};
 const startEdit=item=>{setRemoving(null);setDraft({id:item.id,version:item.version,category:item.category,slot:item.slot,rule:item.rule,purpose:item.scope?.purpose||'',scope:item.scope||{}});};
 return <div className="design-habits">
  <h3>{t("我的设计习惯")}</h3>
  <p className="settings-note-text">{t("设计习惯是你对 PPT 内容整合、排版和表达的固定偏好，生成时会自动参考；当前任务的要求始终优先。")}</p>
  {error?<p className="form-error" role="alert">{error}</p>:null}{note?<p className="form-note" role="status">{t(note)}{shelved?t("同一项目下原来使用的习惯已搁置，可随时重新启用。"):null}</p>:null}
  <label className="settings-checkbox"><input type="checkbox" checked={!!paused} disabled={busy} onChange={e=>perform(()=>api('/work/design-habits/settings',{method:'PUT',body:JSON.stringify({paused:e.target.checked})}),e.target.checked?"已暂停全部设计习惯。":"已恢复使用设计习惯。")}/>{t("暂停全部设计习惯")}</label>
  <p className="settings-note-text">{t("暂停期间，已有习惯保留但不使用，也不会记录你的修改和反馈用于学习。")}</p>
  {draft?<HabitEditor catalogue={catalogue} purposes={purposes} draft={draft} busy={busy} onChange={setDraft} onSubmit={save} onCancel={()=>setDraft(null)}/>
   :<div className="integration-actions habit-toolbar"><button className="primary" disabled={busy} onClick={startAdd}><Plus size={16}/>{t("添加习惯")}</button><button className="secondary" disabled={busy} onClick={exportAll}><Download size={16}/>{t("导出")}</button></div>}
  {!items.length&&!draft?<p className="settings-note-text habit-empty">{t("还没有设计习惯。添加一条，之后生成时会自动参考。")}</p>:null}
  {truncated?<p className="settings-note-text">{t("已搁置的习惯较多，较早的未在此显示；导出的文件包含全部内容。")}</p>:null}
  {categories.map(([id,label,summary])=>{const list=items.filter(item=>item.category===id);return <section className="habit-section" key={id} aria-label={t(label)}>
   <h3>{t(label)}<small>{list.length}</small></h3><p className="settings-note-text">{t(summary)}</p>
   {list.length?<ul className="habit-list">{list.map(item=><HabitItem key={item.id} item={item} label={labelOf(item)} purposes={purposes} busy={busy} removing={removing===item.id} onPatch={patch} onEdit={startEdit} onAskRemove={setRemoving} onRemove={remove}/>)}</ul>:<p className="habit-none">{t("暂无")}</p>}
  </section>;})}
  <section className="habit-reset"><h3>{t("重置全部设计习惯")}</h3><p className="settings-note-text">{t("清空全部设计习惯，以及用于学习的修改和反馈记录，无法恢复。已生成的 PPT 不受影响。")}</p>
   {resetting?<form className="profile-form habit-form" onSubmit={e=>{e.preventDefault();perform(()=>api('/work/design-habits/reset',{method:'POST',body:JSON.stringify({confirm:true})}),"已重置全部设计习惯。").then(done=>{if(done&&live.current){setResetting(false);setResetText('');setDraft(null);}});}}>
    <label>{t("请输入“{v0}”以确认", {v0:confirmWord})}<input aria-label={t("重置确认")} value={resetText} disabled={busy} autoComplete="off" onChange={e=>setResetText(e.target.value)}/></label>
    <div className="integration-actions"><button className="secondary" disabled={busy||resetText.trim()!==confirmWord}><RotateCcw size={16}/>{t("确认重置")}</button><button type="button" className="text-button" disabled={busy} onClick={()=>{setResetting(false);setResetText('');}}>{t("取消")}</button></div></form>
   :<button className="secondary" disabled={busy} onClick={()=>setResetting(true)}><RotateCcw size={16}/>{t("重置全部")}</button>}
  </section>
 </div>;
}
