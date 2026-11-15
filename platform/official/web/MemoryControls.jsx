import {t,getUiLanguage} from './i18n.mjs';
import React,{useState,useEffect,useRef} from 'react';
import {createPortal} from 'react-dom';
import {BookOpen,ChevronDown,RefreshCw,Check} from 'lucide-react';
import {api} from './api';

const modes={auto:'智能记忆',recent:'最近对话',fresh:'仅当前问题'};
export default function MemoryControls({mode,autoCompress,onMode,onAuto,chatId,disabled,onNotice}){
 const [open,setOpen]=useState(false),[working,setWorking]=useState(false);
 const trigger=useRef(null),panel=useRef(null);
 useEffect(()=>{if(open)panel.current?.querySelector('button')?.focus();},[open]);
 function close(){setOpen(false);trigger.current?.focus({preventScroll:true});}
 function keys(e){if(e.key==='Escape'){e.stopPropagation();close();}if(e.key==='Tab'){const all=[...panel.current.querySelectorAll('button:not(:disabled),input:not(:disabled)')];const first=all[0],last=all[all.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}}
 async function compact(){setWorking(true);try{const r=await api('/conversations/'+chatId+'/compact',{method:'POST',body:'{}'});onNotice(r.message);}catch(e){onNotice(e.message);}finally{setWorking(false);}}
 return <div className="memory-control"><button ref={trigger} type="button" className={`tool ${mode!=='fresh'?'selected':''}`} aria-expanded={open} onClick={()=>setOpen(!open)} disabled={disabled}><BookOpen size={18}/>{t(modes[mode])}{mode!=='fresh'&&<Check size={14} className="tool-selection-check" aria-hidden="true"/>}<ChevronDown size={13}/></button>
 {open&&createPortal(<div className="memory-scrim" onClick={close}><section ref={panel} className="memory-options" role="dialog" aria-modal="true" aria-label={t("对话记忆设置")} onClick={e=>e.stopPropagation()} onKeyDown={keys}><div className="memory-options-heading"><strong>{t("本轮如何使用历史")}</strong><button type="button" onClick={close} aria-label={t("关闭记忆设置")}>×</button></div>
 {Object.entries(modes).map(([key,label])=><label key={key}><input type="radio" name="memory-mode" value={key} checked={mode===key} disabled={disabled} onChange={()=>onMode(key)}/><span><b>{t(label)}</b><small>{key==='auto'?t("最近问答与相关历史，按容量选取"):key==='recent'?t("只参考最近问答及已有摘要"):t("不读取历史，完整记录仍会保存")}</small></span></label>)}
 <label className="memory-auto"><input type="checkbox" checked={autoCompress} onChange={e=>onAuto(e.target.checked)} disabled={disabled||mode==='fresh'}/><span><b>{t("自动压缩历史")}</b><small>{t("历史较长时整理摘要，完整记录保留")}</small></span></label>
 <button type="button" className="memory-compact" onClick={compact} disabled={!chatId||disabled||working}><RefreshCw size={15} className={working?'spin':''}/>{working?t("正在整理…"):t("立即整理记忆")}</button>
 <p>{t("记忆仅用于当前对话。摘要可能遗漏细节，重要条件请在问题中明确。")}</p></section></div>,document.body)}
 </div>;
}

export function TurnMemory({memory}){
 if(!memory)return null;
 return <details className="turn-memory"><summary><BookOpen size={14}/>{t(modes[memory.mode])}{memory.mode!=='fresh'&&<> {t(" · 最近 ")}{memory.recent_turns} {t(" 轮 · 检索 ")}{memory.retrieved_chunks} {t(" 处历史")}{memory.compressed?t(" · 已自动压缩"):memory.summary_id?t(" · 已使用摘要"):''}</>}</summary><p>{memory.mode==='fresh'?t("本轮未携带历史内容。"):t("本轮起始历史约 {v0} tokens，在 {v1} tokens 预算内选取。{v2}", {v0: memory.token_estimate.toLocaleString(getUiLanguage()), v1: memory.budget_tokens.toLocaleString(getUiLanguage()), v2: memory.limited?t("较早内容按相关性选取，未全部放入上下文。"):''})} {t(" 完整对话记录保存在账号中。")}</p>{memory.inherited_images?.length>0&&<p>{t("沿用上一轮图片：")}{memory.inherited_images.join('、')}</p>}</details>;
}
