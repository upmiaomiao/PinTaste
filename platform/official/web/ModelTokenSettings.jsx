import {t,getUiLanguage} from './i18n.mjs';
import React,{useState} from 'react';
import {INPUT_HEADROOM,MAX_TOKEN_INTEGER,tokenSettingsError,syncTokenBudgets,longAnswerPreset} from './modelTokenSettings.mjs';
import './model-token-settings.css';

const format=value=>Number(value||0).toLocaleString(getUiLanguage());

export default function ModelTokenSettings({value,onChange,disabled=false}){
 const [note,setNote]=useState(''),[error,setError]=useState('');
 const update=(key,next)=>{setNote('');setError('');onChange({...value,[key]:next});};
 const apply=fn=>{try{onChange(fn(value));setError('');setNote("已更新下方草稿，点击“保存配置”后生效。");}catch(e){setError(e.message);}};
 const context=Number(value.context_window),output=Number(value.max_output_tokens);
 const problem=tokenSettingsError(value);
 const incompatible=(value.additional_models||[]).filter(p=>p.enabled&&Number(p.max_output_tokens??output)>Number(p.context_window)-INPUT_HEADROOM);
 const limits=[
  ['busy_output_tokens',t("多人排队时输出上限"),512,output,t("与常规上限相同即可避免排队时缩短回答。")],
  ['job_token_budget',t("每任务总 token 预算"),context,MAX_TOKEN_INTEGER,t("覆盖检索与多轮模型调用的累计输入和输出。")],
  ['inflight_token_budget',t("同时在途 token 容量"),context,MAX_TOKEN_INTEGER,t("应容纳各个同时运行任务的上下文。")],
  ['job_timeout',t("任务超时（秒）"),30,MAX_TOKEN_INTEGER,t("21600 秒 = 6 小时；较长回答需要足够的生成时间。")],
 ];
 return <section className="model-token-settings" aria-label={t("上下文与回答长度")}>
  <div className="admin-panel-title"><h2>{t("上下文与回答长度")}</h2><button type="button" className="secondary" disabled={disabled} onClick={()=>apply(longAnswerPreset)}>{t("26万上下文 / 16万输出")}</button></div>
  <p className="admin-hint">{t("可直接输入 API 支持的容量，无固定 16K 输出限制。输入、附件、历史与输出共用上下文；最大输出是上限，不代表每次都会生成该长度，实际能力以模型接口为准。")}</p>
  <div className="admin-fields">
   <label>{t("模型上下文容量")}<input type="number" min="8192" max={MAX_TOKEN_INTEGER} step="1" required value={value.context_window??''} disabled={disabled} onChange={e=>update('context_window',e.target.value)}/><small>{t("上下文总量，单位为 tokens。")}</small></label>
   <label>{t("单轮最大输出 tokens")}<input type="number" min="512" max={Number.isSafeInteger(context)?context-INPUT_HEADROOM:MAX_TOKEN_INTEGER} step="1" required value={value.max_output_tokens??''} disabled={disabled} onChange={e=>update('max_output_tokens',e.target.value)}/><small>{!problem?t("按当前上限，为输入预留约 {v0} tokens（含系统与工具开销）。", {v0:(format(context-output))}):t("请先填写有效的上下文和输出上限。")}</small></label>
  </div>
  {problem&&<p className="form-error" role="alert">{problem}</p>}
  <div className="model-token-sync"><button type="button" className="secondary" disabled={disabled||!!problem} onClick={()=>apply(syncTokenBudgets)}>{t("同步关联预算")}</button><span>{t("按当前容量匹配排队上限、任务预算、在途容量与至少 6 小时超时。只修改草稿，不调整每日额度。")}</span></div>
  <p className="model-token-current">{t("排队输出")} {format(value.busy_output_tokens)} {t("· 每任务")} {format(value.job_token_budget)} {t("· 在途")} {format(value.inflight_token_budget)} {t("tokens · 超时")} {format(value.job_timeout)} {t("秒")}</p>
  <details className="model-token-details"><summary>{t("查看与修改关联预算")}</summary><div className="admin-fields">{limits.map(([key,label,min,max,hint])=><label key={key}>{t(label)}<input type="number" min={Number.isFinite(min)?min:undefined} max={Number.isFinite(max)?max:MAX_TOKEN_INTEGER} step="1" required value={value[key]??''} disabled={disabled} onChange={e=>update(key,e.target.value)}/><small>{t(hint)}</small></label>)}</div><p className="admin-hint">{t("同步预算按最大已启用上下文的 4 倍预留每任务用量，按平台同时生成数匹配在途容量；已有更高预算保留。可按实际需要手动调整。")}</p></details>
  {Number(value.busy_output_tokens)<output&&<p className="model-token-warning">{t("当前多人排队时最多输出")} {format(value.busy_output_tokens)} {t("tokens；需要保持完整上限时，请同步关联预算。")}</p>}
  {Number(value.daily_tokens)<Number(value.job_token_budget)&&<p className="form-error" role="alert">{t("每任务预算已超过每日额度，请在“容量与额度”提高每日额度，或降低每任务预算后保存。")}</p>}
  {incompatible.length>0&&<p className="form-error" role="alert">{incompatible.map(p=>p.display_name).join('、')}{t("的输出上限超出各自上下文，请在下方“其他可选模型”设置较小的独立输出上限，或填写该接口实际支持的更大上下文后保存。")}</p>}
  {error&&<p className="form-error" role="alert">{t(error)}</p>}{note&&<p className="form-note" role="status">{t(note)}</p>}
 </section>;
}
