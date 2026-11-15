import {t,getUiLanguage} from './i18n.mjs';
import React, {useId, useState} from 'react';
import {ChevronDown, ExternalLink, ScanLine} from 'lucide-react';
import './remote-sensing-annotations.css';

// Re-check the parser's geometry and attribution before painting on an image.
// These coordinates describe image pixels, not a geographic location or area.
export function validAnnotationBox(bbox) {
  return Array.isArray(bbox) && bbox.length === 4 && bbox.every(value =>
    typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= 0 && value <= 1000
  ) && bbox[0] < bbox[2] && bbox[1] < bbox[3];
}

export function trustedImagePreview(image) {
  if (!image || image.kind !== 'image' || typeof image.id !== 'string') return null;
  const match = typeof image.preview_url === 'string' && image.preview_url.match(/^\/api\/files\/([A-Za-z0-9_-]+)\/preview$/);
  return match && match[1] === image.id ? image.preview_url : null;
}

export function annotationImages(result, images = []) {
  if (result?.status !== 'complete' || result?.answer?.decisionKind !== 'positive') return [];
  const allRegions = Array.isArray(result.regions) ? result.regions : [];
  const eligible = new Set(Array.isArray(result.overlayRegions) ? result.overlayRegions : []);
  // A completed negative decision with nonempty regions is a model conflict.
  if (result.issues?.some(issue => issue.severity === 'error')) return [];
  return images.filter(image => trustedImagePreview(image)).map(image => ({
    image,
    regions: allRegions.map((region, index) => ({...region, selectionKey: `${index}:${region.id || 'region'}`}))
      .filter(region => (eligible.has(allRegions[Number(region.selectionKey.split(':')[0])]) || region.overlayEligible === true)
        && region.valid === true && validAnnotationBox(region.bbox)
        && region.coord === 'normalized_1000' && region.fileId === image.id
        && ['single', 'explicit'].includes(region.imageBinding))
  })).filter(entry => entry.regions.length);
}

export function riskTone(risk) {
  if (/^(高|高风险|high)$/i.test(String(risk || '').trim())) return 'high';
  if (/^(中|中风险|medium|moderate)$/i.test(String(risk || '').trim())) return 'medium';
  if (/^(低|低风险|low)$/i.test(String(risk || '').trim())) return 'low';
  return 'unknown';
}

export function annotationGeometry(bbox, width, height, label, riskLevel) {
  const [x1, y1, x2, y2] = bbox;
  const x = x1 * width / 1000, y = y1 * height / 1000;
  const fontSize = width / 46;
  // Full type text remains in the list. Keep the in-image badge inside the image.
  const badge = Array.from(String(label || '')).slice(0, 16).join('');
  const text = Array.from(String(label || '')).length > 16 ? `${badge}…` : badge;
  const riskFullText = riskLevel === undefined ? '' : t("本轮模型风险：{v0}", {v0:(riskLevel || t("未提供"))});
  const riskChars = Array.from(riskFullText);
  const riskText = riskChars.length > 18 ? `${riskChars.slice(0, 17).join('')}…` : riskFullText;
  const labelWidth = Math.min(width, (Math.max(Array.from(text).length, Array.from(riskText).length * .88) + 1) * fontSize);
  const labelHeight = fontSize * (riskText ? 2.85 : 1.6);
  return {x, y, width: (x2 - x1) * width / 1000, height: (y2 - y1) * height / 1000,
    labelX: Math.max(0, Math.min(x, width - labelWidth)),
    labelY: Math.max(0, Math.min(y - labelHeight, height - labelHeight)),
    labelWidth, labelHeight, fontSize, text, riskText};
}

