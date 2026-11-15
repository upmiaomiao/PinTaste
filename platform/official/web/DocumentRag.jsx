import {t} from './i18n.mjs';
import React,{useEffect,useState} from 'react';
import {FileSearch,Save,ChevronDown,ExternalLink} from 'lucide-react';
import {api} from './api';
import {documentSourcesForTurn,hasDocumentRetrieval} from './documentEvidence.mjs';

export function DocumentSources({turn}){
 const sources=documentSourcesForTurn(turn);
 if(turn.documentRag?.phase==='retrieving'&&hasDocumentRetrieval(turn))return <p className="document-rag-progress"><FileSearch size={16}/>{t("正在查找附件中与问题相关的原文…")}</p>;
 if(!sources.length)return null;
 return <details className="document-evidence"><summary><FileSearch size={16}/><strong>{t("附件原文依据")}</strong><span>{t("本轮选取 ")}{sources.length} {t(" 处")}</span><ChevronDown size={15}/></summary>
  <p className="document-evidence-note">{t("依据本轮问题检索相关内容。这里列出实际选取的原文，可展开核对。")}</p>
  {sources.map(source=><section id={`document-${turn.id}-${source.source_id}`} className="document-evidence-item" key={source.source_id}>
   <b>{source.source_id} · {source.title}{source.page?t(" · 第 {v0} 页", {v0: source.page}):''}{source.section?` · ${source.section}`:''}</b>
   <pre>{source.text}</pre>
   {source.ocr&&<small>{t("文字来自页面识别，数值和符号请与原页核对。")}</small>}
   {source.page&&<a href={`/api/files/${source.file_id}/pages/${source.page}`} target="_blank" rel="noopener noreferrer">{t("查看原页")}<ExternalLink size={12}/></a>}
  </section>)}
 </details>;
}

export default function DocumentRagSettings({onSaved}){
 const [cfg,setCfg]=useState(null),[note,setNote]=useState(''),[busy,setBusy]=useState(false);
 const load=()=>api('/admin/rag').then(setCfg).catch(error=>setNote(error.message));
 useEffect(()=>{void load();},[]);
 if(!cfg)return <p>{note||t("正在加载文档检索配置…")}</p>;
 async function save(event){event.preventDefault();setBusy(true);try{const {key_configured,...body}=cfg;await api('/admin/rag',{method:'PUT',body:JSON.stringify(body)});await load();setNote(t("文档检索配置已保存。"));onSaved();}catch(error){setNote(error.message);}finally{setBusy(false);}}
 return <form className="admin-form" onSubmit={save}><section className="admin-panel">
  <h2><FileSearch size={20}/>{t("文档定向检索")}</h2>
  <p className="admin-hint">{t("上传文件建立全文索引，按用户问题选择相关原文交给当前问答模型，回答显示页码、章节和依据。")}</p>
  <label className="admin-switch"><input type="checkbox" checked={cfg.enabled} onChange={event=>setCfg({...cfg,enabled:event.target.checked})}/>{t("启用文档检索")}</label>
  <label className="admin-switch"><input type="checkbox" checked={cfg.semantic_enabled} onChange={event=>setCfg({...cfg,semantic_enabled:event.target.checked})}/>{t("启用 WeMM 语义检索")}</label>
  <label className="admin-switch"><input type="checkbox" checked={cfg.visual_enabled} onChange={event=>setCfg({...cfg,visual_enabled:event.target.checked})}/>{t("检索 PDF 页面，选中页面尝试识别文字")}</label>
  <div className="admin-fields">
   {[['base_url',t("嵌入服务 API Base URL"),'text'],['model',t("嵌入模型接口名称"),'text'],['api_key',t("API 密钥"),'password']].map(([key,label,type])=><label key={key}>{t(label)}<input type={type} autoComplete={type==='password'?'new-password':undefined} value={cfg[key]||''} onChange={event=>setCfg({...cfg,[key]:event.target.value})}/>{key==='api_key'&&<small>{cfg.key_configured?t("已配置 · 留空保持不变"):t("尚未配置")}</small>}</label>)}
   <label>{t("向量维度")}<select value={cfg.dimensions} onChange={event=>setCfg({...cfg,dimensions:Number(event.target.value)})}>{[64,128,256,512,1024,2048].map(value=><option key={value} value={value}>{value}</option>)}</select></label>
   {[['top_k',t("最多选取原文片段数"),1,12],['context_tokens',t("文档原文上下文预算"),512,8192]].map(([key,label,min,max])=><label key={key}>{t(label)}<input type="number" min={min} max={max} value={cfg[key]} onChange={event=>setCfg({...cfg,[key]:Number(event.target.value)})}/></label>)}
  </div>
  <p className="admin-hint">{t("索引保留全文；每轮仅选取相关片段。语义服务繁忙时保留关键词检索。扫描件识别可能有误，视觉检索不能代替对图表数值的核验。")}</p>
 </section>{note&&<p role="status" className="form-note">{t(note)}</p>}<button className="primary" disabled={busy}><Save size={16}/>{busy?t("正在保存…"):t("保存文档检索配置")}</button></form>;
}
