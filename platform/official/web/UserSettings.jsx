import {t,getUiLanguage} from './i18n.mjs';
import React,{useState,useEffect,useRef} from 'react';
import {Settings,UserRound,Database,Info,Sun,Moon,Monitor,X,Check,Download,LogOut,Save,LoaderCircle,Type,Keyboard,FileText,HardDrive} from 'lucide-react';
import {Logo} from './ChatParts';
import {api} from './api';
import {defaultPreferences} from './preferences';
import {WorkAccountSettings,LinkWorkAccount} from './IntegratedPlatform';
import {canOpenSystemManagement} from './settingsNavigation.mjs';

/** A one-use code from an administrator becomes long-term work points. */
function RedeemCode({onRedeemed}){
 const [code,setCode]=useState(''),[busy,setBusy]=useState(false),[result,setResult]=useState(''),[failure,setFailure]=useState('');
 const active=useRef(false),pending=useRef(false);
 useEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
 async function redeem(e){e.preventDefault();if(pending.current||!code.trim())return;pending.current=true;setBusy(true);setResult('');setFailure('');
  try{const r=await api('/work/quota/redeem',{method:'POST',body:JSON.stringify({code:code.trim()})});onRedeemed();if(!active.current)return;setResult(t("兑换成功，已到账 {v0} 积分，当前可用 {v1} 积分。", {v0:(r.points), v1:(r.quota?.available?.points??'—')}));setCode('');}
  catch(err){if(active.current)setFailure(err.message);}finally{pending.current=false;if(active.current)setBusy(false);}}
 return <form className="settings-form" onSubmit={redeem}><h3>{t("兑换码")}</h3><p className="settings-note-text">{t("输入管理员发给你的兑换码，积分长期有效。")}</p>
  <label className="settings-field">{t("兑换码")}<input aria-label={t("兑换码")} value={code} maxLength={40} placeholder="WY-XXXX-XXXX-XXXX" disabled={busy} onChange={e=>setCode(e.target.value)}/></label>
  <button className="primary" disabled={busy||!code.trim()}>{busy?t("兑换中…"):t("兑换")}</button>
  {result?<p className="form-note" role="status">{result}</p>:null}{failure?<p className="form-error" role="alert">{t(failure)}</p>:null}</form>;
}

