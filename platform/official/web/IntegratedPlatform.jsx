import {t,getUiLanguage} from './i18n.mjs';
import React,{useState,useEffect,useRef} from 'react';
import {BriefcaseBusiness,MessageSquare,LoaderCircle,ShieldCheck,RefreshCw,Link2,Trash2} from 'lucide-react';
import {api} from './api';
import './integrated-platform.css';
import {workDestination,workSsoDestination} from './workNavigation.mjs';
import {canManageWorkQuota,qaHistoryItems,openEmbeddedWork} from './settingsNavigation.mjs';

export const workFeatures={chat:'问答模式',work:'工作模式',upload:'上传材料',ppt_generate:'生成 PPT',ppt_trace:'可编辑描摹',image_gpt:'GPT 生图',image_gemini:'Gemini 生图',template_submit:'提交共享模板',template_review:'审核模板',work_admin:'工作系统管理',work_quota_admin:'积分管理'};
const basicFeatures=Object.keys(workFeatures).filter(key=>!['template_review','work_admin','work_quota_admin'].includes(key));
const unpack=value=>value?.data??value;

export async function openWork(url,workUserId){
 const origin=window.location.origin;
 const destination=workDestination(url,origin);
 const result=await api('/work/session',{method:'POST',body:JSON.stringify({next:destination.pathname+destination.search,...(workUserId?{work_user_id:workUserId}:{})})});
 const target=workSsoDestination(result.work_url,origin);
 window.location.assign(target.href);
}

export function ModeSwitcher({user,onNotice}){
 const [opening,setOpening]=useState(false);
 return <button className="header-manage" aria-label={t("前往无隅工作")} disabled={opening||user.permissions?.work===false} onClick={async()=>{setOpening(true);try{await openWork();}catch(e){onNotice(e.message);setOpening(false);}}}>{opening?<LoaderCircle size={15} className="spin"/>:<BriefcaseBusiness size={16}/>}<span>{opening?t("正在进入…"):t("无隅工作")}</span></button>;
}

export function IntegratedHistory({user,filter,onChat,onNotice,onDelete,onClearFilter,refreshKey}){
 const [history,setHistory]=useState({userId:user.id,items:[],loaded:false});
 const items=history.userId===user.id?history.items:[],loading=history.userId!==user.id||!history.loaded;
 useEffect(()=>{let live=true;const abort=new AbortController();api('/history?mode=chat',{signal:abort.signal}).then(result=>{if(live)setHistory({userId:user.id,items:qaHistoryItems(result.items),loaded:true});}).catch(e=>{if(live){setHistory(current=>current.userId===user.id&&current.loaded?current:{userId:user.id,items:[],loaded:true});onNotice(e.message);}});return()=>{live=false;abort.abort();};},[user.id,refreshKey]);
 const shown=qaHistoryItems(items,filter);
 return <>{loading?<p className="history-empty">{t("正在读取记录…")}</p>:shown.map(item=><div className="history-row integrated-history-item" key={item.id}><button className="integrated-history-row" type="button" title={item.title} onClick={()=>onChat(item.id)}><MessageSquare size={16}/><span><strong>{item.title}</strong><small>{new Date(typeof item.updated_at==='number'?(item.updated_at<100000000000?item.updated_at*1000:item.updated_at):item.updated_at).toLocaleDateString(getUiLanguage())}</small></span></button>{onDelete?<button className="icon-button delete-chat" type="button" aria-label={t("删除对话：")+item.title} title={t("删除对话：")+item.title} aria-haspopup="dialog" onClick={event=>{event.preventDefault();event.stopPropagation();onDelete(item);}}><Trash2 size={14}/></button>:null}</div>)}{!loading&&!shown.length?<div className="history-empty"><p>{items.length&&filter.trim()?t("没有匹配的对话。"):t("还没有问答记录。")}</p>{filter.trim()&&onClearFilter?<button type="button" className="text-button" onClick={onClearFilter}>{t("清空搜索")}</button>:null}</div>:null}</>;
}

