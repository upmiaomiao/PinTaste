import {t,getUiLanguage} from './i18n.mjs';
import React,{useState} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {Brain,ChevronDown,ExternalLink,FileText,FlaskConical,Globe,Search,X} from 'lucide-react';
import logo from './logo.png';
import {sourceMap,sourceTitle,sourceKind} from './sources.mjs';
import {reasoningSections} from './executionTiming.mjs';
const formatTime = value => new Intl.DateTimeFormat(getUiLanguage(), { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(value);
function Logo() { return <span className="brand-mark"><img src={logo} alt={t("无隅循环 Logo")} /></span>; }
const fileSize = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
const toolLabel = name => /wechat/.test(name) ? (name === 'wechat_get_article' ? t("公众号正文读取") : t("微信公众号搜索")) : /scholar/.test(name) ? t("学术搜索") : t("联网搜索");
function FileChips({files, onRemove}) {
  if (!files?.length) return null;
  return <div className="file-chips">{files.map(file => <div className={`file-chip ${file.kind === 'image' ? 'image-chip' : ''}`} key={file.id}>
    {file.kind === 'image' ? <a href={file.preview_url} target="_blank" rel="noopener noreferrer" aria-label={t("查看图片 {v0}", {v0: file.name})}><img src={file.preview_url} alt={file.name} /></a> : <FileText size={25} />}
    <span><b title={file.name}>{file.name}</b><small>{fileSize(file.size)} · {file.kind === 'image' ? t("图片") : t("已解析")}</small>{file.warning && <small className="file-warning" title={file.warning}>{file.warning}</small>}</span>
    {onRemove && <button type="button" className="icon-button" aria-label={t("移除附件 {v0}", {v0: file.name})} onClick={() => onRemove(file.id)}><X size={14}/></button>}
  </div>)}</div>;
}
function Reasoning({turn}) {
  const [open,setOpen] = useState(turn.status === 'streaming');
  const sections=reasoningSections(turn);
  return <details className="reasoning" open={open}><summary onClick={event => {event.preventDefault();setOpen(value => !value);}}><Brain size={16}/><strong>{turn.status === 'streaming' && !turn.answer ? t("正在思考") : t("思考过程")}</strong><span>{turn.reasoning.length.toLocaleString(getUiLanguage())} {t(" 字")}</span><ChevronDown size={15}/></summary><div className="reasoning-text">{sections.map((section,index)=><section className="reasoning-round" key={index}>{section.round!==null&&<strong className="reasoning-round-label">{t('第 {v0} 轮思考',{v0:section.round})}</strong>}{section.round===null&&sections.length>1&&<strong className="reasoning-round-label">{t('未记录轮次的思考')}</strong>}{section.text}</section>)}</div></details>;
}
function Answer({turn, onSource}) {
  const fencedXml = turn.answer.trim().match(/^(`{3,})xml[ \t]*\r?\n([\s\S]*?)\r?\n\1$/i);
  const xmlText = fencedXml ? fencedXml[2] : turn.answer;
  if (/^<(?:thinking|answer)(?:\s|>)/i.test(xmlText.trimStart())) {
    return <div className="answer"><pre className="xml-answer" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',wordBreak:'break-word'}}>{xmlText}</pre></div>;
  }
  const sources = sourceMap(turn);
  return <div className="answer"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{a: ({node,children,href,...props}) => {
    const documentId=String(children).match(/^D\d+$/)?.[0];
    const documentSource=(turn.documentSources||[]).find(source=>source.source_id===documentId);
    if(documentSource)return <a className="document-inline-citation" href={`#document-${turn.id}-${documentId}`} onClick={event=>{event.preventDefault();const target=document.getElementById(`document-${turn.id}-${documentId}`);if(target){target.closest('details').open=true;target.scrollIntoView({behavior:'smooth',block:'center'});}}} title={`${documentSource.title}${documentSource.page?t(" · 第 {v0} 页", {v0: documentSource.page}):''}`}>{documentId}</a>;
    const id=String(children).match(/^S(\d+)$/)?.[0];
    const source=id ? sources.get(id) : [...sources.values()].find(source => source.url === href);
    return source ? <a className="inline-citation" data-source-trigger={`cite-${turn.id}-${source.source_id}`} href={source.url} onClick={event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();onSource(turn, sourceKind(source), source.source_id, event.currentTarget);
    }} target="_blank" rel="noopener noreferrer" title={sourceTitle(source)} aria-label={t("引用 {v0}：{v1}", {v0: source.source_id.slice(1), v1: sourceTitle(source)})}>{source.source_id.slice(1)}</a> : <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  }, img:()=> <span className="image-omitted">{t("[外部图片已省略]")}</span>}}>{citedMarkdown(turn.answer,turn)}</Markdown></div>;
}

