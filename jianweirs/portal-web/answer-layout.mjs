import {readableText, renderMarkdown, escapeHTML as esc, safeURL} from './lib.mjs';

/** Product wording, same rule as wuyu_harness/answer_layout.public_review_text:
 * 见微识别 (detector) vs 独立复核 (any other reviewer); provider brands hidden. */
export function reviewText(value='') {
  return readableText(value)
    .replace(/见微初判\s*(?:与|和|及|→|\/|、)\s*Gemini(?:\s*[\d.]+)?(?:\s*Flash)?\s*(?:全面|综合|最终)?复核/gi,'见微识别与独立复核')
    .replace(/(?:Gemini|DeepSeek)(?:\s*[\d.]+)?(?:\s*Flash)?\s*(?:(?:主模型|模型|全面|综合|最终|交叉|同图|场景|误报|环境|影像|科学|高分|光学|框选|与|和|及)\s*)*(?:复核|审查|审阅|评审|终审)/gi,'独立复核')
    .replace(/见微初判|见微初评/g,'见微识别')
    .replace(/见微复核/g,'独立复核')
    .replace(/\bGemini(?:[-\s]*\d+(?:\.\d+)*[-\s]*(?:flash|pro|preview|latest)?)?\b/gi,'独立复核模型')
    .replace(/\bDeepSeek(?:[-\s]*[\w.]+)?\b/gi,'主模型')
    .replace(/独立复核\s*(?:复核|审查)/g,'独立复核')
    .replace(/独立复核\s*[+与和、/]\s*独立复核/g,'独立复核');
}

/** Reflow saved prose at sentence boundaries; never discard its evidence. */
export function readingText(value='') {
  let text=reviewText(value).replace(/\r\n?/g,'\n');
  text=text.replace(/([^\n])\s*([①②③④⑤⑥⑦⑧⑨⑩])/g,'$1\n\n$2');
  text=text.replace(/(^|\n)\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*/g,'$1- ');
  text=text.replace(/([^\n])\s+(\d{1,2}[）)]\s*)/g,'$1\n\n$2');
  text=text.replace(/([；;。])\s*(\d{1,2}[）)]\s*)/g,'$1\n\n$2');
  text=text.replace(/(^|\n)\s*(\d{1,2})[）)]\s*/g,'$1$2. ');
  return text.split(/\n\s*\n/).map(block=>{
    // Preserve lists, code, tables and authored headings instead of guessing.
    if(block.length<=280 || /(^|\n)(?:\s*[-*+] |\s*\d+[.)、）]|#|```|\|)/.test(block)) return block;
    const sentences=block.match(/[^。！？!?]+[。！？!?]+|[^。！？!?]+$/g)||[block];
    const chunks=[];let current='';
    for(const sentence of sentences){
      if(current.length>=130){chunks.push(current.trim());current='';}
      current+=sentence;
    }
    if(current.trim())chunks.push(current.trim());
    return chunks.join('\n\n');
  }).join('\n\n').trim();
}

export function answerExcerpt(value='', limit=230) {
  const text=readingText(value);
  if(text.length<=limit+50)return {lead:text,rest:''};
  const candidates=[...text.slice(0,limit+1).matchAll(/[。！？!?；;]\s*|\n\s*\n/g)];
  const cut=candidates.findLast(match=>match.index>50);
  if(!cut)return {lead:text,rest:''};
  const index=cut.index+cut[0].length;
  return {lead:text.slice(0,index).trim(),rest:text.slice(index).trim()};
}

export function renderReadableAnswer(value, {id='answer', detailLabel='展开完整答复', limit=230}={}) {
  const {lead,rest}=answerExcerpt(value,limit);
  return `${lead?`<div class="answer-lead markdown-body">${renderMarkdown(lead)}</div>`:''}${rest?`<details class="supporting-details answer-continuation" data-ui-state="answer-${esc(id)}"><summary>${esc(detailLabel)}<span aria-hidden="true">⌄</span></summary><div class="answer-details markdown-body">${renderMarkdown(rest)}</div></details>`:''}`;
}

export function renderCoreReport(report, {latest=false,icon=()=>''}={}) {
  if(!report || !Array.isArray(report.artifacts))return '';
  const files=report.artifacts.filter(file=>['report.docx','report.html'].includes(file.name)&&safeURL(file.url));
  if(!files.length)return '';
  const revision=Number(report.revision||report.version)||null;
  return `<section class="core-report-card ${latest?'is-latest':'is-snapshot'}" aria-label="${latest?'会话核心报告':'本轮核心报告快照'}"><div class="core-report-heading">${icon('file')}<strong>${latest?'会话核心报告':'本轮报告快照'}</strong>${revision?`<span>第 ${revision} 版</span>`:''}</div>${latest?'<p>随本会话的影像证据、政策资料和复核意见持续更新。</p>':'<p>保留截至这一轮的累计判断，后续修订在最新版本中查看。</p>'}<div class="core-report-links">${files.map(file=>`<a href="${esc(safeURL(file.url))}" target="_blank" rel="noopener">${file.name==='report.docx'?'下载 Word':'查看在线报告'}${icon('chevron')}</a>`).join('')}</div></section>`;
}
