import {t,getUiLanguage} from './i18n.mjs';
import React,{useState} from 'react';
import {Download,LoaderCircle} from 'lucide-react';
import {api} from './api';

export default function UserExport({users}){
 const [selected,setSelected]=useState([]),[includeFiles,setIncludeFiles]=useState(true),[working,setWorking]=useState(false),[note,setNote]=useState(''),[error,setError]=useState(''),[filter,setFilter]=useState(''),[downloadUrl,setDownloadUrl]=useState('');
 const visible=users.filter(u=>[u.name,u.email,u.organization].some(v=>v.toLowerCase().includes(filter.toLowerCase())));
 function choose(id,checked){setSelected(list=>checked?[...new Set([...list,id])]:list.filter(x=>x!==id));}
 async function download(scope){setWorking(true);setNote('');setError('');setDownloadUrl('');try{
   const data=await api('/admin/exports',{method:'POST',body:JSON.stringify({scope,user_ids:scope==='all'?[]:selected,include_attachments:includeFiles})});
   setDownloadUrl(data.download_url);setNote((scope==='all'?{message:"已生成全部用户的 JSON 文件（{v0} MB）。点击下方链接下载。",values:{v0:(data.size_bytes/1048576).toFixed(2)}}:{message:"已生成{v0} 位用户的 JSON 文件（{v1} MB）。点击下方链接下载。",values:{v0:selected.length,v1:(data.size_bytes/1048576).toFixed(2)}}));
 }catch(e){setError(e.message);}finally{setWorking(false);}}
 return <section className="admin-panel"><h2>{t("用户数据导出")}</h2><p className="admin-hint">{t("导出完整问题、模型实际返回的思考内容、逐轮模型请求与返回、工具调用及完整结果、引用、原文依据、最终回答、专家反馈的全部版本与当时的评审规则，并保留用量和对话记忆。包含已移除的对话，生成中的任务导出当前快照。")}</p>
 <div className="export-toolbar"><input value={filter} onChange={e=>setFilter(e.target.value)} placeholder={t("按姓名、邮箱或机构筛选")} aria-label={t("筛选导出用户")}/><span>{t("已选")} {selected.length} / {users.length} {t("位")}</span><button type="button" className="secondary" disabled={working} onClick={()=>setSelected(selected.length===users.length?[]:users.map(u=>u.id))}>{selected.length===users.length?t("取消全选"):t("选择全部")}</button></div>
 <div className="table-wrap"><table><thead><tr><th>{t("选择")}</th><th>{t("姓名 / 邮箱")}</th><th>{t("机构")}</th><th>{t("身份")}</th><th>{t("状态")}</th></tr></thead><tbody>{visible.map(u=><tr key={u.id}><td><input type="checkbox" aria-label={t("选择用户 ")+u.email} checked={selected.includes(u.id)} disabled={working} onChange={e=>choose(u.id,e.target.checked)}/></td><td>{u.name}<small>{u.email}</small></td><td>{u.organization}</td><td>{u.identity}</td><td>{u.active?t("正常"):t("已停用")}</td></tr>)}{!visible.length&&<tr><td colSpan={5}>{t("没有匹配的用户")}</td></tr>}</tbody></table></div>
 <label className="admin-switch"><input type="checkbox" checked={includeFiles} onChange={e=>setIncludeFiles(e.target.checked)} disabled={working}/>{t("包含附件全文、原件及图片内容")}</label><p className="admin-hint">{t("勾选后包含已保存的原始文档、完整解析文本、结构化页段及图片 Base64，文件可能较大。旧任务缺少逐轮轨迹、历史附件缺少原件时会明确标记。原始语音转写与修改后的意见一起导出；密码、登录凭证和 API 密钥不导出。")}</p>
 <div className="export-actions"><button className="primary" type="button" disabled={working||!selected.length} onClick={()=>download('selected')}>{working?<LoaderCircle size={16} className="spin"/>:<Download size={16}/>}{t("导出所选用户")}</button><button className="secondary" type="button" disabled={working||!users.length} onClick={()=>download('all')}><Download size={16}/>{t("导出全部用户")}</button></div>
 {working&&<p role="status">{t("正在整理导出文件，请稍候…")}</p>}{note&&<p className="form-note" role="status">{t(note)}</p>}{downloadUrl&&<a className="secondary export-download" href={downloadUrl} download><Download size={16}/>{t("下载 JSON 文件")}</a>}{error&&<p className="form-error" role="alert">{t(error)}</p>}
 </section>;
}
