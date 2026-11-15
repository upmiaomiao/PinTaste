import {t,getUiLanguage} from './i18n.mjs';
import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
const labels={daily_points:'每日赠送',signup_points:'注册赠送',standard_page_points:'标准页估算积分',image_points:'生图每次',trace_points:'描摹每页',input_tokens_per_point:'输入Token/积分',output_tokens_per_point:'输出Token/积分',search_points:'搜索每次',fetch_points:'网页读取每次',scholar_points:'学术检索每次',wechat_search_points:'公众号搜索每次',wechat_fetch_points:'公众号读取每次',voice_points:'工作语音每次（最长120秒）'};
const kinds={image:'生图',trace:'描摹',text:'文字与模型',search:'搜索',fetch:'网页读取',scholar:'学术检索',wechat_search:'公众号搜索',wechat_fetch:'公众号读取',voice:'语音'};
const events={grant:'每日赠送',signup:'注册赠送',reserve:'预占',settle:'结算',refund:'返还',adjustment:'管理员调整',migration:'迁移',redeem:'兑换码'};
const reasons={IDENTITY_DISABLED:'账号已停用',WORK_QUOTA_ADMIN_REQUIRED:'你的账号没有积分管理权限',QUOTA_ADJUSTMENT_INSUFFICIENT:'可扣回的长期积分不足',WORK_BRIDGE_UNAVAILABLE:'工作服务暂时连不上（可能正在重启），请稍后点重试',WORK_BRIDGE_REJECTED:'工作服务拒绝了请求'};
const splitTargets=text=>[...new Set(text.split(/[\n,，;；\s]+/).map(x=>x.trim()).filter(Boolean))].slice(0,200);