function ImageAnnotations({entry, selected, onSelect, showBoxes, riskLevel}) {
  const {image, regions} = entry;
  const [dimensions, setDimensions] = useState(null);
  const [failed, setFailed] = useState(false);
  const preview = trustedImagePreview(image);
  const imageName = image.name || t("遥感影像");
  return <figure className="wuyu-rs-image-card">
    <figcaption><span title={imageName}>{imageName}</span><a href={preview} target="_blank" rel="noopener noreferrer">{t("打开原图")}<ExternalLink size={13}/></a></figcaption>
    {failed ? <div className="wuyu-rs-image-error" role="status">{t("影像加载失败，可通过“打开原图”查看；下方仍保留疑点信息。")}</div> :
      <div className="wuyu-rs-image-frame" style={dimensions ? {aspectRatio: `${dimensions.width} / ${dimensions.height}`} : undefined}>
        <img src={preview} alt={t("{v0}，模型标注影像", {v0:(imageName)})} onError={() => setFailed(true)} onLoad={event => {
          const {naturalWidth, naturalHeight} = event.currentTarget;
          if (naturalWidth > 0 && naturalHeight > 0) setDimensions({width: naturalWidth, height: naturalHeight});
        }}/>
        {dimensions && showBoxes && <svg className="wuyu-rs-overlay" viewBox={`0 0 ${dimensions.width} ${dimensions.height}`} preserveAspectRatio="xMidYMid meet" aria-label={t("模型疑点标注，使用 Tab 键选择目标")}>
          {regions.map(region => {
            const id = region.id || t("疑点");
            const label = region.label || t("类型未提供");
            const risk = riskLevel || t("未提供");
            const geometry = annotationGeometry(region.bbox, dimensions.width, dimensions.height, `${id} · ${t(label)}`, risk);
            const active = selected === region.selectionKey;
            return <g key={region.selectionKey} className={`wuyu-rs-region wuyu-rs-region-risk-${riskTone(risk)} ${active ? 'is-selected' : ''}`} role="button" tabIndex="0" aria-label={t("选择 {v0}，模型类型：{v1}，本轮模型风险：{v2}", {v0:(id), v1:(label), v2:(risk)})} aria-pressed={active} onClick={() => onSelect(region.selectionKey)} onKeyDown={event => {
              if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();onSelect(region.selectionKey);}
            }}>
              <title>{t("{v0} · 模型类型：{v1} · 本轮模型风险：{v2} · 待核查疑点", {v0:(id), v1:(label), v2:(risk)})}</title>
              <rect className="wuyu-rs-box" x={geometry.x} y={geometry.y} width={geometry.width} height={geometry.height} vectorEffect="non-scaling-stroke"/>
              <rect className="wuyu-rs-box-label-bg" x={geometry.labelX} y={geometry.labelY} width={geometry.labelWidth} height={geometry.labelHeight} rx={geometry.fontSize / 5}/>
              <text className="wuyu-rs-box-label" x={geometry.labelX + geometry.fontSize / 2} y={geometry.labelY + geometry.fontSize * 1.17} fontSize={geometry.fontSize}>{geometry.text}</text>
              <text className="wuyu-rs-box-risk-label" x={geometry.labelX + geometry.fontSize / 2} y={geometry.labelY + geometry.fontSize * 2.4} fontSize={geometry.fontSize * .88}>{geometry.riskText}</text>
            </g>;
          })}
        </svg>}
      </div>}
  </figure>;
}

function attributionLabel(region, images) {
  const image = images.find(item => item.id === region.fileId);
  if (image) return image.name || t("本轮影像");
  return region.imageBinding === 'ambiguous' ? t("多个影像的目标归属尚未明确") : t("影像来源尚未匹配");
}

function unavailableLabel(region, answer) {
  if (answer?.decisionKind === 'negative') return t("模型结论与目标清单冲突，未投框");
  if (answer?.decisionKind !== 'positive') return t("结论尚未明确，未投框");
  if (!region.valid || !validAnnotationBox(region.bbox) || region.coord !== 'normalized_1000') return t("坐标无效或格式未明确，未投框");
  if (region.imageBinding === 'ambiguous') return t("多个影像的目标归属尚未明确，未投框");
  if (region.imageBinding === 'missing') return t("影像来源尚未匹配，未投框");
  return t("标注暂不可用");
}