const tabs=[['general','通用设置',Settings],['account','账号管理',UserRound],['work','工作与额度',FileText],['linked','关联原账号',UserRound],['data','数据管理',Database],['about','关于平台',Info]];
export default function UserSettings({user,initialTab='general',prefs,onUser,onClose,onLogout,onAdmin,context='chat'}){
 const [tab,setTab]=useState(initialTab),[profile,setProfile]=useState({name:user.name,organization:user.organization,identity:user.identity}),[identities,setIdentities]=useState([]),[saving,setSaving]=useState(false),[error,setError]=useState(''),[note,setNote]=useState(''),[quotaRefresh,setQuotaRefresh]=useState(0),[includeFiles,setIncludeFiles]=useState(false),[download,setDownload]=useState('');
 const dialog=useRef(null),close=useRef(onClose);close.current=onClose;
 const [usage,setUsage]=useState({userId:user.id,status:'loading',data:null,error:''});
 const currentUser=useRef(user.id),usageRequest=useRef({id:0,abort:null});currentUser.current=user.id;
 const ownUsage=usage.userId===user.id,data=ownUsage?usage.data:null,usageError=ownUsage?usage.error:'',usageLoading=!ownUsage||usage.status==='loading';
 function loadUsage(){
  const userId=user.id,id=++usageRequest.current.id;usageRequest.current.abort?.abort();const abort=new AbortController();usageRequest.current.abort=abort;
  const current=()=>usageRequest.current.id===id&&currentUser.current===userId&&!abort.signal.aborted;
  setUsage({userId,status:'loading',data:null,error:''});
  api('/me/data',{signal:abort.signal}).then(data=>{if(current())setUsage({userId,status:'ready',data,error:''});})
   .catch(e=>{if(current())setUsage({userId,status:'error',data:null,error:e.message});});
 }
 useEffect(()=>{let valid=true;api('/public').then(d=>{if(valid)setIdentities(d.identities);}).catch(()=>{});return()=>{valid=false;};},[]);
 useEffect(()=>{loadUsage();return()=>{++usageRequest.current.id;usageRequest.current.abort?.abort();};},[user.id]);
 useEffect(()=>{const previous=document.activeElement;dialog.current?.querySelector('button')?.focus();const key=e=>{if(e.key==='Escape'){e.preventDefault();close.current();}if(e.key==='Tab'){const items=[...dialog.current.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)')].filter(el=>el.getClientRects().length);const first=items[0],last=items.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}};document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);if(previous?.isConnected)previous.focus();};},[]);
 async function saveProfile(e){e.preventDefault();setSaving(true);setError('');setNote('');try{const updated=await api('/me/profile',{method:'PATCH',body:JSON.stringify(profile)});onUser(updated);setNote("账号资料已保存。");}catch(e){setError(e.message);}finally{setSaving(false);}}
 async function exportData(){setSaving(true);setError('');setNote('');setDownload('');try{const r=await api('/me/exports',{method:'POST',body:JSON.stringify({include_attachments:includeFiles})});setDownload(r.download_url);setNote(({message:"文件已准备好（{v0} MB），下载链接 1 小时内有效。",values:{v0:((r.size_bytes/1048576).toFixed(2))}}));}catch(e){setError(e.message);}finally{setSaving(false);}}
 const p=prefs.preferences,disabled=prefs.saving||!prefs.ready;
 return <div className="user-settings-scrim" onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}><section className="user-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="user-settings-title" ref={dialog}>
 <header><h2 id="user-settings-title">{t("系统设置")}</h2><button className="icon-button" aria-label={t("关闭系统设置")} onClick={onClose}><X size={20}/></button></header>
 <div className="user-settings-body"><nav aria-label={t("个人设置分类")}>{tabs.map(([id,label,Icon])=><button key={id} aria-current={id===tab?'page':undefined} onClick={()=>{setTab(id);setError('');setNote('');}}><Icon size={18}/>{t(label)}</button>)}{canOpenSystemManagement(user)&&onAdmin?<button type="button" onClick={()=>onAdmin(user.role==='admin'?'overview':'quota')}><Settings size={18}/>{t("系统管理")}</button>:null}</nav>
 <div className="user-settings-content">{error&&<p className="form-error" role="alert">{t(error)}</p>}{prefs.error&&<p className="form-error" role="alert">{t(prefs.error)}</p>}{note&&<p className="form-note" role="status">{t(note)}</p>}
 {tab==='general'&&<><label className="preference-row"><span>{t('界面语言')}</span><select aria-label={t('界面语言')} value={p.ui_language||'zh-CN'} disabled={disabled} onChange={e=>prefs.update({ui_language:e.target.value})}><option value="zh-CN">中文</option><option value="en">English</option></select></label><p className="settings-note-text">{t('问答与工作台共用此语言设置；PPT内容语言单独选择。')}</p>{context!=='work'&&<><h3>{t("主题")}</h3><div className="theme-choices">{[['light',t("浅色"),Sun],['dark',t("深色"),Moon],['system',t("跟随系统"),Monitor]].map(([value,label,Icon])=><button key={value} type="button" aria-pressed={p.theme===value} disabled={disabled} onClick={()=>prefs.update({theme:value})}><Icon size={22}/><span>{t(label)}</span>{p.theme===value&&<Check className="theme-check" size={14}/>}</button>)}</div>
 <label className="preference-row"><span><Type size={18}/>{t("问答字号")}</span><select aria-label={t("问答字号")} value={p.font_size} disabled={disabled} onChange={e=>prefs.update({font_size:e.target.value})}><option value="standard">{t("标准 · 18px")}</option><option value="large">{t("较大 · 20px")}</option><option value="larger">{t("大号 · 22px")}</option></select></label>
 <div className={`font-preview font-preview-${p.font_size}`}>{t("让每一次探索，都有专业的回应。")}</div>
 <label className="preference-row"><span><Keyboard size={18}/>{t("发送快捷键")}</span><select aria-label={t("发送快捷键")} value={p.send_key} disabled={disabled} onChange={e=>prefs.update({send_key:e.target.value})}><option value="enter">{t("Enter 发送")}</option><option value="ctrl_enter">{t("Ctrl / ⌘ + Enter 发送")}</option></select></label>
 <p className="settings-note-text">{p.send_key==='enter'?t("Shift + Enter 换行。"):t("Enter 换行，Ctrl + Enter 或 ⌘ + Enter 发送。")}</p>
 <div className="settings-save-line"><span role="status">{prefs.saving?t("正在保存…"):prefs.ready?t("偏好自动保存至当前账号"):t("正在加载偏好…")}</span><button className="text-button" disabled={disabled} onClick={()=>prefs.update(defaultPreferences)}>{t("恢复默认")}</button></div></>}</>}
 {tab==='work'&&<><WorkAccountSettings key={user.id} user={user} onAdmin={onAdmin} context={context} refreshKey={quotaRefresh}/><RedeemCode key={user.id} onRedeemed={()=>{if(dialog.current&&currentUser.current===user.id)setQuotaRefresh(value=>value+1);}}/></>}
 {tab==='linked'&&<LinkWorkAccount user={user} onLinked={()=>api('/me').then(onUser).catch(e=>setError(e.message))}/>}
 {tab==='account'&&<><div className="settings-profile"><span className="profile-initial">{user.name.slice(0,1)}</span><div><strong>{user.name}</strong><p>{user.email||user.legacy_alias}</p></div><span className="role-badge">{user.role==='admin'?t("系统管理员"):user.work_admin?t("工作管理员"):t("用户")}</span></div>
 <form onSubmit={saveProfile} className="profile-form"><label>{t("姓名")}<input aria-label={t("账号姓名")} required maxLength={60} value={profile.name} onChange={e=>setProfile({...profile,name:e.target.value})}/></label><label>{t("所在机构")}<input aria-label={t("账号所在机构")} required maxLength={160} value={profile.organization} onChange={e=>setProfile({...profile,organization:e.target.value})}/></label><label>{t("身份")}<select aria-label={t("账号身份")} value={profile.identity} onChange={e=>setProfile({...profile,identity:e.target.value})}>{(identities.length?identities:[user.identity]).map(i=><option key={i} value={i}>{t(i)}</option>)}</select></label><button className="secondary" disabled={saving}><Save size={16}/>{saving?t("正在保存…"):t("保存资料")}</button></form>
 <div className="settings-account-footer"><p>{t("退出登录后，后台生成任务会继续执行。")}</p><button className="secondary" onClick={onLogout}><LogOut size={16}/>{t("退出登录")}</button></div></>}
 {tab==='data'&&<><h3>{t("我的数据")}</h3>{usageError&&<p className="form-error" role="alert">{t(usageError)} <button type="button" className="text-button" onClick={loadUsage}>{t("重试")}</button></p>}{data?<><div className="personal-data-cards"><div><FileText size={18}/><strong>{data.conversations}</strong><span>{t("历史对话")}</span></div><div><HardDrive size={18}/><strong>{(data.files.bytes/1048576).toFixed(1)} <small>MB</small></strong><span>{data.files.count} {t("个附件 /")} {data.limits.storage_mb} MB</span></div></div><div className="personal-usage"><span>{t("今日提问")}</span><b>{data.today.questions} / {data.limits.quota_unlimited?t("无上限"):data.limits.daily_questions}</b><span>{t("今日已结算 tokens")}</span><b>{data.today.charged.toLocaleString(getUiLanguage())}</b><span>{t("进行中预留 tokens")}</span><b>{data.today.reserved.toLocaleString(getUiLanguage())}</b><span>{t("每日 token 额度")}</span><b>{data.limits.quota_unlimited?t("无上限"):data.limits.daily_tokens.toLocaleString(getUiLanguage())}</b></div></>:usageLoading?<p className="settings-note-text" role="status">{t("正在加载用量…")}</p>:null}
 <h3>{t("导出个人数据")}</h3><p className="settings-note-text">{t("导出你的账号资料、完整问答、引用和对话记忆，包含已移除的对话。密码与服务密钥不会导出。")}</p><label className="settings-checkbox"><input type="checkbox" checked={includeFiles} disabled={saving} onChange={e=>setIncludeFiles(e.target.checked)}/>{t("包含附件解析文本与图片")}</label><button className="secondary" disabled={saving} onClick={exportData}>{saving?<LoaderCircle size={16} className="spin"/>:<Download size={16}/>}{t("导出我的数据")}</button>{download&&<a className="export-download" href={download} download><Download size={16}/>{t("下载 JSON 文件")}</a>}</>}
 {tab==='about'&&<><div className="settings-about-brand"><Logo/><h3>{context==='work'?t("无隅工作"):t("无隅问答")}</h3></div><p className="settings-note-text">{context==='work'?t("固废与资源循环领域的专业工作平台"):t("固废与资源循环领域的专家问答")}</p><div className="about-note"><h4>{t("模型与资料")}</h4><p>{t("提问和所选附件会发送至当前选择的模型服务。开启联网搜索后，模型可按需检索网页或学术资料。")}</p><h4>{t("使用提示")}</h4><p>{t("内容由 AI 生成，请仔细甄别。重要事实与引用请结合原始来源核实。")}</p></div></>}
 </div></div></section></div>;
}
