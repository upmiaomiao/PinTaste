import {t} from './i18n.mjs';
import React, { useEffect, useRef, useState } from 'react';
import { BookOpen, Check, ChevronRight, ExternalLink, Globe, MessageCircle, X } from 'lucide-react';
import { sourceCatalog, sourceHost, sourceKind, sourceTitle } from './sources.mjs';
import './sources.css';

const kinds = [{ id: 'webpage', label: '网页', unit: '个', Icon: Globe },
               { id: 'scholar', label: '文献', unit: '篇', Icon: BookOpen },
               { id: 'wechat', label: '公众号', unit: '篇', Icon: MessageCircle }];

function SourceBadge({ source }) {
  const host = sourceHost(source);
  const tone = [...host].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 4;
  return <span aria-hidden="true" className={`source-badge tone-${tone}`}>{host[0].toUpperCase()}</span>;
}

export function SourceButtons({ turn, view, onOpen, children }) {
  const searched = !!turn.request?.network_search && turn.status !== 'streaming';
  if (!searched && !children) return null;
  const sources = searched ? sourceCatalog(turn) : [];
  const citedCount = sources.filter(source => source.cited).length;
  return <div className="source-summary">
    {(!!sources.length || children) && <div className="source-buttons" aria-label={t("回答相关操作")}>{!!sources.length && kinds.map(({ id, label, unit, Icon }) => {
      const group = sources.filter(source => sourceKind(source) === id);
      const icons = [...new Map(group.map(source => [sourceHost(source), source])).values()].slice(0, 3);
      const active = view?.turnId === turn.id && view.kind === id;
      return <button key={id} type="button" className={`source-pill ${active ? 'active' : ''}`} disabled={!group.length}
        aria-label={t("查看{v0}{v1}{v2}来源", {v0: group.length, v1: t(unit), v2: t(label)})} aria-expanded={active} aria-controls="source-panel"
        title={t("{v0}{v1}{v2}，其中{v3}{v4}被正文引用", {v0: group.length, v1: t(unit), v2: t(label), v3: group.filter(source => source.cited).length, v4: t(unit)})}
        onClick={event => onOpen(turn, id, null, event.currentTarget)}>
        {icons.length ? <span className="source-avatar-stack">{icons.map(source => <SourceBadge key={sourceHost(source)} source={source}/>)}</span> : <Icon size={17}/>}
        <span>{group.length} {t(unit)}{t(label)}</span><ChevronRight size={14}/>
      </button>;
    })}{children}</div>}
    {searched && (sources.length ? <p className="source-summary-note">{t("正文引用 {count} 个来源 · 点击查看详情",{count:citedCount})}</p>
      : <p className="source-summary-note">{turn.toolCalls?.length ? t("本次搜索未取得可用来源。") : t("本次未调用搜索工具。")}</p>)}
    {!!sources.length && !citedCount && <p className="source-summary-note">{t("检索结果已保留，回答尚未标明可匹配的引用。")}</p>}
    {!!turn.searchMeta?.unmatched_ids?.length && <p className="citation-warning">{t("未匹配的引用：")}{turn.searchMeta.unmatched_ids.join('、')}</p>}
  </div>;
}

