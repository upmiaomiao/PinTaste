import {t,getUiLanguage} from './i18n.mjs';
import React, {useState} from 'react';
import {Brain, ChevronDown} from 'lucide-react';
import RemoteSensingAnnotations from './RemoteSensingAnnotations';
import {Answer} from './ChatParts';
import {remoteSensingSummary, remoteSensingScoreText} from './remoteSensingFormat.mjs';
import {remoteSensingIsGenerating,remoteSensingHasMarkup} from './remoteSensingDisplay.mjs';
import './remote-sensing-response.css';

function Paragraphs({text}) {
  return String(text || '').split(/\n\s*\n/).filter(part => part.trim()).map((part, index) => <p key={index}>{part.trim()}</p>);
}

export function RemoteSensingReasoning({turn, result}) {
  const [open, setOpen] = useState(undefined);
  const running = remoteSensingIsGenerating(turn);
  const sections = running ? result.stream?.sections || [] : result.sections;
  const review = running ? result.stream?.review || [] : result.review;
  const candidate = running ? result.stream?.ordinaryReasoning : result.ordinaryNonXmlReasoning;
  const ordinary = remoteSensingHasMarkup(candidate) ? '' : candidate;
  const extras = running ? [] : result.additionalText.filter(text => !remoteSensingHasMarkup(text));
  if (!sections.length && !review.length && !ordinary && !extras.length) return null;
  const count = [...sections.map(section => section.text), ...review.map(item => item.text), ordinary, ...extras].filter(Boolean).join('').length;
  return <details className={`reasoning wuyu-rs-reasoning${running ? ' is-streaming' : ''}`} open={open ?? running}>
    <summary onClick={event => {event.preventDefault();setOpen(value => !(value ?? running));}}><Brain size={16}/><strong>{t("影像分析过程")}</strong><span>{running ? t("正在分析") : t("{v0} 字", {v0:(count.toLocaleString(getUiLanguage()))})}</span><ChevronDown size={15}/></summary>
    <div className="wuyu-rs-analysis-text">
      {sections.map((section, index) => <section key={`${index}:${section.name}`}><h4>{section.name || t("分析环节 {v0}", {v0:(index + 1)})}</h4><Paragraphs text={section.text}/></section>)}
      {!!review.length && <section className="wuyu-rs-decision-review"><h4>{t("综合复核")}</h4>{review.map((item, index) => <div key={`${index}:${item.key}`}><Paragraphs text={item.label === '综合复核' ? item.text : `${item.label}：${item.text}`}/></div>)}</section>}
      {ordinary && <section>{!!sections.length && <h4>{t("影像观察")}</h4>}<Paragraphs text={ordinary}/></section>}
      {!!extras.length && <section className="wuyu-rs-other-observations"><h4>{t("其他观察")}</h4>{extras.map((text, index) => <Paragraphs key={index} text={text}/>)}</section>}
    </div>
  </details>;
}

export function RemoteSensingAnswer({turn, result, images, onSource}) {
  if (remoteSensingIsGenerating(turn)) {
    const stream = result.stream || {};
    const hasAnalysis = stream.sections?.length || stream.review?.length || stream.ordinaryReasoning;
    const conclusion = stream.conclusion || stream.ordinaryAnswer || '';
    return <div className="wuyu-rs-stream-response" aria-busy="true">
      {conclusion && <section className="answer wuyu-rs-answer is-streaming" aria-label={t("正在生成遥感回答")}><h3>{t("分析结论")}</h3><Paragraphs text={conclusion}/></section>}
      <p className={hasAnalysis || conclusion ? 'wuyu-rs-stream-status' : 'wuyu-rs-awaiting'} role="status"><span className="wuyu-rs-stream-dot" aria-hidden="true"/>{hasAnalysis || conclusion ? t("正在生成…") : turn.status === 'queued' ? t("正在排队…") : t("正在读取影像…")}</p>
    </div>;
  }
  if (!result.hasStructured && !remoteSensingHasMarkup(turn.answer + turn.reasoning)) return turn.answer ? <Answer turn={turn} onSource={onSource || (() => {})}/> : null;
  // The response archive is immutable. The readable view only separates fields.
  const recovered = turn.remoteSensingRecoveredAnswer === true;
  const incomplete = !recovered && result.issues.some(issue => issue.code === 'generation_incomplete');
  const invalid = result.issues.some(issue => issue.severity === 'error');
  const answer = result.answer?.complete ? result.answer : null;
  const remaining = remoteSensingHasMarkup(result.remainingAnswerText) ? '' : result.remainingAnswerText;
  const score = remoteSensingScoreText(answer);
  return <div className="wuyu-rs-response">
    {(incomplete || invalid) && <p className="wuyu-rs-format-notes" role="status">{incomplete ? t("这次回答未完整生成，可继续追问或重试。") : t("部分标注信息未能可靠读取，文字结论仍可查看。")}</p>}
    {answer && <section className="answer wuyu-rs-answer" aria-label={t("遥感分析结论")}>
      <h3>{t("分析结论")}</h3>
      <p className="wuyu-rs-conclusion-summary">{remoteSensingSummary(result)}</p>
      <Paragraphs text={answer.conclusion}/>
      {score && <p className="wuyu-rs-model-score">{score}</p>}
      {!!result.regions.length && result.status === 'partial' && <div className="wuyu-rs-unresolved-regions"><h4>{t("模型返回的目标")}</h4>{result.regions.map((region, index) => <p key={`${index}:${region.id}`}>{region.id || t("目标 {v0}", {v0:(index + 1)})} {t("· 模型类型：")}{region.label || t("未提供")}{region.valid ? t(" · 图像坐标：[{v0}]", {v0:(region.bbox.join(', '))}) : t(" · 坐标尚不可可靠标注")}</p>)}</div>}
    </section>}
    {remaining && <section className="answer wuyu-rs-extra"><h4>{answer ? t("补充回答") : t("回答")}</h4><Paragraphs text={remaining}/></section>}
    {!answer && !remaining && <p className="wuyu-rs-format-notes">{t("模型未提供可展示的完整结论，请重试。")}</p>}
    <RemoteSensingAnnotations result={result} images={images}/>
  </div>;
}