export function WorkAccountSettings({user,onNotice,onAdmin,context='chat',refreshKey=0}){
 const [state,setState]=useState({userId:user.id,quota:null,error:'',loading:true});
 const currentUser=useRef(user.id),request=useRef({id:0,abort:null});currentUser.current=user.id;
 const ownState=state.userId===user.id,quota=ownState?state.quota:null,error=ownState?state.error:'',loading=!ownState||state.loading;
 const load=()=>{
  const userId=user.id,id=++request.current.id;request.current.abort?.abort();const abort=new AbortController();request.current.abort=abort;
  const current=()=>request.current.id===id&&currentUser.current===userId&&!abort.signal.aborted;
  setState(previous=>({userId,quota:previous.userId===userId?previous.quota:null,error:'',loading:true}));
  api('/work/quota',{signal:abort.signal}).then(value=>{if(current())setState({userId,quota:unpack(value),error:'',loading:false});})
   .catch(e=>{if(current())setState(previous=>({...previous,error:e.message,loading:false}));});
 };
 const setError=error=>setState(previous=>previous.userId===user.id?{...previous,error}:previous);
 useEffect(()=>{load();return()=>{++request.current.id;request.current.abort?.abort();};},[user.id,refreshKey]);
 const available=quota?.available;
 const enterWork=path=>context==='work'?Promise.resolve(openEmbeddedWork(path)):openWork(path);
 return <><h3>{t("工作与额度")}</h3><p className="settings-note-text">{t("问答与工作共用此账号。工作中的文字、生图、描摹、联网与语音统一使用积分。")}</p>{error?<p role="alert" className="form-error">{t(error)}</p>:null}
 {available?<><div className="personal-data-cards"><div><BriefcaseBusiness size={18}/><strong>{quota.unlimited?t("无上限"):quota.daily?.points.available??0}</strong><span>{t("当日可用积分")}</span></div><div><ShieldCheck size={18}/><strong>{quota.unlimited?t("无上限"):available.points}</strong><span>{t("可用积分")}</span></div></div><p className="settings-note-text">{t("处理中预占：")}{quota.reserved?.points??0} {t("积分。每日积分北京时间零点重置、不累加；注册与管理员赠额长期保留。")}</p></>:loading?<p>{t("正在读取工作额度…")}</p>:null}
 {canManageWorkQuota(user)&&onAdmin?<p><button className="text-button" onClick={()=>onAdmin('quota')}>{t("管理积分与用量")}</button></p>:null}<h3>{t("已开通的功能")}</h3><div className="permission-badges">{Object.entries(workFeatures).map(([key,label])=><span key={key} data-enabled={user.permissions?.[key]===true}>{t(label)} · {user.permissions?.[key]?t("已开通"):t("未开通")}</span>)}</div><div className="integration-actions"><button className="secondary" disabled={loading} onClick={load}><RefreshCw size={16}/>{t("刷新额度")}</button>{context!=='work'?<button className="secondary" disabled={user.permissions?.work===false} onClick={()=>enterWork('/work').catch(e=>{setError(e.message);onNotice?.(e.message);})}>{t("进入工作模式")}</button>:null}{user.permissions?.template_review?<button className="secondary" onClick={()=>enterWork('/work?templates=review').catch(e=>setError(e.message))}>{t("模板审核")}</button>:null}{user.permissions?.work_admin?<button className="secondary" onClick={()=>enterWork('/work/admin').catch(e=>setError(e.message))}>{t("工作模型设置")}</button>:null}</div></>;
}

export function LinkWorkAccount({user,onLinked}){
 const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState('');
 const legacy=user?.account_type==='legacy_work';
 return <><h3>{legacy?t("关联无隅 AI 平台账号"):t("关联原工作账号")}</h3><p className="settings-note-text">{t("验证另一个账号后，在同一身份下访问记录与额度。同名不会自动关联；存在数据冲突时保留双方账号，由系统明确提示。")}</p><form className="profile-form" onSubmit={async event=>{event.preventDefault();setBusy(true);setError('');setNote('');try{await api(legacy?'/me/link-platform':'/me/link-work',{method:'POST',body:JSON.stringify({[legacy?'email':'username']:username,password,idempotency_key:crypto.randomUUID()})});setPassword('');setNote("账号已关联，原有数据保留。");onLinked?.();}catch(e){setError(e.message);}finally{setBusy(false);}}}><label>{legacy?t("无隅 AI 平台邮箱"):t("原工作账号")}<input type={legacy?'email':'text'} value={username} required maxLength={legacy?254:80} autoComplete="username" onChange={e=>setUsername(e.target.value)}/></label><label>{t("另一个账号的密码")}<input type="password" value={password} required maxLength={128} autoComplete="current-password" onChange={e=>setPassword(e.target.value)}/></label>{error?<p className="form-error" role="alert">{t(error)}</p>:null}{note?<p className="form-note" role="status">{t(note)}</p>:null}<button className="secondary" disabled={busy}><Link2 size={16}/>{busy?t("正在验证…"):t("验证并关联")}</button></form></>;
}

