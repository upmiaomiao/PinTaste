import {t,getUiLanguage} from './uiLanguage.mjs';
const fieldNames={max_output_tokens:'单轮最大输出',busy_output_tokens:'多人排队时输出上限',context_window:'模型上下文容量',temperature:'温度',history_turns:'最近问答轮数',memory_tokens:'历史记忆预算',memory_summary_tokens:'摘要预算',memory_retrieval_count:'检索片段数量',memory_trigger_ratio:'自动压缩触发比例',max_parallel:'平台同时生成数',per_user_parallel:'每人同时生成数',max_queue:'平台排队上限',per_user_queue:'每人待处理任务上限',inflight_token_budget:'同时在途容量',job_token_budget:'每任务预算',daily_tokens:'每日 token 额度',daily_questions:'每日提问次数',job_timeout:'任务超时',storage_mb:'附件空间',base_url:'模型接口地址',model:'接口模型名称',display_name:'模型显示名称',system:'系统提示词',email:'邮箱',password:'密码',name:'姓名',organization:'机构',identity:'身份',message:'问题'};
export function apiErrorMessage(detail){
 if(typeof detail==='string')return t(detail);
 if(Array.isArray(detail)&&detail.length)return detail.slice(0,3).map(item=>{
  const field=item.loc?.filter(x=>x!=='body').join('.'),label=t(fieldNames[field])||field||t("配置"),ctx=item.ctx||{};
  const messages={greater_than_equal:t("不能小于 {v0}", {v0:(ctx.ge)}),less_than_equal:t("不能大于 {v0}", {v0:(ctx.le)}),greater_than:t("必须大于 {v0}", {v0:(ctx.gt)}),less_than:t("必须小于 {v0}", {v0:(ctx.lt)}),int_parsing:t("请输入整数"),int_type:t("请输入整数"),int_from_float:t("请输入整数"),float_parsing:t("请输入数字"),float_type:t("请输入数字"),finite_number:t("请输入有效数字"),missing:t("请填写此项"),string_too_short:t("至少需要 {v0} 个字符", {v0:(ctx.min_length)}),string_too_long:t("不能超过 {v0} 个字符", {v0:(ctx.max_length)}),extra_forbidden:t("当前服务不支持此设置，请刷新页面后重试")};
  return t("{v0}：{v1}", {v0:(label), v1:(messages[item.type]||(item.msg||t("请检查此项")).replace(/^Value error,\s*/,''))});
 }).join('；');
 return t("请求未成功，请检查输入或稍后重试。");
}
export async function api(path, options={}) {
  const response=await fetch('/api'+path,{...options,headers:options.body instanceof FormData?options.headers:{'Content-Type':'application/json',...options.headers}});
  if(!response.ok){let body;try{body=await response.json();}catch{}
   // The work service's own reason (e.g. 这个兑换码已经被使用过了。) is forwarded in a header.
   let reason='';try{reason=decodeURIComponent(response.headers.get('X-Wuyu-Message')||'');}catch{}
   const error=new Error(t(reason)||apiErrorMessage(body?.detail));error.status=response.status;error.code=typeof body?.detail==='string'?body.detail:undefined;throw error;}
  return response.json();
}
export async function streamJob(id,signal,onSnapshot){
  const response=await fetch('/api/jobs/'+id+'/events',{signal});
  if(!response.ok)throw new Error(t("无法同步生成进度，请刷新对话；后台任务仍会继续。"));
  const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='',finished=false;
  try {while(true){const {done,value}=await reader.read();buffer+=decoder.decode(value,{stream:!done}).replace(/\r\n/g,'\n');let end;while((end=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,end);buffer=buffer.slice(end+2);for(const line of block.split('\n'))if(line.startsWith('data: ')){const event=JSON.parse(line.slice(6));if(event.type==='snapshot'){finished=!['queued','running'].includes(event.turn.status);onSnapshot(event.turn);}}}if(done)break;}if(!finished&&!signal.aborted)throw new Error(t("正在重新同步生成进度，后台任务会继续执行。"));}
  finally{reader.releaseLock();}
}