/** Quick grants, one-use codes, and the full wallet/rules view, over one server ledger. */
export default function PointsConsole(){
 const [tab,setTab]=useState('grant');
 const [users,setUsers]=useState([]),[query,setQuery]=useState(''),[overview,setOverview]=useState(null),[rules,setRules]=useState(null);
 const [balance,setBalance]=useState(null),[ledger,setLedger]=useState(null),[adjust,setAdjustment]=useState(''),[daily,setDaily]=useState(''),[frozen,setFrozen]=useState(false);
 const [reason,setReason]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState(''),[retry,setRetry]=useState(false);
 // quick grant
 const [targets,setTargets]=useState(''),[grantPoints,setGrantPoints]=useState('600'),[grantReason,setGrantReason]=useState(''),[preview,setPreview]=useState(null),[grantResult,setGrantResult]=useState(null);
 // codes
 const [codeCount,setCodeCount]=useState('10'),[codePoints,setCodePoints]=useState('600'),[codeExpiry,setCodeExpiry]=useState(''),[codeNote,setCodeNote]=useState(''),[fresh,setFresh]=useState(null),[batches,setBatches]=useState([]);
 const pending=useRef(null),grantTask=useRef(null);
 async function load(){const [directory,state]=await Promise.all([api('/admin/work/quota/users'),api('/admin/work/quota/overview')]);setUsers(directory.users);setOverview(state);setRules(state.policy.settings);}
 async function loadBatches(){const value=await api('/admin/work/quota/command',{method:'POST',body:JSON.stringify({action:'codes_list'})});setBatches(value.batches||[]);}
 useEffect(()=>{load().catch(e=>setError(e.message));},[]);
 useEffect(()=>{if(tab==='codes')loadBatches().catch(e=>setError(e.message));},[tab]);
 // Live preview: who the pasted text will reach. No points move until "加积分".
 useEffect(()=>{const list=splitTargets(targets);setGrantResult(null);if(!list.length){setPreview(null);return;}
  const timer=setTimeout(()=>{api('/admin/work/quota/resolve',{method:'POST',body:JSON.stringify({targets:list})}).then(setPreview).catch(()=>setPreview(null));},350);
  return()=>clearTimeout(timer);},[targets]);
 async function wallet(user,offset=0){const [value,history]=await Promise.all([api('/admin/work/quota/users/'+encodeURIComponent(user.id)),api('/admin/work/quota/command',{method:'POST',body:JSON.stringify({action:'ledger',user_id:user.id,limit:25,offset})})]);setBalance({user,...value});setLedger(history);setDaily(value.control.daily_points_override??'');setFrozen(value.control.spending_frozen);}
 async function read(fn){setBusy(true);setError('');try{await fn();}catch(e){setError(e.message);}finally{setBusy(false);}}
 async function mutate(payload,{needsReason=true}={}){
  if(busy)return;if(needsReason&&!pending.current&&!reason.trim()){setError("请填写本次修改的原因，系统将保留审计记录。");return;}
  const task=pending.current??{path:'/admin/work/quota/command',body:{...payload,reason:(reason.trim()||'管理员调整'),idempotency_key:crypto.randomUUID()}};pending.current=task;setBusy(true);setError('');setNote('');
  let accepted=false;
  try{await api(task.path,{method:'POST',body:JSON.stringify(task.body)});
   accepted=true;pending.current=null;setRetry(false);setNote("已保存并留痕。");await load();if(balance)await wallet(balance.user);
  }catch(e){if(accepted){setError("操作已保存，余额刷新暂未完成；请点击刷新查看最新结果。");}else if(e.status>=400&&e.status<500){pending.current=null;setRetry(false);setError(e.status===409?"版本或余额已变化，请刷新核对后重新提交。":e.message);}else{setRetry(true);setError("结果尚未确认（工作服务可能正在重启）。请点“重试”，系统会避免重复入账。");}}
  finally{setBusy(false);}
 }
 async function grant(){
  const list=splitTargets(targets),amount=Number(grantPoints);
  if(busy||!list.length||!Number.isInteger(amount)||!amount)return;
  // The same click retried after a network problem keeps its key, so nobody is credited twice.
  const task=grantTask.current&&grantTask.current.fingerprint===JSON.stringify([list,amount,grantReason.trim()])?grantTask.current
   :{fingerprint:JSON.stringify([list,amount,grantReason.trim()]),body:{targets:list,points:amount,reason:grantReason.trim(),idempotency_key:crypto.randomUUID()}};
  grantTask.current=task;setBusy(true);setError('');setNote('');
  for(let attempt=0;attempt<2;attempt++){
   try{const result=await api('/admin/work/quota/grant-by-email',{method:'POST',body:JSON.stringify(task.body)});setGrantResult(result);
    if(result.results.every(r=>r.success))grantTask.current=null;
    const ok=result.results.filter(r=>r.success).length;setNote(({message:amount>0?"已给 {v0} 人加 {v1} 积分。":"已给 {v0} 人扣 {v1} 积分。",values:{v0:ok,v1:Math.abs(amount)}}));break;}
   catch(e){if(e.status>=500&&attempt===0){await new Promise(r=>setTimeout(r,1500));continue;}
    setError(e.status>=500?"工作服务暂时连不上（可能正在重启），请稍后再点一次；已成功的不会重复。":e.message);}
  }
  setBusy(false);
 }
 async function createCodes(){
  if(busy)return;setBusy(true);setError('');setNote('');
  try{const value=await api('/admin/work/quota/command',{method:'POST',body:JSON.stringify({action:'codes_create',count:Number(codeCount),points:Number(codePoints),...(codeExpiry?{expires_at:codeExpiry}:{}),note:codeNote.trim()})});
   setFresh(value);await loadBatches();}catch(e){setError(e.message);}finally{setBusy(false);}
 }
 async function revoke(batch){if(busy||!window.confirm(t("作废这一批中尚未使用的 {v0} 个兑换码？已兑换的不受影响。", {v0:(batch.unused)})))return;
  await read(async()=>{await api('/admin/work/quota/command',{method:'POST',body:JSON.stringify({action:'codes_revoke',batch_id:batch.batch_id})});await loadBatches();});}
 function downloadCodes(){if(!fresh)return;const rows=[[t("兑换码"),t("积分"),t("失效时间"),t("备注")],...fresh.codes.map(c=>[c,fresh.points,fresh.expires_at||t("长期有效"),fresh.note])];
  const blob=new Blob(['﻿'+rows.map(r=>r.map(x=>'"'+String(x).replace(/"/g,'""')+'"').join(',')).join('\n')],{type:'text/csv;charset=utf-8'});
  const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=t("无隅兑换码-")+fresh.batch_id.slice(0,8)+'.csv';link.click();URL.revokeObjectURL(link.href);}
 const locked=busy||retry,shown=users.filter(u=>[u.name,u.email,u.legacy_alias,u.organization].filter(Boolean).join(' ').toLowerCase().includes(query.toLowerCase()));
 const tabs=[['grant',t("快速加积分")],['codes',t("兑换码")],['users',t("用户与规则")]];
 return <section className="admin-panel work-quota-admin"><h2>{t("积分与用量管理")}</h2>
 <nav className="integrated-user-toolbar" role="tablist" aria-label={t("积分管理")}>{tabs.map(([key,label])=><button key={key} role="tab" aria-selected={tab===key} className={tab===key?'primary':'secondary'} onClick={()=>{setTab(key);setError('');setNote('');}}>{t(label)}</button>)}</nav>
 {error?<p className="form-error" role="alert">{t(error)}</p>:null}{note?<p className="form-note" role="status">{t(note)}</p>:null}

 {tab==='grant'?<form className="quota-grant-form" onSubmit={e=>{e.preventDefault();grant();}}>
  <label>{t("给谁加（邮箱、姓名或原工作账号；可粘贴多个，逗号或换行分隔）")}<textarea aria-label={t("加积分对象")} rows={4} value={targets} disabled={busy} placeholder={'zhang@example.com\nli@example.com'} onChange={e=>setTargets(e.target.value)}/></label>
  {preview?<p className="admin-hint" role="status">{t("已找到")} {preview.found.length} {t("人")}{preview.found.length?'：'+preview.found.map(x=>x.name+(x.active?'':t("（已停用）"))).join('、'):''}{preview.missing.length?t("；未找到：{v0}", {v0:(preview.missing.join('、'))}):''}{preview.ambiguous.length?t("；有重名，请改用邮箱：{v0}", {v0:(preview.ambiguous.join('、'))}):''}</p>:null}
  <label>{t("每人积分（负数为扣回长期积分）")}<input type="number" aria-label={t("每人积分")} min="-10000000" max="10000000" step="1" required disabled={busy} value={grantPoints} onChange={e=>setGrantPoints(e.target.value)}/></label>
  <label>{t("原因（可不填，默认“管理员赠送”）")}<input aria-label={t("加积分原因")} maxLength={200} disabled={busy} value={grantReason} placeholder={t("内测邀请、售后补偿等")} onChange={e=>setGrantReason(e.target.value)}/></label>
  <button className="primary" disabled={busy||!preview?.found.length||!Number(grantPoints)}>{Number(grantPoints)<0?t("扣回积分"):t("加积分")}{preview?.found.length?t("（{v0} 人）", {v0:(preview.found.length)}):''}</button>
  <p className="admin-hint">{t("加的是长期积分，不过期，优先级在每日赠送之后。每日赠额北京时间零点重置、不累加。")}</p>
  {grantResult?<div className="table-wrap"><table><thead><tr><th>{t("对象")}</th><th>{t("结果")}</th><th>{t("当前可用")}</th></tr></thead><tbody>{grantResult.results.map(r=><tr key={r.input}><td>{r.name}<small>{r.email||r.input}</small></td><td>{r.success?t("已到账"):t(r.message||reasons[r.code]||r.code)}</td><td>{r.success?r.available_points??'—':'—'}</td></tr>)}
   {grantResult.missing.map(x=><tr key={'m'+x}><td>{x}</td><td>{t("未找到这个用户")}</td><td>—</td></tr>)}{grantResult.ambiguous.map(x=><tr key={'a'+x}><td>{x}</td><td>{t("有重名，请改用邮箱")}</td><td>—</td></tr>)}</tbody></table></div>:null}
 </form>:null}

 {tab==='codes'?<>
  <form className="quota-grant-form" onSubmit={e=>{e.preventDefault();createCodes();}}><h3>{t("生成兑换码")}</h3>
   <label>{t("数量（一次最多500个）")}<input type="number" aria-label={t("兑换码数量")} min="1" max="500" step="1" required disabled={busy} value={codeCount} onChange={e=>setCodeCount(e.target.value)}/></label>
   <label>{t("每个码的积分")}<input type="number" aria-label={t("每个码的积分")} min="1" max="100000" step="1" required disabled={busy} value={codePoints} onChange={e=>setCodePoints(e.target.value)}/></label>
   <label>{t("失效日期（可不填，不填为长期有效）")}<input type="date" aria-label={t("失效日期")} disabled={busy} value={codeExpiry} onChange={e=>setCodeExpiry(e.target.value)}/></label>
   <label>{t("备注（用户兑换后在流水里看到）")}<input aria-label={t("兑换码备注")} maxLength={200} disabled={busy} value={codeNote} placeholder={t("如：10月培训班")} onChange={e=>setCodeNote(e.target.value)}/></label>
   <button className="primary" disabled={busy}>{t("生成")}</button>
   <p className="admin-hint">{t("每个码只能被一个人用一次。用户在无隅工作右上角积分面板或个人设置里输入兑换码。")}</p></form>
  {fresh?<section aria-label={t("新生成的兑换码")}><h3>{t("新生成")} {fresh.codes.length} {t("个 · 每个")} {fresh.points} {t("积分")}{fresh.expires_at?t(" · {v0} 前有效", {v0:(new Date(fresh.expires_at).toLocaleDateString(getUiLanguage()))}):t(" · 长期有效")}</h3>
   <p className="form-error">{t("兑换码只在此时显示一次，请立即复制或下载保存。")}</p>
   <textarea aria-label={t("兑换码列表")} readOnly rows={Math.min(12,fresh.codes.length+1)} value={fresh.codes.join('\n')} onFocus={e=>e.target.select()}/>
   <div className="integrated-user-toolbar"><button className="secondary" onClick={()=>navigator.clipboard?.writeText(fresh.codes.join('\n')).then(()=>setNote("已复制到剪贴板。"))}>{t("复制全部")}</button><button className="secondary" onClick={downloadCodes}>{t("下载 CSV")}</button><button className="secondary" onClick={()=>setFresh(null)}>{t("我已保存，隐藏")}</button></div></section>:null}
  <h3>{t("历史批次")}</h3><div className="table-wrap"><table><thead><tr><th>{t("生成时间")}</th><th>{t("每码积分")}</th><th>{t("备注")}</th><th>{t("已兑换 / 未用 / 过期 / 作废")}</th><th>{t("失效")}</th><th></th></tr></thead><tbody>
   {batches.length?batches.map(b=><tr key={b.batch_id}><td>{new Date(b.created_at).toLocaleString(getUiLanguage())}</td><td>{b.points}</td><td>{b.note||'—'}</td><td>{b.redeemed} / {b.unused} / {b.expired} / {b.revoked}{t("（共")} {b.total}）</td><td>{b.expires_at?new Date(b.expires_at).toLocaleDateString(getUiLanguage()):t("长期")}</td><td>{b.unused?<button className="text-button" disabled={busy} onClick={()=>revoke(b)}>{t("作废未用")}</button>:null}</td></tr>)
   :<tr><td colSpan={6}>{t("还没有生成过兑换码。")}</td></tr>}</tbody></table></div>
 </>:null}

 {tab==='users'?<>
  <p className="admin-hint">{t("工作能力统一扣积分。每日赠额北京时间零点重置、不累加、优先使用；注册、管理员赠送与兑换码积分长期保留。")}</p>
  <div className="personal-data-cards"><div><strong>{overview?.usage.reduce((sum,x)=>sum+x.points,0)??0}</strong><span>{t("今日实际扣除积分")}</span></div><div><strong>{overview?.reserved_points??'—'}</strong><span>{t("预占积分 ·")} {overview?.pending_reservations??0} {t("笔")}</span></div></div>
  <p className="admin-hint">{overview?.usage.map(x=>t("{v0} {v1}次 / {v2}积分", {v0:(t(kinds[x.kind]||x.kind)), v1:(x.calls), v2:(x.points)})).join('；')||t("今日暂无已结算用量。")}</p>
  <label>{t("修改原因（仅修改规则、暂停消费或个人每日赠额时需要）")}<input aria-label={t("积分操作原因")} value={reason} disabled={locked} maxLength={200} placeholder={t("规则调整、试用到期等")} onChange={e=>setReason(e.target.value)}/></label>
  {retry?<button className="primary" disabled={busy} onClick={()=>mutate(null)}>{t("重试")}</button>:null}
  <details><summary>{t("积分规则 · 版本")} {overview?.policy.revision??'—'}</summary><p className="admin-hint">{t("标准页仅作估算，复杂材料、修改和深度探索增加消耗。文字输入、输出分别向上取整，最少1积分。修改注册赠额只影响未领取的账号。")}</p>
  <form className="quota-grant-form" onSubmit={e=>{e.preventDefault();mutate({action:'policy',settings:rules,expected_revision:overview.policy.revision});}}>{rules?Object.entries(labels).map(([key,label])=><label key={key}>{t(label)}<input type="number" required min={['daily_points','signup_points'].includes(key)?0:1} max="10000000" step="1" disabled={locked} value={rules[key]} onChange={e=>setRules(old=>({...old,[key]:Number(e.target.value)}))}/></label>):null}<button className="primary" disabled={locked||!rules}>{t("保存积分规则")}</button></form></details>
  <h3>{t("用户积分")}</h3><div className="integrated-user-toolbar"><input type="search" aria-label={t("搜索积分用户")} placeholder={t("姓名、账号或机构")} value={query} onChange={e=>setQuery(e.target.value)}/><button className="secondary" disabled={locked} onClick={()=>read(load)}>{t("刷新")}</button></div>
  <div className="table-wrap"><table><thead><tr><th>{t("用户")}</th><th>{t("机构")}</th><th>{t("积分")}</th></tr></thead><tbody>{shown.map(user=><tr key={user.id}><td>{user.name}<small>{user.email||user.legacy_alias}{!user.active?t(" · 已停用"):''}</small></td><td>{user.organization}</td><td><button className="text-button" disabled={locked||!user.active} onClick={()=>read(()=>wallet(user))}>{t("余额与流水")}</button></td></tr>)}</tbody></table></div>
  {balance?<section aria-label={t("用户积分详情")}><h3>{balance.user.name} {t("· 积分钱包")}</h3><p>{t("可用")} {balance.available.points} {t("积分：当日")} {balance.daily.points.available} {t("+ 长期")} {balance.bonus.points.available}{t("；预占")} {balance.reserved.points}{t("。下次刷新：")}{new Date(balance.reset_at).toLocaleString(getUiLanguage())}。</p>
  <form className="quota-grant-form" onSubmit={e=>{e.preventDefault();if(Number(adjust))mutate({action:'adjustment',user_id:balance.user.id,points:Number(adjust)},{needsReason:false});}}><label>{t("长期积分调整（正数赠送，负数扣回）")}<input type="number" min="-10000000" max="10000000" step="1" required disabled={locked} value={adjust} onChange={e=>setAdjustment(e.target.value)}/></label><button className="secondary" disabled={locked||!Number(adjust)}>{t("提交调整")}</button><p className="admin-hint">{t("只能扣回未使用的长期积分，不动用每日积分或任务预占。")}</p></form>
  <form className="quota-grant-form" onSubmit={e=>{e.preventDefault();mutate({action:'control',user_id:balance.user.id,settings:{spending_frozen:frozen,daily_points_override:daily===''?null:Number(daily)},expected_revision:balance.control.revision});}}><label><input type="checkbox" checked={frozen} disabled={locked} onChange={e=>setFrozen(e.target.checked)}/>{t("暂停此用户的新积分消费")}</label><label>{t("个人每日赠额（留空跟随全局，0为不赠送，-1为不限额度）")}<input type="number" min="-1" max="10000000" step="1" disabled={locked} value={daily} onChange={e=>setDaily(e.target.value)}/></label><button className="secondary" disabled={locked}>{t("保存用户积分规则")}</button></form>
  <h4>{t("积分流水 · 共")} {ledger?.total??0} {t("条")}</h4><div className="table-wrap"><table><thead><tr><th>{t("时间")}</th><th>{t("事项")}</th><th>{t("余额变动")}</th><th>{t("实际扣除")}</th><th>{t("原因 / 预占记录")}</th></tr></thead><tbody>{ledger?.entries.map(row=><tr key={row.id}><td>{new Date(row.created_at).toLocaleString(getUiLanguage())}</td><td>{t(events[row.event]||row.event)} {t(kinds[row.kind]||'')}</td><td>{row.points>0?'+':''}{row.points}</td><td>{row.charged_points||'—'}</td><td>{row.reason}<small>{row.reservation_id||row.id}</small></td></tr>)}</tbody></table></div>
  <button className="secondary" disabled={locked||!ledger?.offset} onClick={()=>read(()=>wallet(balance.user,Math.max(0,ledger.offset-25)))}>{t("上一页")}</button><button className="secondary" disabled={locked||!ledger||ledger.offset+25>=ledger.total} onClick={()=>read(()=>wallet(balance.user,ledger.offset+25))}>{t("下一页")}</button></section>:null}
  <details><summary>{t("规则与用户控制审计（最近50条）")}</summary><div className="table-wrap"><table><thead><tr><th>{t("时间")}</th><th>{t("操作 / 原因")}</th><th>{t("变更")}</th><th>{t("操作者")}</th></tr></thead><tbody>{overview?.audit.map(row=><tr key={row.sequence}><td>{new Date(row.created_at).toLocaleString(getUiLanguage())}</td><td>{row.action==='policy'?t("全局积分规则"):t("个人积分规则")}<small>{row.reason}</small></td><td>{Object.entries(row.after.settings).filter(([key,v])=>row.before.settings[key]!==v).map(([key,v])=>t("{v0}：{v1} → {v2}", {v0:(t(labels[key])||({spending_frozen:t("暂停消费"),daily_points_override:t("个人每日赠额")}[key])), v1:(String(row.before.settings[key]??t("默认"))), v2:(String(v??t("默认")))})).join('；')}</td><td>{users.find(u=>u.work_user_id===row.actor_user_id)?.name||row.actor_user_id}</td></tr>)}</tbody></table></div></details>
 </>:null}
 </section>;
}