function citedMarkdown(text, turn, history = false) {
  const sources = sourceMap(turn);
  return text.split(/(```[\s\S]*?(?:```|$)|`[^`\n]+`)/g).map(part => part.startsWith('`') ? part : part.replace(/\[(D\d+)\](?:\([^\n)]*\))?/g,(whole,id)=>{
    const source=(turn.documentSources||[]).find(source=>source.source_id===id);
    if(!source)return whole;
    return history?t("（附件：{v0}{v1}）", {v0: source.title, v1: source.page?t("，第 {v0} 页", {v0: source.page}):''}):`[${id}](#document-${turn.id}-${id})`;
  }).replace(
    /\[(S\d+(?:\s*[,，;；]\s*S\d+)*)\](?:\([^\n)]*\))?/g,
    (whole, ids) => [...ids.matchAll(/S\d+/g)].map(([id]) => {
      const source = sources.get(id);
      if (!source) return history ? t("（历史引用未匹配）") : `[${id}]`;
      const url = source.url.replace(/\(/g, '%28').replace(/\)/g, '%29');
      return history ? t("（来源：{v0}；{v1}）", {v0: sourceTitle(source), v1: url}) : `[${id}](${url})`;
    }).join(' ')
  )).join('');
}
function SourceList({ sources, compact = false }) {
  return <ol className={`source-list ${compact ? 'compact' : ''}`}>{sources.map(source => <li key={source.source_id}>
    <span className="source-id">{source.source_id.replace(/^S/,'')}</span><div><a href={source.url} target="_blank" rel="noopener noreferrer">{sourceTitle(source)}<ExternalLink size={12} /></a>
    <small>{source.type === 'wechat' ? t("微信公众号 · {v0}", {v0: source.account || t("账号未标明")}) : source.type === 'scholar' ? t("学术") : t("网页")}{source.date ? ` · ${source.date}` : ''} · {new URL(source.url).hostname}</small>
    {!!source.authors?.length && !compact && <small>{source.authors.join('、')}</small>}
    {source.year_status === 'unknown' && <small>{t("发表年份未标明，不能确认属于近十年")}</small>}
    {compact && source.snippet && <p>{source.snippet}</p>}</div>
  </li>)}</ol>;
}
function ToolHistory({ turn }) {
  if (!turn.toolCalls?.length) return null;
  const running = turn.status === 'streaming' && turn.toolCalls.some(call => call.status === 'running');
  return <details className="tool-history" open={running || undefined}><summary><Globe size={16} /><strong>{t("工具调用记录")}</strong><span>{turn.toolCalls.length} {t(" 次")}</span><ChevronDown size={15} /></summary>
    {turn.toolCalls.map(call => <div className="tool-record" key={call.call_id}><div className="tool-record-title">{/scholar/.test(call.name) ? <FlaskConical size={15} /> : <Search size={15} />}<strong>{call.label?t(call.label):toolLabel(call.name)}{call.language ? ` · ${call.language === 'zh' ? t("中文") : t("英文")}` : ''}</strong><span className={call.status === 'error' ? 'tool-failed' : ''}>{call.status === 'running' ? (turn.status === 'streaming' ? t("正在调用…") : t("已中断")) : call.status === 'error' ? t("调用失败") : t("{v0} 个来源{v1}{v2}", {v0: call.sources?.length||0, v1: call.data?t(" · 已返回数据"):'', v2: call.cached?t(" · 复用结果"):''})}</span></div>
      <p className="tool-query">{call.query || t("工具查询")}</p>{call.error && <p className="tool-failed">{t(call.error)}</p>}
      {call.data&&<details className="tool-sources"><summary>{t("查看工具返回数据")}</summary><pre className="tool-test-result">{call.data}</pre>{call.truncated&&<small>{t("结果过长，已截取部分内容。")}</small>}</details>}{call.date_window && <p className="tool-date">{t("近十年优先 · ")}{call.date_window.from} {t(" 至 ")}{call.date_window.to}{call.excluded_by_date ? t(" · 已排除 {v0} 个范围外结果", {v0: call.excluded_by_date}) : ''}</p>}
      {!!call.sources?.length && <details className="tool-sources"><summary>{t("查看本次检索结果")}{call.seconds != null ? ` · ${call.seconds}s` : ''}</summary><SourceList sources={call.sources} compact /></details>}
    </div>)}
  </details>;
}


export {Logo,FileChips,Reasoning,Answer,ToolHistory,citedMarkdown};