export function SourcePanel({ turn, view, onKindChange, onClose }) {
  const [citedOnly, setCitedOnly] = useState(false);
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 1080px)').matches);
  const panel = useRef(null), closeButton = useRef(null), list = useRef(null), cards = useRef(new Map());
  const sources = sourceCatalog(turn);
  const group = sources.filter(source => sourceKind(source) === view.kind);
  const shown = group.filter(source => !citedOnly || source.cited);
  const citedCount = group.filter(source => source.cited).length;

  useEffect(() => {
    const media = window.matchMedia('(max-width: 1080px)');
    const update = () => setCompact(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => { closeButton.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => { setCitedOnly(false); }, [view.sourceId, view.kind, view.openKey]);
  useEffect(() => {
    if (view.sourceId) {
      const element = cards.current.get(view.sourceId);
      if (element && list.current) list.current.scrollTo({ top: list.current.scrollTop + element.getBoundingClientRect().top - list.current.getBoundingClientRect().top - 12 });
    } else if (list.current) list.current.scrollTop = 0;
  }, [view.sourceId, view.kind, citedOnly, view.openKey]);

  function keyboard(event) {
    if (event.key === 'Escape') { event.stopPropagation(); onClose(); return; }
    if (!compact || event.key !== 'Tab') return;
    const focusable = [...panel.current.querySelectorAll('button:not(:disabled),a[href],input,summary,[tabindex]')]
      .filter(element => element.tabIndex >= 0 && element.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  return <>
    {compact && <button className="source-panel-scrim" aria-label={t("关闭来源面板遮罩")} tabIndex={-1} onClick={onClose}/>}
    <aside className="source-panel" id="source-panel" ref={panel} role={compact ? 'dialog' : 'complementary'}
      aria-modal={compact || undefined} aria-labelledby="source-panel-title" onKeyDown={keyboard}>
      <header className="source-panel-header"><h2 id="source-panel-title">{t("搜索来源")}</h2>
        <button className="icon-button" ref={closeButton} aria-label={t("关闭来源面板")} title={t("关闭")} onClick={onClose}><X size={20}/></button></header>
      <div className="source-panel-tabs" role="tablist" aria-label={t("来源类型")}>{kinds.map(({ id, label, Icon }) =>
        <button key={id} id={`source-tab-${id}`} role="tab" aria-selected={view.kind === id} aria-controls="source-results"
          tabIndex={view.kind === id ? 0 : -1} onClick={() => onKindChange(id)}
          onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault(); const index = kinds.findIndex(kind => kind.id === id);
            const next = kinds[event.key === 'Home' ? 0 : event.key === 'End' ? kinds.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + kinds.length) % kinds.length].id;
            onKindChange(next); document.getElementById(`source-tab-${next}`)?.focus();
          } }}><Icon size={16}/><span>{t(label)}</span><span className="source-tab-count">{sources.filter(source => sourceKind(source) === id).length}</span></button>
      )}</div>
      <div className="source-panel-filter"><span>{t("{count} 个来源 · 已引用 {cited} 个",{count:group.length,cited:citedCount})}</span>
        <label><input type="checkbox" checked={citedOnly} onChange={event => setCitedOnly(event.target.checked)}/>{t("仅看已引用")}</label></div>
      <div className="source-panel-results" ref={list} id="source-results" role="tabpanel" aria-labelledby={`source-tab-${view.kind}`} tabIndex={0}>
        {shown.length ? <ol>{shown.map(source => <li key={source.source_id} ref={element => { if (element) cards.current.set(source.source_id, element); else cards.current.delete(source.source_id); }}
          className={`source-card ${view.sourceId === source.source_id ? 'selected' : ''}`}>
          <div className="source-card-meta"><SourceBadge source={source}/><span title={sourceHost(source)}>{sourceHost(source)}</span>
            {source.date && <time>{source.date}</time>}<span className="source-id">{source.source_id.slice(1)}</span></div>
          <a className="source-card-title" href={source.url} target="_blank" rel="noopener noreferrer">{sourceTitle(source)}<ExternalLink size={13}/></a>
          {source.type === 'scholar' && !!source.authors?.length && <p className="source-card-authors" title={source.authors.join('、')}>{source.authors.join('、')}</p>}
          {source.type === 'wechat' && <p className="source-card-authors">{t("微信公众号 · ")}{source.account || t("账号未标明")}</p>}
          {source.snippet && <details className="source-excerpt"><summary><span>{source.snippet}</span><small>{t("展开摘要")}</small></summary><p>{source.snippet}</p></details>}
          <div className="source-card-footer">{source.cited ? <span className="source-cited"><Check size={12}/>{t("正文已引用")}</span> : <span>{t("检索结果")}</span>}
            {source.year_status === 'unknown' && <span>{t("发表年份未标明")}</span>}</div>
          {source.type === 'wechat' && <div className="source-card-footer"><span>{source.content_kind === 'full_text' ? t("已读取正文") : source.content_kind === 'excerpt' ? t("已读取正文片段") : t("检索摘要")}</span>{!source.date && <span>{t("发布日期未标明")}</span>}</div>}
        </li>)}</ol> : <div className="source-panel-empty"><BookOpen size={27}/><p>{citedOnly ? t("这类来源尚未被正文引用") : t("本次没有{v0}来源", {v0: t(kinds.find(kind => kind.id === view.kind)?.label || '网页')})}</p>
          {citedOnly && <button onClick={() => setCitedOnly(false)}>{t("查看全部来源")}</button>}</div>}
      </div>
      <footer className="source-panel-note">{t("来源编号对应正文引用；检索摘要不代表已阅读全文。")}</footer>
    </aside>
  </>;
}
