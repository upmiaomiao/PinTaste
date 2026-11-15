"""Versioned, evidence-preserving core reports over an owner-checked conversation.

Authorization belongs to the portal. This module only accepts the authorized
turns and saved packages handed to it; it never opens an asset-store path,
fetches a URL, acquires imagery, or calls a model. Original task artifacts remain
unchanged. A question/answer supplement is never promoted into a new observation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import tempfile
from threading import Lock
from urllib.parse import urlsplit

from .answer_layout import concise_lead, public_review_text, readable_blocks
from .consumer_conclusion import package_consumer_conclusion
from .task_modes import LABELS, stored_workflow
from .task_outputs import presentation_contract


REPORT_NAMES = ('report.html', 'report.md', 'report.docx')
MAX_TURNS = 500
_LOCK = Lock()
_ID = re.compile(r'[a-f0-9]{32}')


def _mapping(value):
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value, limit=24000):
    if isinstance(value, (list, tuple)):
        value = '\n\n'.join(_text(row, limit) for row in value)
    return public_review_text(str(value or '').strip())[:limit]


def _scope(task, assets):
    """Different periods/places remain separate even within the same chat."""
    value = {key: task.get(key) for key in ('center', 'bbox', 'date_start', 'date_end')}
    if not value['center'] and not value['bbox']:
        value['uploaded_asset_ids'] = sorted(str(a.get('derived_from_asset_id') or a.get('id'))
            for a in assets if isinstance(a, Mapping) and a.get('id'))
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16], value


def _safe_url(value):
    if not isinstance(value, str) or '\\' in value:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _mode(package, task):
    return stored_workflow(package.get('task') or task)


def _capture_date(package, asset):
    row = next((row for row in _mapping(package.get('task_results')).get('timeline', [])
        if isinstance(row, Mapping) and row.get('asset_id') == asset.get('id')), {})
    value = row.get('actual_capture_date')
    if row.get('actual_capture_date_verified') is not True or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _date_label(ref):
    if ref.get('actual_capture_date'):
        return str(ref['actual_capture_date'])
    if ref.get('reported_capture_date'):
        return str(ref['reported_capture_date'])+'（提供方记录；实际日期适用范围未核实）'
    return '实际拍摄日期未核实'


def _decision(package, mode):
    if mode == 'environmental_risk':
        risk = _mapping(package.get('environmental_risk'))
        final = _mapping(risk.get('final_review'))
        return str(risk.get('final_level')) if risk.get('status') == 'completed' and final.get('status') == 'completed' else None
    if mode == 'temporal_change':
        facts = presentation_contract(package).get('temporal_facts') or {}
        return json.dumps({key: facts.get(key) for key in ('first_positive_date', 'latest_positive_date', 'onset_interval')}, sort_keys=True) if facts.get('first_positive_date') else None
    fusion = _mapping(package.get('fusion'))
    if fusion.get('level') in {'high', 'medium', 'low'}:
        return str(fusion['level'])
    return None


def build_core_report(conversation_id: str, turns: Sequence[Mapping], *,
                      packages: Mapping | Callable[[str], Mapping] | None = None,
                      through_task_id: str | None = None) -> dict:
    """Build an immutable snapshot from chronological, same-owner turns.

    ``packages`` may be an authorized lazy loader. No client-supplied path is
    accepted. ``through_task_id`` produces the accumulated report at that turn.
    """
    if not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id):
        raise ValueError('核心报告的会话标识无效')
    if not turns or len(turns) > MAX_TURNS:
        raise ValueError('核心报告需要1至500轮经校验的会话记录')
    selected = list(turns)
    if through_task_id is not None:
        index = next((i for i, row in enumerate(selected) if row.get('id') == through_task_id), None)
        if index is None:
            raise ValueError('报告快照不属于本会话')
        selected = selected[:index+1]
    ids = [row.get('id') for row in selected]
    if any(not isinstance(identity, str) or not _ID.fullmatch(identity) for identity in ids) or len(set(ids)) != len(ids):
        raise ValueError('核心报告的轮次标识无效或重复')
    if ids[0] != conversation_id:
        raise ValueError('核心报告必须从本会话首轮开始')
    rows, revisions, sources, evidence = [], [], [], []
    primary = None
    root_scope = None
    root_mode = None
    latest_facets, conflicts = {}, []
    for index, turn in enumerate(selected, 1):
        identity = turn['id']
        raw = packages(identity) if callable(packages) else (packages or {}).get(identity, {})
        package = _mapping(raw or turn.get('package') or turn.get('result'))
        task = _mapping(package.get('task') or turn.get('task'))
        assets = [a for a in package.get('assets', turn.get('images', [])) if isinstance(a, Mapping)]
        mode = _mode(package, task)
        root_mode = root_mode or mode
        scope_key, scope = _scope(task, assets)
        root_scope = root_scope or scope_key
        figure_refs = []
        for figure, asset in enumerate(assets, 1):
            aid = asset.get('id')
            if not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', aid):
                continue
            ref = {'id': f'T{index:03d}-F{figure:03d}', 'task_id': identity, 'turn_index': index,
                'asset_id': aid, 'figure': f'图{figure}', 'label': f'第{index}轮·图{figure}',
                'url': f'../../../assets/{aid}', 'source': _text(_mapping(asset.get('metadata')).get('source'), 100),
                'actual_capture_date': _capture_date(package, asset),
                'reported_capture_date': _mapping(asset.get('metadata')).get('acquisition_time'),
                'note': re.sub(r'^同图复核[：:]\s*', '', _text(next((row.get('analysis_summary') for row in package.get('image_assessments', [])
                    if isinstance(row, Mapping) and row.get('asset_id') == aid), ''), 600))}
            figure_refs.append(ref); evidence.append(ref)
        local_sources = []
        policy = _mapping(package.get('policy') or turn.get('policy'))
        policy_sources = [source for source in policy.get('sources', []) if isinstance(source, Mapping)]
        cited = set(_mapping(package.get('narrative')).get('cited_policy_ids') or [])
        cited.update(_mapping(package.get('contextual_answer')).get('cited_policy_ids') or [])
        # Keep actual citations ahead of the unreferenced discovery catalogue.
        # Later turns often reuse old sources before adding the new local law.
        policy_sources.sort(key=lambda source: str(source.get('id')) not in cited)
        for source in policy_sources[:max(40, len(cited))]:
            if not isinstance(source, Mapping) or not (url := _safe_url(source.get('url'))):
                continue
            original = str(source.get('id') or f'R{len(local_sources)+1:03d}')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', original):
                continue
            local_sources.append({'id': f'T{index:03d}-{original}', 'original_id': original,
                'task_id': identity, 'turn_index': index, 'title': _text(source.get('title') or '参考资料', 500),
                'url': url, 'legal_status': str(source.get('legal_status') or 'unverified'),
                'published_at': source.get('published_at'), 'retrieved_at': source.get('retrieved_at')})
        sources.extend(local_sources)

        def projected(value):
            result = _text(value)
            for source in local_sources:
                result = re.sub(r'(?<![A-Za-z0-9_-])'+re.escape(source['original_id'])+r'(?![A-Za-z0-9_-])', source['id'], result)
            return result

        def entry(value, *, kind='model_conclusion'):
            return {'text': projected(value), 'kind': kind, 'task_id': identity, 'turn_index': index,
                'scope_key': scope_key, 'figure_refs': figure_refs, 'source_refs': [s['id'] for s in local_sources]}

        completed = turn.get('status') in {'succeeded', 'completed', 'complete'} and bool(package)
        contract = presentation_contract(package) if package else {}
        consumer = package_consumer_conclusion(package) if package else {}
        lead = _text(consumer.get('text') or contract.get('lead') or _mapping(package.get('narrative')).get('summary') or turn.get('summary'))
        is_answer = mode == 'contextual_qa' or bool(task.get('search_only'))
        body = []
        if completed and lead:
            body.append(entry(lead, kind='answer_supplement' if is_answer else 'evidence_conclusion'))
        if completed:
            supplements = _mapping(_mapping(package.get('contextual_answer')).get('user_supplements'))
            if supplements.get('jurisdiction') and supplements.get('provenance') == 'current_user_statement':
                body.append(entry('辖区：'+str(supplements['jurisdiction'])+'（用户补充，尚未独立核实；不代替影像定位证明）', kind='user_statement'))
            for section in contract.get('sections', []):
                if not isinstance(section, Mapping):
                    continue
                for value in section.get('paragraphs', []):
                    if _text(value): body.append({**entry(value, kind='answer_supplement' if is_answer else 'evidence_detail'),
                        'heading': _text(section.get('heading'), 150)})
                columns, records = section.get('columns') or [], section.get('records') or []
                if columns and records:
                    body.append({**entry('', kind='answer_supplement' if is_answer else 'evidence_detail'),
                        'heading': _text(section.get('heading'), 150), 'columns': [_text(v, 500) for v in columns],
                        'records': [[projected(v) for v in record] for record in records if isinstance(record, (list, tuple))][:100]})
        pending = [*_mapping(package.get('recognition')).get('unknowns', []),
            *_mapping(package.get('environmental_risk')).get('next_steps', []),
            *_mapping(package.get('environmental_risk')).get('limitations', [])]
        row = {'task_id': identity, 'turn_index': index, 'mode': mode, 'scope_key': scope_key, 'scope': scope,
            'objective': _text(task.get('objective') or turn.get('objective'), 1000), 'completed': completed,
            'lead': projected(lead), 'entries': body, 'created_at': turn.get('created_at'), 'updated_at': turn.get('updated_at'),
            'status': turn.get('status'), 'pending': [entry(value, kind='verification_needed') for value in pending if isinstance(value, str) and value.strip()],
            'report_url': f'../../../tasks/{identity}/artifacts/report.html'}
        rows.append(row)
        if completed and not is_answer:
            key = (scope_key, mode)
            decision = _decision(package, mode)
            old = latest_facets.get(key)
            if old and decision and old['decision'] and decision != old['decision']:
                conflicts.append({'scope_key': scope_key, 'mode': mode, 'before_task_id': old['row']['task_id'],
                    'after_task_id': identity, 'text': f'第{old["row"]["turn_index"]}轮与第{index}轮的{LABELS.get(mode,"任务")}结论发生调整；两轮依据分别保留，不删除旧记录。'})
            # An incomplete/failed risk review cannot overwrite a supported grade.
            if not old or decision or not old['decision']:
                latest_facets[key] = {'row': row, 'decision': decision}
            if scope_key == root_scope and lead and (mode == root_mode or primary is None):
                if primary is None or decision or not primary.get('decision'):
                    primary = {'row': row, 'decision': decision}
        revisions.append({'revision': index, 'task_id': identity, 'updated_at': turn.get('updated_at') or turn.get('created_at'),
            'question': row['objective'], 'mode': mode, 'status': turn.get('status'), 'included': completed,
            'change': ('补充解释与资料；未新增影像观测事实' if is_answer else '新增或更新本轮影像证据及任务结论') if completed else '本轮尚未可靠完成；沿用先前已有结论'})

    first_task = _mapping(selected[0].get('task'))
    topic = _text(first_task.get('objective') or rows[0]['objective'] or '遥感核查', 1000)
    heading, _ = concise_lead(topic, 70)
    answer_base = next((row for row in rows if row['completed'] and row['lead']), None)
    core_lead = primary['row']['lead'] if primary else ('资料答复（本轮未新增影像核查）：'+answer_base['lead'] if answer_base else '本会话尚未形成已完成的核查结论；已收集的要求和执行状态保留如下。')
    summary, remainder = concise_lead(core_lead, 220)
    sections = []
    if remainder:
        basis = primary['row'] if primary else answer_base
        sections.append({'id': 'core-detail', 'heading': '核心判断依据', 'entries': [{**basis['entries'][0], 'text': remainder}]})
    for (scope_key, mode), facet in latest_facets.items():
        row = facet['row']
        sections.append({'id': f'facet-{scope_key}-{mode}', 'heading': LABELS.get(mode, '遥感核查') + (' · 其他地点或时段' if scope_key != root_scope else ''),
            'scope_key': scope_key, 'scope': row['scope'], 'entries': row['entries'], 'active_task_id': row['task_id']})
    supplements = [row for row in rows if row['completed'] and (row['mode'] == 'contextual_qa' or _mapping(selected[row['turn_index']-1].get('task')).get('search_only'))]
    for row in supplements:
        sections.append({'id': f'supplement-{row["task_id"]}', 'heading': f'第{row["turn_index"]}轮补充 · {row["objective"][:65]}',
            'scope_key': row['scope_key'], 'scope': row['scope'], 'entries': row['entries'],
            'evidence_boundary': '本节为本轮问答与检索补充。未执行新影像核查的内容，不视为新增影像事实或已核实违规。'})
    if conflicts:
        sections.append({'id': 'revisions', 'heading': '结论修正与分歧', 'entries': [{'text': c['text'], 'kind': 'revision', **c} for c in conflicts]})
    pending_entries, seen_pending = [], set()
    for facet in latest_facets.values():
        for value in facet['row']['pending']:
            if value['text'] not in seen_pending:
                pending_entries.append(value); seen_pending.add(value['text'])
    if pending_entries:
        sections.append({'id': 'unresolved', 'heading': '仍需核实与下一步', 'entries': pending_entries})
    # Include every completed turn in a short audit ledger even if a newer
    # evidence review superseded that facet's visible body.
    report = {'schema': 'jianwei-core-report-v14', 'conversation_id': conversation_id, 'id': conversation_id,
        'title': '无隅见微核心报告 · ' + heading, 'topic': topic, 'revision': len(rows), 'version': len(rows),
        'updated_at': rows[-1]['updated_at'] or rows[-1]['created_at'], 'summary': summary, 'lead': summary,
        'status': 'completed' if primary else 'answer_materials' if answer_base else 'collecting_evidence', 'core_task_id': primary['row']['task_id'] if primary else None,
        'root_scope_key': root_scope, 'sections': sections, 'revisions': revisions, 'sources': sources, 'evidence': evidence,
        'task_ids': ids[:len(rows)], 'conflicts': conflicts,
        'limitations': ['核心结论仅依据已保存且完成的影像任务；后续问答是解释或资料补充。',
            '不同地点、拍摄时段与来源分别追溯；没有新证据时不把问答复述升级为现场事实。'],
        'models_called': False, 'imagery_reacquired': False, 'original_artifacts_replaced': False}
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str).encode()
    report['evidence_digest'] = hashlib.sha256(payload).hexdigest()
    return report



MAX_REPORT_IMAGES = 24
_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}


def _prepare_images(image_files):
    """Return {asset_id: {'src': jpeg bytes, 'ann': jpeg bytes}} from server-owned files only."""
    from io import BytesIO
    from PIL import Image
    result = {}
    for asset_id, pair in list((image_files or {}).items())[:MAX_REPORT_IMAGES]:
        if not isinstance(asset_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', asset_id):
            continue
        row = {}
        for key in ('src', 'ann'):
            path = (pair or {}).get(key)
            if not path:
                continue
            path = Path(path)
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES or path.stat().st_size > 30_000_000:
                    continue
                with Image.open(BytesIO(path.read_bytes())) as image:
                    if image.width * image.height > 80_000_000:
                        continue
                    image.load()
                    image = image.convert('RGB')
                    image.thumbnail((1200, 1200))
                    out = BytesIO()
                    image.save(out, format='JPEG', quality=84)
                    row[key] = out.getvalue()
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
        if row:
            result[asset_id] = row
    return result


def _figure_refs(report, images):
    return [ref for ref in report.get('evidence', []) if ref.get('asset_id') in (images or {})]


def _html_figures(report, images):
    import base64
    esc = lambda value: html.escape(str(value or ''))
    refs = _figure_refs(report, images)
    if not refs:
        return ''
    cards = []
    for ref in refs:
        pair = images[ref['asset_id']]
        views = [(label, pair[key]) for label, key in (('原始影像', 'src'), ('见微标注', 'ann')) if pair.get(key)]
        media = ''.join('<figure><img alt="'+esc(ref['label']+' '+label)+'" src="data:image/jpeg;base64,'+base64.b64encode(data).decode()+'"><figcaption>'+label+'</figcaption></figure>' for label, data in views)
        note = ref.get('note') or '本图暂无单独结论，见上方核心结论。'
        cards.append('<article class="fig"><div class="fig-media">'+media+'</div><div class="fig-text"><h3><span class="fig-no">'+esc(ref['label'])+'</span> '+esc(ref.get('source') or '影像')+'</h3><p>'+esc(note)+'</p><p class="meta">拍摄日期：'+esc(_date_label(ref))+'</p></div></article>')
    return '<section class="figs"><h2>依据影像与逐图结论</h2>'+''.join(cards)+'</section>'


def _word_figures(doc, report, images):
    from io import BytesIO
    from docx.shared import Inches
    from .docx_export import _paragraph
    refs = _figure_refs(report, images)
    if not refs:
        return
    doc.add_heading('依据影像与逐图结论', level=1)
    for ref in refs:
        pair = images[ref['asset_id']]
        views = [(label, pair[key]) for label, key in (('原始影像', 'src'), ('见微标注', 'ann')) if pair.get(key)]
        doc.add_heading(f'{ref["label"]} · {ref.get("source") or "影像"}', level=2)
        table = doc.add_table(rows=1, cols=len(views))
        width = Inches(3.2 if len(views) > 1 else 4.8)
        for cell, (label, data) in zip(table.rows[0].cells, views):
            cell.paragraphs[0].add_run(label).bold = True
            cell.add_paragraph().add_run().add_picture(BytesIO(data), width=width)
        _paragraph(doc, ref.get('note') or '本图暂无单独结论，见上方核心结论。')
        _paragraph(doc, f'拍摄日期：{_date_label(ref)}。原图与标注属于同一份证据，标注为待核查候选位置。', style='Caption')


def _blocks(value):
    return [part.strip() for part in readable_blocks(_text(value)).split('\n\n') if part.strip()]


def _html_blocks(value):
    result = []
    for block in _blocks(value):
        lines = block.splitlines()
        if all(re.match(r'^\s*(?:[-*+]\s|\d+\.\s)', line) for line in lines):
            result.append('<ul>' + ''.join('<li>'+html.escape(re.sub(r'^\s*(?:[-*+]\s|\d+\.\s)', '', line))+'</li>' for line in lines) + '</ul>')
        else:
            result.append('<p>'+html.escape(block).replace('\n', '<br>')+'</p>')
    return ''.join(result)


def render_core_report_markdown(report):
    lines = ['# '+_text(report['title']), '', f'版本 {report["revision"]} · 更新：{report.get("updated_at") or "未记录"}', '', '## 核心结论', '', _text(report['lead']), '']
    for section in report['sections']:
        lines.extend(['## '+_text(section['heading']), ''])
        if section.get('evidence_boundary'): lines.extend([_text(section['evidence_boundary']), ''])
        for entry in section['entries']:
            if entry.get('heading'): lines.extend(['### '+_text(entry['heading']), ''])
            if entry.get('text'): lines.extend([readable_blocks(_text(entry['text'])), ''])
            if entry.get('columns') and entry.get('records'):
                lines.extend(['| '+' | '.join(entry['columns'])+' |', '| '+' | '.join('---' for _ in entry['columns'])+' |'])
                lines.extend('| '+' | '.join(str(v).replace('|','／').replace('\n',' ') for v in row)+' |' for row in entry['records']); lines.append('')
            if entry.get('turn_index'): lines.extend([f'依据：第{entry["turn_index"]}轮；'+ '、'.join(ref['label'] for ref in entry.get('figure_refs', [])), ''])
    lines.extend(['## 会话修订记录', ''])
    for revision in report['revisions']:
        lines.extend([f'- 第{revision["revision"]}轮：{revision["question"]}；{revision["change"]}', ''])
    lines.extend(['## 来源与原始影像索引', ''])
    for source in report['sources']:
        lines.extend([f'- [{source["id"]}] {source["title"]}：{source["url"]}', ''])
    for ref in report['evidence']:
        lines.extend([f'- {ref["label"]}：来源 {_text(ref.get("source")) or "未记录"}；日期 {_date_label(ref)}；影像编号 {ref["asset_id"]}', ''])
    lines.extend(['## 适用范围', '', *('- '+_text(v) for v in report['limitations'])])
    return '\n'.join(lines)+'\n'


def render_core_report_html(report, images=None):
    esc = lambda value: html.escape(str(value or ''))
    content = ['<header><span class="eyebrow">无隅见微 · 会话核心报告</span><h1>'+esc(report['topic'])+'</h1><p class="meta">版本 '+str(report['revision'])+' · 更新：'+esc(report.get('updated_at'))+'</p></header>',
        '<section class="core"><h2>核心结论</h2>'+_html_blocks(report['lead'])+'</section>', _html_figures(report, images)]
    for section in report['sections']:
        content.append('<section><h2>'+esc(section['heading'])+'</h2>')
        if section.get('evidence_boundary'): content.append('<p class="meta">'+esc(section['evidence_boundary'])+'</p>')
        for entry in section['entries']:
            if entry.get('heading'): content.append('<h3>'+esc(entry['heading'])+'</h3>')
            if entry.get('text'): content.append(_html_blocks(entry['text']))
            if entry.get('columns') and entry.get('records'):
                content.append('<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(v)+'</th>' for v in entry['columns'])+'</tr></thead><tbody>')
                content.extend('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in entry['records'])
                content.append('</tbody></table></div>')
            if entry.get('turn_index'):
                content.append('<p class="meta">依据：第'+str(entry['turn_index'])+'轮'+(' · '+'、'.join(esc(ref['label']) for ref in entry.get('figure_refs', [])) if entry.get('figure_refs') else '')+'</p>')
        content.append('</section>')
    content.append('<details><summary>会话修订与原始依据</summary><ol>')
    for revision in report['revisions']:
        content.append('<li><strong>第'+str(revision['revision'])+'轮</strong> '+esc(revision['question'])+'<p class="meta">'+esc(revision['change'])+'</p><a href="../../../tasks/'+esc(revision['task_id'])+'/artifacts/report.html">查看本轮原始报告</a></li>')
    content.append('</ol>')
    for ref in report['evidence']:
        content.append('<p><a href="'+esc(ref['url'])+'">'+esc(ref['label'])+'</a> · '+esc(ref.get('source') or '来源未记录')+' · '+esc(_date_label(ref))+'</p>')
    content.append('</details><details><summary>引用来源</summary><ul>')
    for source in report['sources']:
        content.append('<li><a href="'+esc(source['url'])+'" rel="noopener noreferrer">['+esc(source['id'])+'] '+esc(source['title'])+'</a></li>')
    content.append('</ul></details><footer>'+''.join('<p>'+esc(v)+'</p>' for v in report['limitations'])+'</footer>')
    css="body{font:17px/1.85 system-ui,'Microsoft YaHei',sans-serif;max-width:880px;margin:44px auto;padding:0 24px;color:#202124;background:#fff}h1{font-size:27px;line-height:1.45}h2{font-size:21px;margin:32px 0 18px}h3{font-size:17px;margin:22px 0 12px}p,li{overflow-wrap:anywhere}p{margin:12px 0 18px}li{margin:12px 0}section{margin:30px 0}.core{padding:20px 24px;background:#f6f8f7;border-radius:14px}.core h2{margin-top:0}.meta,.eyebrow,footer{color:#626a73;font-size:13px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;text-align:left;border:1px solid #ddd;vertical-align:top}.table-wrap{overflow:auto;max-width:100%}details{margin:24px 0;border-top:1px solid #ddd;padding-top:18px}summary{cursor:pointer;font-weight:600}a{color:inherit;text-underline-offset:3px}.figs h2{margin-bottom:18px}.fig{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:20px;margin:0 0 20px;padding:16px;border:1px solid #d5e5e0;border-radius:16px;background:#f7fbfa;break-inside:avoid}.fig-media{display:flex;gap:10px;flex-wrap:wrap}.fig figure{margin:0;flex:1 1 150px}.fig img{width:100%;border-radius:10px;display:block}.fig figcaption{margin-top:6px;font-size:13px;color:#626a73;text-align:center}.fig h3{margin:0 0 8px;font-size:17px}.fig-no{display:inline-block;padding:1px 10px;border-radius:8px;background:#0f766e;color:#fff;font-size:14px}.fig p{margin:6px 0}@media(max-width:700px){.fig{grid-template-columns:1fr}}@media(max-width:600px){body{padding:0 16px;margin:25px auto}h1{font-size:23px}}@media print{details{display:block}h2{break-after:avoid}.core{break-inside:avoid}}"
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(report['title'])+'</title><style>'+css+'</style><body>'+''.join(content)+'</body></html>'


def export_core_report_docx(report, path: Path, images=None):
    from .docx_export import _document, _paragraph, _save, _table
    path = Path(path)
    root = path.parent.resolve(strict=True)
    if path.is_symlink() or path.name != 'report.docx':
        raise ValueError('核心Word报告输出路径无效')
    doc = _document(_text(report['title'], 120))
    _paragraph(doc, f'版本 {report["revision"]} · 更新：{report.get("updated_at") or "未记录"}', style='Caption')
    doc.add_heading('核心结论', level=1)
    _word_blocks(doc, report['lead'])
    _word_figures(doc, report, images)
    for section in report['sections']:
        doc.add_heading(_text(section['heading'], 200), level=1)
        if section.get('evidence_boundary'): _paragraph(doc, section['evidence_boundary'], style='Caption')
        for entry in section['entries']:
            if entry.get('heading'): doc.add_heading(_text(entry['heading']), level=2)
            _word_blocks(doc, entry.get('text'))
            if entry.get('columns') and entry.get('records'):
                count = len(entry['columns']); _table(doc, entry['columns'], entry['records'], [6.9/count]*count)
            if entry.get('turn_index'): _paragraph(doc, f'依据：第{entry["turn_index"]}轮；'+'、'.join(ref['label'] for ref in entry.get('figure_refs', [])), style='Caption')
    doc.add_heading('会话修订记录', level=1)
    for revision in report['revisions']:
        _paragraph(doc, f'第{revision["revision"]}轮：{revision["question"]}')
        _paragraph(doc, revision['change'], style='Caption')
    doc.add_heading('来源与原始影像索引', level=1)
    for source in report['sources']: _paragraph(doc, f'[{source["id"]}] {source["title"]}\n{source["url"]}')
    for ref in report['evidence']: _paragraph(doc, f'{ref["label"]}；来源 {ref.get("source") or "未记录"}；日期 {_date_label(ref)}；原始影像编号 {ref["asset_id"]}', style='Caption')
    base = report.get('public_base_url')
    if base:
        doc.add_heading('原始证据链接', level=1)
        for revision in report['revisions']:
            _word_link(doc, f'第{revision["revision"]}轮原始报告', base+'/portal/api/tasks/'+revision['task_id']+'/artifacts/report.html')
        for ref in report['evidence']:
            _word_link(doc, ref['label']+'原始影像', base+'/portal/api/assets/'+ref['asset_id'])
    doc.add_heading('适用范围', level=1)
    for value in report['limitations']: _paragraph(doc, value)
    return _save(doc, root, path.name, [])


def _word_blocks(doc, value):
    from .docx_export import _paragraph
    for block in _blocks(value):
        lines = block.splitlines()
        if all(re.match(r'^\s*[-*+]\s+', line) for line in lines):
            for line in lines: _paragraph(doc, re.sub(r'^\s*[-*+]\s+', '', line), style='List Bullet')
        elif all(re.match(r'^\s*\d+\.\s+', line) for line in lines):
            for line in lines:
                match = re.match(r'^\s*(\d+)\.\s+(.*)', line)
                paragraph = doc.add_paragraph(match[2], style='List Number')
                _word_number_start(doc, paragraph, int(match[1]))
        else:
            _paragraph(doc, block)


def _word_number_start(doc, paragraph, number):
    """Keep the writer's explicit number even when separated by prose/sections."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    numbering = doc.part.numbering_part.element
    style_number = doc.styles['List Number'].element.find(qn('w:pPr')).find(qn('w:numPr')).find(qn('w:numId')).get(qn('w:val'))
    original = next(node for node in numbering.findall(qn('w:num')) if node.get(qn('w:numId')) == style_number)
    abstract = original.find(qn('w:abstractNumId')).get(qn('w:val'))
    identity = max(int(node.get(qn('w:numId'))) for node in numbering.findall(qn('w:num'))) + 1
    node = OxmlElement('w:num'); node.set(qn('w:numId'), str(identity))
    base = OxmlElement('w:abstractNumId'); base.set(qn('w:val'), abstract); node.append(base)
    override = OxmlElement('w:lvlOverride'); override.set(qn('w:ilvl'), '0')
    start = OxmlElement('w:startOverride'); start.set(qn('w:val'), str(number)); override.append(start); node.append(override)
    numbering.append(node)
    properties = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    properties.get_or_add_ilvl().val = 0; properties.get_or_add_numId().val = identity


