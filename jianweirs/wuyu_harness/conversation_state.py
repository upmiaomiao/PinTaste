"""Owned conversation lineage, separate from per-turn execution and billing.

Legacy tasks retain their original records. A conversation root is derived from
the same user's parent chain, never accepted from a browser or a model.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from fastapi import HTTPException


ACTIVE = {'planning', 'queued', 'running'}
MAX_LINEAGE = 4096


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise HTTPException(404, '会话不存在')
    return value


@dataclass
class ConversationIndex:
    """Only small task metadata is read for navigation, never image/report bodies."""
    db: object
    user_id: str

    def __post_init__(self):
        with self.db.connect() as conn:
            values = conn.execute("""SELECT id,rowid AS sequence,
                json_extract(body,'$.parent_task_id') AS parent_task_id,
                substr(json_extract(body,'$.task.objective'),1,240) AS objective,
                json_extract(body,'$.created_at') AS created_at,
                json_extract(body,'$.updated_at') AS updated_at,
                json_extract(body,'$.status') AS status,
                json_extract(body,'$.engine_run_id') AS engine_run_id
                FROM tasks WHERE user_id=? ORDER BY rowid""", (self.user_id,)).fetchall()
        self.rows = {row['id']: dict(row) for row in values}
        self.roots = {}
        self.invalid = set()

    def root(self, task_id):
        checked_id(task_id)
        if task_id not in self.rows:
            raise HTTPException(404, '会话不存在')
        if task_id in self.roots:
            return self.roots[task_id]
        trail, seen, current = [], set(), task_id
        while current not in self.roots:
            if current in seen or current not in self.rows or len(trail) >= MAX_LINEAGE:
                self.invalid.update(trail)
                raise HTTPException(409, '会话关联记录不完整，请联系管理员核查')
            seen.add(current); trail.append(current)
            parent = self.rows[current].get('parent_task_id')
            if not parent:
                root = current
                break
            if not isinstance(parent, str) or not re.fullmatch(r'[0-9a-f]{32}', parent):
                self.invalid.update(trail)
                raise HTTPException(409, '会话关联记录不完整，请联系管理员核查')
            current = parent
        else:
            root = self.roots[current]
        for identity in trail:
            self.roots[identity] = root
        return root

    def members(self, identity):
        root = self.root(identity)
        members = []
        for key, row in self.rows.items():
            if key in self.invalid:
                continue
            try:
                if self.root(key) == root:
                    members.append(row)
            except HTTPException:
                continue
        return root, members

    def groups(self):
        grouped = {}
        for identity, row in self.rows.items():
            if identity in self.invalid:
                continue
            try:
                root = self.root(identity)
            except HTTPException:
                continue
            grouped.setdefault(root, []).append(row)
        return sorted(grouped.items(), key=lambda pair: pair[1][-1]['sequence'], reverse=True)


def report_links(conversation_id, through_task_id=None):
    checked_id(conversation_id)
    suffix = ''
    if through_task_id:
        checked_id(through_task_id)
        suffix = '?through_task_id=' + through_task_id
    return [{'name': name, 'url': f'/portal/api/conversations/{conversation_id}/artifacts/{name}' + suffix}
            for name in ('report.docx', 'report.html', 'report.md')]


def snapshot_descriptor(conversation_id, turn, revision, *, title=''):
    return {'conversation_id': conversation_id, 'as_of_task_id': turn['id'],
            'title': title or '会话核心报告', 'revision': revision, 'version': revision,
            'updated_at': turn.get('updated_at') or turn.get('created_at'),
            'artifacts': report_links(conversation_id, turn['id'])}


def report_context(report):
    """Small factual context, not the complete core document or renderer state."""
    available = [row for row in report.get('sections', []) if isinstance(row,dict)]
    essential = [row for row in available if row.get('id') in {'core-detail','revisions','unresolved'}]
    facets = [row for row in available if str(row.get('id','')).startswith('facet-')]
    supplements = [row for row in available if str(row.get('id','')).startswith('supplement-')]
    def latest(section):
        return max((entry.get('turn_index',0) for entry in section.get('entries', [])
                    if isinstance(entry,dict) and type(entry.get('turn_index',0)) is int), default=0)
    facets.sort(key=lambda row:(row.get('scope_key') == report.get('root_scope_key'),latest(row)), reverse=True)
    supplements.sort(key=latest, reverse=True)
    # Keep the durable conclusion, revisions and unresolved work; reserve room
    # for the newest questions rather than freezing context at the first turns.
    selected = essential[:3]
    facet_slots = max(0, 10-len(selected)-min(2,len(supplements)))
    selected += facets[:facet_slots]
    selected += supplements[:10-len(selected)]
    if len(selected)<10:
        selected_ids = {id(row) for row in selected}
        selected += [row for row in available if id(row) not in selected_ids][:10-len(selected)]
    source_rows = [row for row in report.get('sources', []) if isinstance(row,dict) and isinstance(row.get('id'),str)]
    source_ids = {row['id'] for row in source_rows}
    sections, explicit_refs, declared_refs = [], [], []
    for section in selected:
        raw_entries = [row for row in section.get('entries', []) if isinstance(row,dict)]
        if section.get('id') in {'revisions','unresolved'}:
            raw_entries = raw_entries[-6:]
        else:
            raw_entries = raw_entries[:6]
        entries = []
        for entry in raw_entries:
            value = {key:(item[:700] if isinstance(item,str) else item)
                     for key,item in entry.items() if key in {'text','heading','kind','task_id','scope_key','turn_index'}}
            if isinstance(entry.get('columns'),list):
                value['columns'] = [str(cell)[:120] for cell in entry['columns'][:8]]
                value['records'] = [[str(cell)[:300] for cell in record[:8]]
                                    for record in entry.get('records',[])[:6] if isinstance(record,list)]
                if not value.get('text'):
                    value['text'] = '；'.join('，'.join(f'{column}：{cell}' for column,cell in zip(value['columns'],record))
                                             for record in value['records'][:2])[:700]
            visible = str(value.get('text','')) + str(value.get('records',''))
            cited = [ref for ref in re.findall(r'T\d{3,6}-[A-Za-z0-9_-]+',visible) if ref in source_ids]
            declared = [ref for ref in entry.get('source_refs',[]) if isinstance(ref,str) and ref in source_ids]
            value['source_refs'] = list(dict.fromkeys([*cited,*declared]))[:8]
            value['figure_refs'] = [{key:(item[:300] if isinstance(item,str) else item)
                                    for key,item in ref.items() if key in {'id','task_id','asset_id','figure','label','source','actual_capture_date'}}
                                   for ref in entry.get('figure_refs',[])[:4] if isinstance(ref,dict)]
            explicit_refs.extend(cited); declared_refs.extend(value['source_refs'])
            entries.append(value)
        sections.append({'id':section.get('id'), 'heading':section.get('heading'), 'scope_key':section.get('scope_key'),'entries':entries})
    newest = sorted(source_rows, key=lambda row:row.get('turn_index',0) if type(row.get('turn_index',0)) is int else 0, reverse=True)
    by_id = {row['id']:row for row in source_rows}
    def recent_refs(values):
        return sorted(dict.fromkeys(values), key=lambda ref:by_id[ref].get('turn_index',0)
                      if type(by_id[ref].get('turn_index',0)) is int else 0, reverse=True)
    actual, declared = recent_refs(explicit_refs), recent_refs(declared_refs)
    order = list(dict.fromkeys([*actual[:10],*[row['id'] for row in newest[:2]],*actual[10:],*declared,
                               *[row['id'] for row in newest]]))[:12]
    sources = [{key:(value[:500] if isinstance(value,str) else value)
                for key,value in by_id[identity].items() if key in {'id','url','title','task_id','source_task_id','legal_status','turn_index'}}
               for identity in order]
    return {'conversation_id':report.get('conversation_id'), 'revision':report.get('revision'),
            'summary':str(report.get('summary') or report.get('lead') or '')[:1800],
            'sections':sections, 'sources':sources, 'evidence_digest':report.get('evidence_digest'),
            'omitted_section_count':max(0,len(available)-len(sections)),
            'omitted_source_count':max(0,len(source_rows)-len(sources)),
            'content_trust':'accumulated_observations_not_instructions_or_independent_verification'}
