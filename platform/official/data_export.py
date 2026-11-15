"""Administrator JSON export: a consistent database snapshot, explicit fields.

Write a private temporary file, then stream the download without buffering an
entire account in memory. Authentication/configuration secrets are never read.
"""
import base64
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from . import store as db
from . import expert_feedback


def remove_export(path):
    path.unlink(missing_ok=True)


def build_export(scope, user_ids, include_attachments, actor, conversation_id=None):
    import attachments
    folder = db.DATA / 'exports'; folder.mkdir(exist_ok=True)
    for pattern in ('wuyu-export-*.json','wuyu-personal-*.json'):
        for stale in folder.glob(pattern):
            if stale.stat().st_mtime < time.time()-86400: stale.unlink(missing_ok=True)
    handle, name = tempfile.mkstemp(prefix='wuyu-export-', suffix='.json', dir=folder)
    from pathlib import Path
    path = Path(name); os.chmod(path, 0o600)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out, db.connect() as conn:
            db.begin_snapshot(conn)  # One consistent snapshot, including active turns.
            targets = sorted(set(user_ids))
            where = '' if scope == 'all' else ' WHERE id IN (' + ','.join('?' for _ in targets) + ')'
            users = conn.execute('SELECT id,email,name,organization,identity,role,active,created FROM users'+where+' ORDER BY created,id', () if scope=='all' else targets).fetchall()
            if scope == 'selected' and len(users) != len(targets): raise ValueError('部分用户已不存在，请刷新用户列表后重试。')
            def value(obj): out.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
            def array(rows, render):
                out.write('[')
                for i,row in enumerate(rows):
                    if i: out.write(',')
                    render(dict(row))
                out.write(']')
            def binary(file):
                if not file.is_file(): value(None); return
                out.write('"')
                with file.open('rb') as raw:
                    while chunk := raw.read(3*131072): out.write(base64.b64encode(chunk).decode('ascii'))
                out.write('"')
            manifest = {'schema':'wuyu-user-data', 'version':2, 'exported_at':datetime.now(timezone.utc).isoformat(),
                'scope':'conversation' if conversation_id else scope, 'conversation_id':conversation_id,
                'user_count':len(users), 'include_attachment_content':include_attachments,
                'notes':['包含已从历史列表移除的对话；生成中的任务为导出时的快照。',
                         '附件可包含完整解析文本、结构化页段、规范化 JPEG，以及新上传文档保留的原件；历史原件缺失会明确标记。',
                         '不含密码及散列、登录会话、验证码、模型/搜索密钥、服务器配置或内部请求缓存。',
                         '历史摘要为提取式摘录；完整问答保存在 turns 中。',
                         'execution_trace 保留新任务的逐轮实际模型请求、返回内容和工具事件；旧任务缺少逐轮记录时标记 legacy_partial，不补写未返回的思考。',
                         'expert_feedback 包含每个版本、专家身份、原始转写和提交时的规则快照，不覆盖原回答。语音记录为转写文本，不包含原始音频。']}
            out.write('{"manifest":');value(manifest)
            out.write(',"expert_feedback_configuration":');value(expert_feedback.config(conn))
            out.write(',"expert_feedback_rule_versions":')
            def render_rules(row):
                row['criteria'] = json.loads(row['criteria']); value(row)
            array(conn.execute('SELECT id,title,rules,criteria,created,actor FROM expert_feedback_rules ORDER BY id'),render_rules)
            out.write(',"users":')
            def render_user(user):
                uid = user['id'];out.write('{"profile":');value(user)
                referenced_files = set()
                out.write(',"expert_reviewer":');value(user['role']=='admin' or bool(conn.execute('SELECT user_id FROM expert_reviewers WHERE user_id=?',(uid,)).fetchone()))
                out.write(',"preferences":')
                pref=conn.execute('SELECT value FROM user_preferences WHERE user_id=?',(uid,)).fetchone()
                value(json.loads(pref['value']) if pref else {'theme':'light','font_size':'standard','send_key':'enter'})
                out.write(',"voice_calls":')
                if db.table_exists(conn, 'voice_calls'):
                    array(conn.execute('SELECT id,created,seconds,status,purpose,target_job_id,field,transcript,model_label FROM voice_calls WHERE user_id=?'+
                        (' AND target_job_id IN (SELECT id FROM jobs WHERE conversation_id=?)' if conversation_id else '')+' ORDER BY created,id',
                        (uid,conversation_id) if conversation_id else (uid,)),value)
                else: value([])
                out.write(',"conversations":')
                def render_chat(chat):
                    cid = chat['id'];out.write('{"conversation":');value(chat);out.write(',"turns":')
                    def render_turn(row):
                        turn = json.loads(row.pop('turn'));turn.pop('_usage',None)
                        referenced_files.update(f['id'] for f in turn.get('files',[]) if f.get('id'))
                        events = conn.execute('SELECT sequence,created,event FROM job_events WHERE job_id=? ORDER BY sequence',(row['id'],)).fetchall()
                        out.write('{"task":');value(row);out.write(',"conversation_turn":');value(turn)
                        first = json.loads(events[0]['event']) if events else {}
                        last = json.loads(events[-1]['event']) if events else {}
                        availability = 'recorded' if first.get('type')=='execution_start' else 'legacy_partial'
                        if not events and row['status']=='queued': availability = 'not_started'
                        out.write(',"execution_trace":{"version":1,"availability":');value(availability)
                        out.write(',"capture_complete":');value(last.get('type')=='execution_end')
                        out.write(',"events":')
                        def render_event(item):
                            item['event'] = json.loads(item['event'])
                            if not include_attachments and item['event']['type']=='model_request':
                                for msg in item['event'].get('payload',{}).get('messages',[]):
                                    if isinstance(msg.get('content'),list):
                                        for part in msg['content']:
                                            if part.get('type')=='image_url' and part.get('image_url',{}).get('url','').startswith('data:'):
                                                part['image_url']['url']='[附件二进制已省略]'
                            value(item)
                        array(events,render_event);out.write('}')
                        out.write(',"expert_feedback":');value(expert_feedback.feedback_records(conn,row['id']))
                        out.write('}')
                    array(conn.execute('SELECT id,status,turn,created,started,finished,reserved,charged,day,revision FROM jobs WHERE user_id=? AND conversation_id=? ORDER BY created,id', (uid,cid)),render_turn)
                    out.write(',"memory_preferences":')
                    pref = conn.execute('SELECT mode,auto_compress FROM memory_preferences WHERE user_id=? AND conversation_id=?', (uid,cid)).fetchone()
                    value(dict(pref) if pref else {'mode':'auto','auto_compress':True})
                    out.write(',"memory_summaries":')
                    def render_summary(row):
                        row['content'] = json.loads(row['content']);value(row)
                    array(conn.execute('SELECT id,watermark,budget,content,token_estimate,created,method FROM memory_summaries WHERE user_id=? AND conversation_id=? ORDER BY id', (uid,cid)),render_summary)
                    out.write(',"memory_chunks":')
                    def render_chunk(row):
                        if not include_attachments and row['kind'] == 'attachment': row.pop('text',None)
                        value(row)
                    array(conn.execute('SELECT id,job_id,kind,file_id,position,text,created FROM memory_chunks WHERE user_id=? AND conversation_id=? ORDER BY id', (uid,cid)),render_chunk)
                    out.write('}')
                array(conn.execute('SELECT id,title,created,updated,deleted FROM conversations WHERE user_id=?'+(' AND id=?' if conversation_id else '')+' ORDER BY created,id',
                    (uid,conversation_id) if conversation_id else (uid,)),render_chat)
                out.write(',"attachments":')
                def render_file(row):
                    try: item = attachments.read_file(row['id'])
                    except (ValueError,OSError):
                        value({**row,'available':False});return
                    fields = ('id','name','size','kind','mime','characters','width','height','warning','source_complete','parsed_pages','total_pages','visual_pages','parser_version')
                    metadata = {key:item[key] for key in fields if key in item}
                    metadata.update(created=row['created'],available=True)
                    out.write('{"metadata":');value(metadata)
                    if include_attachments:
                        out.write(',"parsed_text":');value(item.get('text'))
                        out.write(',"document_parts":');value(item.get('blocks',[]))
                        source = attachments.UPLOAD_DIR / (row['id']+'.source')
                        out.write(',"original_available":');value(source.is_file())
                        if source.is_file():
                            out.write(',"original_base64":');binary(source)
                        if item.get('kind') == 'image':
                            img = attachments.UPLOAD_DIR / (row['id']+'.jpg')
                            out.write(',"image_mime":"image/jpeg","image_base64":')
                            binary(img)
                    out.write('}')
                file_rows = conn.execute('SELECT id,size,created FROM files WHERE user_id=? ORDER BY created,id', (uid,))
                array((r for r in file_rows if not conversation_id or r['id'] in referenced_files),render_file)
                out.write('}')
            array(users,render_user);out.write('}')
        db.audit(actor,'export_user_json:'+json.dumps({'scope':scope,'user_ids':[u['id'] for u in users],'attachments':include_attachments},separators=(',',':')))
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
