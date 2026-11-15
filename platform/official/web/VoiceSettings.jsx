import {t,getUiLanguage} from './i18n.mjs';
import React,{useEffect,useState} from 'react';
import {Mic,Save} from 'lucide-react';
import {api} from './api';
export default function VoiceSettings({onSaved}){
 const [cfg,setCfg]=useState(null),[note,setNote]=useState(''),[busy,setBusy]=useState(false);
 const load=()=>api('/admin/voice').then(setCfg).catch(e=>setNote(e.message));useEffect(()=>{void load();},[]);
 if(!cfg)return <p>{note||t("正在加载语音配置…")}</p>;
 const omni=cfg.provider==='qwen_omni';
 const credentials=omni?[['omni_api_key','DMXAPI Key','omni']]:[['app_id','App ID','app_id'],['api_key','API Key','api_key'],['api_secret','API Secret','api_secret']];
 return <form className="admin-form" onSubmit={async e=>{e.preventDefault();setBusy(true);try{const {configured,usage,...body}=cfg;await api('/admin/voice',{method:'PUT',body:JSON.stringify(body)});await load();setNote("语音配置已保存。");onSaved();}catch(e){setNote(e.message);}finally{setBusy(false);}}}>
  <section className="admin-panel">
   <h2><Mic size={20}/>{t("语音输入")}</h2>
   <p className="admin-hint">{t("录音转成文字填入输入框，核对后发送；问答继续使用工作台中选定的模型。")}</p>
   <label className="admin-switch"><input type="checkbox" checked={cfg.enabled} onChange={e=>setCfg({...cfg,enabled:e.target.checked})}/>{t("启用语音输入")}</label>
   <div className="admin-fields">
    <label>{t("语音识别模型")}<select value={cfg.provider||'xfyun'} onChange={e=>setCfg({...cfg,provider:e.target.value})}><option value="qwen_omni">Qwen3.8 Omni（DMXAPI）</option><option value="xfyun">{t("讯飞实时语音识别")}</option></select><small>{omni?t("录音完成后由 qwen3.8-omni-flash 转写。"):t("录音过程中实时显示识别文字。")}</small></label>
    {credentials.map(([key,label,configured])=><label key={key}>{t(label)}<input type="password" autoComplete="new-password" value={cfg[key]||''} onChange={e=>setCfg({...cfg,[key]:e.target.value})}/><small>{cfg.configured[configured]?t("已配置 · 留空保持不变"):t("尚未配置")}</small></label>)}
    {[['max_seconds',t("单次录音时长（秒）"),10,120],['concurrency',t("同时语音会话数"),1,8],['daily_sessions',t("每人 24 小时内最多录音次数"),1,1000]].map(([key,label,min,max])=><label key={key}>{t(label)}<input type="number" min={min} max={max} value={cfg[key]} onChange={e=>setCfg({...cfg,[key]:Number(e.target.value)})}/></label>)}
   </div>
   <p className="admin-hint">{omni?t("录音通过服务器提交给 DMXAPI 进行语音转写。"):t("录音通过服务器提交给讯飞进行实时转写。")}{t("音频不写入数据库，仅记录调用时长和状态。")}</p>
   <p className="admin-hint">{t("浏览器录音需要 HTTPS 或 localhost，网站需将 /api/voice/stream 的 WebSocket 请求转发到后端。")}</p>
   <p className="admin-hint">{t("本站累计")} {cfg.usage.calls} {t("次调用，提交音频")} {Math.round(cfg.usage.seconds)} {t("秒；实际额度以")}{omni?' DMXAPI':t("讯飞")}{t("控制台为准。")}</p>
  </section>
  {note&&<p role="status" className="form-note">{t(note)}</p>}
  <button className="primary" disabled={busy}><Save size={16}/>{busy?t("正在保存…"):t("保存语音配置")}</button>
 </form>;
}