def _word_link(doc, label, url):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE
    if not _safe_url(url): return
    paragraph = doc.add_paragraph()
    element = OxmlElement('w:hyperlink')
    element.set(qn('r:id'), paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
    run = OxmlElement('w:r'); text = OxmlElement('w:t'); text.text = label
    run.append(text); element.append(run); paragraph._p.append(element)


def core_report_files(root: Path, report: dict, *, public_base_url=None, image_files=None) -> dict[str, Path]:
    """Write a content-addressed derived view below a server-owned directory."""
    supplied = Path(root)
    if supplied.is_symlink(): raise ValueError('核心报告缓存目录无效')
    supplied.mkdir(parents=True, exist_ok=True)
    base = supplied.resolve(strict=True)
    if public_base_url is not None and not _safe_url(public_base_url):
        raise ValueError('核心报告公开地址无效')
    rendered = {**report, 'public_base_url':public_base_url.rstrip('/') if public_base_url else None}
    revision = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('answer_layout.py').read_bytes() + Path(__file__).with_name('docx_export.py').read_bytes()).hexdigest()
    images = _prepare_images(image_files)
    image_hash = hashlib.sha256(b''.join(k.encode()+v.get('src', b'')+v.get('ann', b'') for k, v in sorted(images.items()))).hexdigest()
    digest = hashlib.sha256((str(report['evidence_digest'])+revision+str(rendered['public_base_url'])+image_hash).encode()).hexdigest()
    target = base/digest
    if target.is_symlink(): raise ValueError('核心报告派生目录无效')
    with _LOCK:
        marker = target/'manifest.json'
        if marker.is_file() and not marker.is_symlink():
            manifest = json.loads(marker.read_text(encoding='utf-8'))
            if manifest.get('evidence_digest') == report['evidence_digest'] and all((target/name).is_file() and not (target/name).is_symlink() and hashlib.sha256((target/name).read_bytes()).hexdigest() == manifest.get('files', {}).get(name) for name in REPORT_NAMES):
                return {name: target/name for name in REPORT_NAMES}
            raise ValueError('核心报告缓存校验失败')
        if target.exists(): raise ValueError('核心报告派生目录尚未完成')
        temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=base))
        try:
            (temporary/'report.html').write_text(render_core_report_html(rendered, images), encoding='utf-8')
            (temporary/'report.md').write_text(render_core_report_markdown(rendered), encoding='utf-8')
            export_core_report_docx(rendered, temporary/'report.docx', images)
            (temporary/'manifest.json').write_text(json.dumps({'schema':'jianwei-core-report-files-v14',
                'evidence_digest':report['evidence_digest'],'renderer_revision':revision,
                'files':{name:hashlib.sha256((temporary/name).read_bytes()).hexdigest() for name in REPORT_NAMES},
                'models_called':False,'original_artifacts_replaced':False}, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.rename(target)
        finally:
            if temporary.exists() and temporary.parent == base and temporary.name.startswith('.building-'):
                shutil.rmtree(temporary)
    return {name: target/name for name in REPORT_NAMES}