export function IntegratedUserManagement(){
 const [users,setUsers]=useState([]),[selected,setSelected]=useState([]),[feature,setFeature]=useState('work'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState(''),[query,setQuery]=useState('');
 const load=()=>api('/admin/integrated-users').then(data=>{setUsers(data.users);setSelected(old=>old.filter(id=>data.users.some(user=>user.id===id)));});
 useEffect(()=>{load().catch(e=>setError(e.message));},[]);
 const shown=users.filter(user=>[user.name,user.email,user.legacy_alias,user.organization].filter(Boolean).join(' ').toLowerCase().includes(query.toLowerCase()));
 async function permissions(ids,patch){if(!ids.length)return;setBusy(true);setError('');setNote('');try{await api('/admin/integrated-users/permissions',{method:'POST',body:JSON.stringify({user_ids:ids,patch,idempotency_key:crypto.randomUUID()})});await load();setNote("权限已保存，将同步用于问答和工作接口。");}catch(e){setError(e.message);}finally{setBusy(false);}}
 return <section className="admin-panel"><h2>{t("统一用户与功能权限")}</h2><p className="admin-hint">{t("平台账号与原工作账号一并管理；不同身份不会按姓名合并。模板审核、工作管理与平台管理员分别授权。")}</p>{error?<p className="form-error" role="alert">{t(error)}</p>:null}{note?<p className="form-note" role="status">{t(note)}</p>:null}
 <div className="integrated-user-toolbar"><input type="search" aria-label={t("搜索统一用户")} placeholder={t("搜索姓名、账号或机构…")} value={query} onChange={e=>setQuery(e.target.value)}/><button className="secondary" disabled={busy} onClick={()=>setSelected(shown.filter(u=>u.role!=='admin').map(u=>u.id))}>{t("选择当前普通用户")}</button><button className="secondary" disabled={busy} onClick={()=>setSelected([])}>{t("清空选择")}</button></div>
 <div className="integrated-user-toolbar"><span>{t("已选")} {selected.length} {t("人")}</span><select aria-label={t("批量功能权限")} value={feature} onChange={e=>setFeature(e.target.value)}>{Object.entries(workFeatures).map(([key,label])=><option key={key} value={key}>{t(label)}</option>)}</select><button className="secondary" disabled={busy||!selected.length} onClick={()=>permissions(selected,{[feature]:true})}>{t("批量开通")}</button><button className="secondary" disabled={busy||!selected.length} onClick={()=>permissions(selected,{[feature]:false})}>{t("批量关闭")}</button><button className="primary" disabled={busy||!selected.length} onClick={()=>permissions(selected,Object.fromEntries(basicFeatures.map(key=>[key,true])))}>{t("一键开通常用功能")}</button></div>
 <div className="table-wrap"><table className="integrated-users"><thead><tr><th>{t("选择")}</th><th>{t("用户")}</th><th>{t("账号类型")}</th><th>{t("功能权限")}</th><th>{t("管理")}</th></tr></thead><tbody>{shown.map(user=><tr key={user.id}><td><input type="checkbox" aria-label={t("选择用户：")+user.name} disabled={busy||user.role==='admin'} checked={selected.includes(user.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,user.id]:old.filter(id=>id!==user.id))}/></td><td>{user.name}<small>{user.email||user.legacy_alias}</small><small>{user.organization}</small></td><td>{user.role==='admin'?t("平台管理员"):user.account_type==='legacy_work'?t("原工作账号"):t("平台账号")}{user.work_admin&&user.role!=='admin'?<small>{t("工作管理员")}</small>:null}{user.linked_to_user_id?<small>{t("已关联至")} {users.find(item=>item.id===user.linked_to_user_id)?.name||t("主账号")}{t("；权限随主账号同步")}</small>:null}</td><td><div className="permission-checks">{Object.entries(workFeatures).map(([key,label])=><label key={key}><input type="checkbox" checked={!!user.permissions?.[key]} disabled={busy||user.role==='admin'} onChange={e=>permissions([user.id],{[key]:e.target.checked})}/>{t(label)}</label>)}</div></td><td><div className="integration-actions"><button className="text-button" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('/admin/users/'+user.id,{method:'PATCH',body:JSON.stringify({active:!user.active})});await load();}catch(e){setError(e.message);}finally{setBusy(false);}}}>{user.active?t("停用账号"):t("恢复账号")}</button><button className="text-button" disabled={busy||!!user.linked_to_user_id} title={user.linked_to_user_id?t("请在关联的主账号上管理平台角色"):undefined} onClick={async()=>{if(!window.confirm(user.role==='admin'?t("将此用户改为普通用户？最后一位平台管理员不能移除。"):t("授予此用户平台管理员权限？将允许其管理所有用户及系统配置。")))return;setBusy(true);try{await api('/admin/integrated-users/role',{method:'POST',body:JSON.stringify({user_id:user.id,role:user.role==='admin'?'user':'admin',idempotency_key:crypto.randomUUID()})});await load();}catch(e){setError(e.message);}finally{setBusy(false);}}}>{user.role==='admin'?t("移除平台管理员"):t("设为平台管理员")}</button></div></td></tr>)}</tbody></table></div>
 <p className="admin-hint">{t("积分赠送与消费控制请使用系统管理中的积分与用量管理。")}</p></section>;
}
