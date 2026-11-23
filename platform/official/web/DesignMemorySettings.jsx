import {t,getUiLanguage} from './i18n.mjs';
import React,{useState,useEffect,useRef} from 'react';
import {Save,RefreshCw,Play,Check,RotateCcw} from 'lucide-react';
import {api} from './api';

const numbers={interval_hours:['自动凝练间隔（小时）',1,720],system_min_users:['每条系统规则最少用户数',3,1000],system_min_feedback:['每条系统规则最少有效反馈数',3,10000],system_daily_points:['系统每日凝练积分上限',1,1000],personal_min_feedback:['个人自动凝练最少新增反馈',2,1000],personal_min_projects:['个人规则最少已确认项目数',2,100],personal_daily_points:['每人每日凝练积分上限',1,100],max_feedback_per_run:['单次最大读取反馈数',10,5000],retrieval_limit:['每次最多引用设计规则数',1,12],habit_learning_idle_minutes:['项目空闲多少分钟后整理修改意见',5,1440]};
const names={candidate:'待审核',active:'使用中',superseded:'历史版本',disabled:'已停用',running:'凝练中',completed:'已完成',failed:'失败'};

export default function DesignMemorySettings(){
 const [data,setData]=useState(null),[settings,setSettings]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState('');
 const compileRequest=useRef(null);
 const receive=value=>{setData(value);setSettings(value.settings);};
 const load=()=>api('/admin/work/design-memory').then(receive).catch(e=>setError(e.message));
 useEffect(()=>{let live=true;api('/admin/work/design-memory').then(value=>{if(live)receive(value);}).catch(e=>{if(live)setError(e.message);});return()=>{live=false;};},[]);
 async function perform(action){setBusy(true);setError('');setNote('');try{await action();}catch(e){setError(e.message);}finally{setBusy(false);}}
 const toggle=(key,label)=><label className="admin-switch"><input type="checkbox" checked={settings[key]} disabled={busy} onChange={e=>setSettings({...settings,[key]:e.target.checked})}/>{t(label)}</label>;
 // op195: up to three library templates recommended per deck type; empty slots are dropped on save.
 const mapping=(settings&&settings.template_by_deck_type)||{};
 const setSlot=(type,index,value)=>{const list=[...(mapping[type]||[])];list[index]=value;const clean=list.filter(Boolean).filter((v,i,a)=>a.indexOf(v)===i);const next={...mapping};if(clean.length)next[type]=clean;else delete next[type];setSettings({...settings,template_by_deck_type:next});};
 return <section className="admin-panel"><div className="admin-panel-title"><h2>{t("设计记忆与规则")}</h2><button className="secondary" disabled={busy} onClick={()=>perform(load)}><RefreshCw size={16}/>{t("刷新")}</button></div>
 {error?<p role="alert" className="form-error">{t(error)}</p>:null}{note?<p role="status" className="form-note">{t(note)}</p>:null}
 {!data||!settings?<p>{t("正在读取…")}</p>:<>
 <p className="admin-hint">{t("此管理项仅管理员可见。个人修改原文与模板按用户隔离；系统凝练仅向 DeepSeek 提供匿名设计信号及支持数量。材料内容、用户身份和项目数值不进入跨用户凝练。")}</p>
 <form onSubmit={e=>{e.preventDefault();perform(async()=>{receive(await api('/admin/work/design-memory',{method:'PUT',body:JSON.stringify({expected_revision:data.revision,settings})}));setNote("设置已保存。自动生成的系统规则仍须审核启用。");});}}>
 {toggle('capture_enabled',t("记录用户实际设计修改"))}{toggle('personal_enabled',t("生成时参考用户自己的设计记忆"))}{toggle('personal_auto_compile',t("积累足够已确认反馈后自动凝练个人设计规则"))}
 {toggle('system_enabled',t("启用经审核的系统设计规则"))}{toggle('system_periodic',t("定期调用 DeepSeek 凝练系统候选规则"))}
 {toggle('habit_learning_enabled',t("从用户的修改意见中自动学习设计习惯（同一偏好出现在2个项目或用户说“以后都”才生效；扣用户积分）"))}
 <div className="admin-fields">{Object.entries(numbers).map(([key,[label,min,max]])=><label key={key}>{t(label)}<input type="number" required min={min} max={max} step="1" value={settings[key]} disabled={busy} onChange={e=>setSettings({...settings,[key]:Number(e.target.value)})}/></label>)}</div>
 <p className="admin-hint">{t("预算计入发起管理员或用户的现有积分，不额外扣 PPT 页数。失败或超时不自动重试；手动重试保留原请求，避免重复调用。当前任务要求优先于历史偏好。")}</p>
 {data.deck_types?.length?<><h3>{t("按PPT类型推荐模板")}</h3>
 <p className="admin-hint">{t("为每类PPT指定最多3个模板库模板。用户选模板时排在其个人同类历史之后、自己上传的模板之前，并显示“推荐用于该类PPT”。不指定则按原有方式推荐。")}</p>
 <div className="table-wrap"><table><thead><tr><th>{t("PPT类型")}</th><th>{t("推荐模板（按顺序）")}</th></tr></thead><tbody>{data.deck_types.map(type=><tr key={type.value}><td>{t(type.label)}</td><td>{[0,1,2].map(index=>{const list=mapping[type.value]||[];return <select key={index} aria-label={t(type.label)+' '+(index+1)} value={list[index]||''} disabled={busy||index>list.length} onChange={e=>setSlot(type.value,index,e.target.value)}><option value="">{t("不指定")}</option>{(data.template_options||[]).map(option=><option key={option.id} value={option.id}>{option.title}</option>)}</select>;})}</td></tr>)}</tbody></table></div></>:null}
 <button className="primary" disabled={busy}><Save size={16}/>{t("保存设置")}</button></form>
 <h3>{t("本次可凝练的匿名信号")}</h3><p className="admin-hint">{t("已确认反馈")} {data.preview?.supported_feedback??0} {t("条，覆盖")} {data.preview?.supported_projects??0} {t("个项目。仅展示达到多人支持门槛的规则。")}</p>
 <div className="table-wrap"><table><thead><tr><th>{t("设计规则")}</th><th>{t("有效反馈")}</th><th>{t("用户数")}</th><th>{t("项目数")}</th></tr></thead><tbody>{(data.preview?.eligible_rules||[]).map(rule=><tr key={rule.id}><td>{rule.rule}</td><td>{rule.feedback_count}</td><td>{rule.user_count}</td><td>{rule.project_count}</td></tr>)}</tbody></table></div>
 <button className="secondary" disabled={busy||!settings.system_enabled||!data.preview?.eligible_rules?.length} onClick={()=>perform(async()=>{compileRequest.current??=crypto.randomUUID();const run=await api('/admin/work/design-memory/compile',{method:'POST',body:JSON.stringify({request_id:compileRequest.current})});if(run.status!=='running')compileRequest.current=null;await load();setNote(run.status==='completed'?"候选规则已生成，审核后才会生效。":run.status==='running'?"正在凝练，稍后刷新查看。":"此次凝练失败；请查看运行记录，未自动重试。");})}><Play size={16}/>{t("凝练候选规则")}</button>
 <h3>{t("规则版本与审核")}</h3>{!data.rule_sets?.length?<p className="admin-hint">{t("尚无系统规则版本。")}</p>:data.rule_sets.map(version=><article className="admin-panel" key={version.id}><div className="admin-panel-title"><strong>{t(names[version.status]||version.status)} · {new Date(version.created_at).toLocaleString(getUiLanguage())}</strong><div className="integration-actions"><button className="secondary" disabled={busy||version.status==='active'} onClick={()=>perform(async()=>{receive(await api('/admin/work/design-memory/review',{method:'POST',body:JSON.stringify({rule_set_id:version.id,action:'activate'})}));setNote("此版本已启用，旧版本保留，可随时回退。");})}>{version.status==='candidate'?<Check size={15}/>:<RotateCcw size={15}/>}{version.status==='candidate'?t("审核并启用"):t("恢复此版本")}</button><button className="text-button" disabled={busy||version.status==='disabled'} onClick={()=>perform(async()=>receive(await api('/admin/work/design-memory/review',{method:'POST',body:JSON.stringify({rule_set_id:version.id,action:'disable'})})))}>{t("停用")}</button></div></div><ul>{version.rules.map(rule=><li key={rule.id}>{rule.rule}</li>)}</ul></article>)}
 <h3>{t("最近凝练记录")}</h3><div className="table-wrap"><table><thead><tr><th>{t("时间")}</th><th>{t("状态")}</th><th>{t("积分预留上限")}</th></tr></thead><tbody>{(data.runs||[]).map(run=><tr key={run.id}><td>{new Date(run.created_at).toLocaleString(getUiLanguage())}</td><td>{t(names[run.status]||run.status)}</td><td>{run.points_upper}</td></tr>)}</tbody></table></div>
 </>}
 </section>;
}