export function RemoteSensingAnnotationContent({result, images = []}) {
  const [showBoxes, setShowBoxes] = useState(true);
  const [selection, setSelection] = useState(null);
  if (!result?.isRemoteSensing || !['complete', 'invalid'].includes(result.status) || !result.regions?.length) return null;
  const answer = result.answer;
  const entries = annotationImages(result, images);
  const eligibleKeys = new Set(entries.flatMap(entry => entry.regions.map(region => region.selectionKey)));
  const selected = selection && result.regions.some((region, index) => `${index}:${region.id || 'region'}` === selection)
    ? selection : entries[0]?.regions[0]?.selectionKey || `0:${result.regions[0].id || 'region'}`;
  const selectedIndex = Number(selected.split(':')[0]);
  const selectedRegion = result.regions[selectedIndex];
  const conflict = answer?.decisionKind === 'negative';
  const tone = riskTone(answer?.riskLevel);
  const riskText = answer?.riskLevel || t("未提供");
  const scoreText = answer?.confidenceText || (Number.isFinite(answer?.confidence) ? String(answer.confidence) : t("未提供"));
  const safeImages = images.filter(image => trustedImagePreview(image));
  return <div className="wuyu-rs-annotations-body">
      <div className="wuyu-rs-annotation-meta"><span className="wuyu-rs-review-badge">{t("待核查疑点")}</span><span className={`wuyu-rs-risk wuyu-rs-risk-${tone}`}>{t("本轮模型风险：")}{riskText}</span></div>
      <p className="wuyu-rs-annotation-note">{t("标签与范围来自本轮模型输出，供现场核查参考。类型和风险尚未经过现场确认。")}</p>
      {!entries.length && <p className="wuyu-rs-annotation-warning" role="status">{conflict
        ? t("模型结论与非空目标清单存在冲突，暂不在影像上投框。")
        : result.status === 'invalid'
          ? t("结构化结果存在冲突或无效字段，暂不在影像上投框。")
        : result.regions.some(region => region.imageBinding === 'ambiguous')
          ? t("多个影像的目标归属尚未明确，暂不在影像上投框。")
          : answer?.decisionKind !== 'positive'
            ? t("模型结论尚未明确，暂不在影像上投框。")
            : t("缺少可验证的影像来源或有效坐标，暂不在影像上投框。")}</p>}
      {!!entries.length && <label className="wuyu-rs-annotation-toggle"><input type="checkbox" checked={showBoxes} onChange={event => setShowBoxes(event.target.checked)}/>{t("显示疑点框和标签")}</label>}
      {entries.map(entry => <ImageAnnotations key={entry.image.id} entry={entry} selected={selected} onSelect={setSelection} showBoxes={showBoxes} riskLevel={answer?.riskLevel}/>)}
      {!entries.length && !!safeImages.length && <div className="wuyu-rs-original-links">{safeImages.map((image, index) => <a key={image.id} href={trustedImagePreview(image)} target="_blank" rel="noopener noreferrer">{t("打开原图")} {safeImages.length > 1 ? index + 1 : ''}<ExternalLink size={13}/></a>)}</div>}
      <div className="wuyu-rs-region-list" role="group" aria-label={t("模型疑点清单")}>
        {result.regions.map((region, index) => {
          const selectionKey = `${index}:${region.id || 'region'}`;
          const canPaint = eligibleKeys.has(selectionKey);
          return <button key={selectionKey} type="button" className={`wuyu-rs-region-choice ${selected === selectionKey ? 'is-selected' : ''}`} aria-pressed={selected === selectionKey} onClick={() => setSelection(selectionKey)}>
            <strong>{region.id || t("目标 {v0}", {v0:(index + 1)})}</strong><span>{t("模型类型：")}{region.label || t("未提供")}</span><small>{canPaint ? t("已在影像标注") : unavailableLabel(region, answer)}</small>
          </button>;
        })}
      </div>
      {selectedRegion && <dl className="wuyu-rs-region-details" aria-live="polite">
        <div><dt>{t("所选目标")}</dt><dd>{selectedRegion.id || t("目标 {v0}", {v0:(selectedIndex + 1)})} {t("· 待核查疑点")}</dd></div>
        <div><dt>{t("模型类型")}</dt><dd>{selectedRegion.label || t("未提供")}</dd></div>
        <div><dt>{t("影像来源")}</dt><dd>{attributionLabel(selectedRegion, images)}</dd></div>
        <div><dt>{t("图像坐标")}</dt><dd>{validAnnotationBox(selectedRegion.bbox) && selectedRegion.coord === 'normalized_1000' ? t("[{v0}]（0–1000 归一化坐标）", {v0:(selectedRegion.bbox.join(', '))}) : t("坐标无效或格式未明确")}</dd></div>
        <div><dt>{t("本轮模型风险")}</dt><dd>{riskText}</dd></div>
        <div><dt>{t("本轮模型自报评分")}</dt><dd>{scoreText}{scoreText !== '未提供' && <span className="wuyu-rs-score-note">{t("（未经校准）")}</span>}</dd></div>
      </dl>}
    </div>;
}

export default function RemoteSensingAnnotations({result, images = []}) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();
  if (!result?.isRemoteSensing || !['complete', 'invalid'].includes(result.status) || !result.regions?.length) return null;
  const label = t("{v0}影像标注（{v1}处疑点）", {v0:(expanded ? t("收起") : t("查看")), v1:(result.regions.length)});
  return <section className="wuyu-rs-annotations">
    <button type="button" className="wuyu-rs-annotation-action" aria-label={t(label)} aria-expanded={expanded} aria-controls={panelId} aria-describedby={`${panelId}-description`} onClick={() => setExpanded(value => !value)}>
      <ScanLine size={19} aria-hidden="true"/>
      <span className="wuyu-rs-annotation-action-copy"><strong>{t(label)}</strong><small id={`${panelId}-description`}>{t("按需查看目标位置、模型类型和本轮风险，结果供现场核查参考。")}</small></span>
      <ChevronDown size={17} aria-hidden="true"/>
    </button>
    <div id={panelId} className="wuyu-rs-annotation-panel" hidden={!expanded}>
      {expanded && <RemoteSensingAnnotationContent result={result} images={images}/>}
    </div>
  </section>;
}
