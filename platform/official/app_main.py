import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.background import BackgroundTask
from . import store as db
from . import auth
from .static_assets import PublicAssetCache
from .runtime_config import settings as runtime
from . import integrated_identity
from .integrated_identity_api import router as identity_router
from . import memory
from . import data_export
from . import model_profiles
from . import user_settings
from . import quota_limits
from .jobs import Scheduler, public_turn, request_cancel
from .runtime_bootstrap import initialize_runtime
from .remote_sensing_view import enrich_turn

CORE = Path(__file__).resolve().parent.parent / 'model-chat'
sys.path.append(str(CORE))
from server import ChatRequest
import attachments
from pinned_transport import model_client_options
from . import document_rag
from . import expert_feedback
from . import tool_registry
from . import voice

SECURE = os.environ.get('WUYU_SECURE_COOKIE', '0') == '1'
ORIGINS = runtime.allowed_origins
HOSTS = runtime.hosts
PUBLIC_ASSETS = PublicAssetCache(db.ROOT / 'dist')


scheduler = None
if runtime.chat_execution_mode == 'combined':
    from .chat_execution import runner
    scheduler = Scheduler(runner)


@asynccontextmanager
async def lifespan(app):
    if runtime.chat_execution_mode == 'worker':
        raise RuntimeError('HTTP application cannot run in worker mode')
    db.start_async_work()
    try:
        await db.run_sync(initialize_runtime)
        if runtime.chat_execution_mode == 'combined':
            with db.SingleProcess():
                await scheduler.start()
                try: yield
                finally: await scheduler.stop()
        else:
            yield
    finally:
        await document_rag.stop()
        await db.close_async_work()


app = FastAPI(title='无隅 AI 平台', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(identity_router)
app.include_router(user_settings.router)
app.include_router(tool_registry.router)
app.include_router(voice.router)
app.include_router(document_rag.router)
app.include_router(expert_feedback.router)


@app.middleware('http')
async def guard(request, call_next):
    if request.headers.get('host') not in HOSTS:
        return JSONResponse({'detail': '访问地址未配置。'}, status_code=403, headers={'Cache-Control': 'no-store'})
    if request.headers.get('origin') and request.headers['origin'] not in ORIGINS:
        return JSONResponse({'detail': '不允许跨站请求。'}, status_code=403, headers={'Cache-Control': 'no-store'})
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '不允许跨站请求。'}, status_code=403, headers={'Cache-Control': 'no-store'})
        upload = request.method == 'POST' and request.url.path == '/api/files'
        try: length = int(request.headers.get('content-length', '0'))
        except ValueError: length = -1
        if upload and not 0 < length <= 20 * 1048576 + 65536:
            return JSONResponse({'detail': '单个上传文件不能超过 20 MB。'}, status_code=413, headers={'Cache-Control': 'no-store'})
        empty_delete = False
        if request.method == 'DELETE' and length == 0:
            # Inspect without buffering an unbounded body with a missing or false
            # Content-Length. A nonempty non-JSON DELETE is still rejected below.
            if not request.headers.get('content-type', '').startswith('application/json'):
                empty_delete = True
                async for chunk in request.stream():
                    if chunk:
                        empty_delete = False
                        break
                if empty_delete:
                    request._body = b''  # Preserve the proven-empty body for downstream readers.
        if not upload and (length < 0 or length > 3000000 or
                           not (empty_delete or request.headers.get('content-type', '').startswith('application/json'))):
            return JSONResponse({'detail': '请求格式或大小不符合要求。'}, status_code=415, headers={'Cache-Control': 'no-store'})
    try:await integrated_identity.enforce_platform_request_async(request)
    except integrated_identity.IdentityError as error:return JSONResponse({'detail':error.code},status_code=error.status, headers={'Cache-Control': 'no-store'})
    except HTTPException as error:return JSONResponse({'detail':error.detail},status_code=error.status_code, headers={'Cache-Control': 'no-store'})
    response = await call_next(request)
    work_settings_frame = (
        request.method in ('GET', 'HEAD') and request.url.path == '/'
        and request.query_params.getlist('embed') == ['work']
        and len(request.query_params.getlist('settings')) == 1
        and request.query_params['settings'] in ('general', 'account', 'work', 'linked', 'data', 'about', 'admin', 'quota')
        and response.headers.get('content-type', '').split(';', 1)[0].strip().lower() == 'text/html')
    frame_ancestors = "'self'" if work_settings_frame else "'none'"
    response.headers.update({'Cache-Control': PUBLIC_ASSETS.cache_control(request, response), 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'SAMEORIGIN' if work_settings_frame else 'DENY',
        'Content-Security-Policy': f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors {frame_ancestors}; base-uri 'none'; form-action 'none'"})
    if SECURE: response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.get('/api/health')
def health(): return {'app': 'wuyu-official', 'version': '2.3', 'status': 'ok', 'chat_execution_mode': runtime.chat_execution_mode}


@app.get('/api/admin/task-health')
def task_health(user=Depends(auth.admin)):
    from .task_health import snapshot
    return snapshot()


@app.get('/api/public')
def public():
    cfg = db.settings()
    return {'auth_method': 'email_password', 'registration_open': cfg['registration_open'], 'identities': auth.IDENTITIES}


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)


class RegisterInput(LoginInput):
    name: str = Field(min_length=1, max_length=60)
    organization: str = Field(min_length=1, max_length=160)
    identity: str = Field(max_length=40)


@app.post('/api/auth/register')
def register(body: RegisterInput, request: Request):
    user = auth.register(body.email, body.password.get_secret_value(), body.name, body.organization, body.identity, request.client.host)
    response = JSONResponse({'user': integrated_identity.public_user(user['id'])}); auth.issue_session(user, response, SECURE); return response


@app.post('/api/auth/login')
def login(body: LoginInput, request: Request):
    user = auth.login(body.email, body.password.get_secret_value(), request.client.host)
    response = JSONResponse({'user': integrated_identity.public_user(user['id'])}); auth.issue_session(user, response, SECURE); return response


@app.post('/api/auth/logout')
def logout(request: Request):
    for name in (auth.COOKIE,auth.LEGACY_COOKIE):
        if request.cookies.get(name):db.execute('DELETE FROM sessions WHERE hash=?',(db.digest(request.cookies[name]),))
    response = JSONResponse({'ok': True})
    for name in (auth.COOKIE,auth.LEGACY_COOKIE,auth.WORK_COOKIE):
        auth.clear_cookie(response, name)
    return response


@app.get('/api/me')
def me(user=Depends(auth.current)): return user


@app.get('/api/config')
def client_config(user=Depends(auth.current)):
    cfg = db.settings()
    models = model_profiles.public_catalog(cfg)
    return {key: cfg[key] for key in ('display_name', 'max_output_tokens', 'history_turns', 'daily_tokens', 'daily_questions')} | {
        'quota_unlimited': quota_limits.unlimited(user['id']),
        'models': models, 'default_model_id': model_profiles.default_model_id(cfg, models), 'model_ready': any(p['ready'] for p in models),
        'search_ready': bool(tool_registry.available()), 'files': attachments.config(),
        'tools': [{'id': t['id'], 'label': t['label']} for t in tool_registry.available()], 'voice': voice.public_config(),
        'document_rag': document_rag.public_config(), 'expert_feedback': expert_feedback.public_config(user)}


def own_conversation(cid, user):
    row = db.query('SELECT * FROM conversations WHERE id=? AND user_id=? AND deleted=0', (cid, user['id']), one=True)
    if not row: raise HTTPException(404, '对话不存在。')
    return row


def own_job(jid, user):
    row = db.query('SELECT * FROM jobs WHERE id=? AND user_id=?', (jid, user['id']), one=True)
    if not row: raise HTTPException(404, '任务不存在。')
    return row


@app.get('/api/conversations')
def conversations(user=Depends(auth.current)):
    return db.query("SELECT c.*, (SELECT COUNT(*) FROM jobs j WHERE j.conversation_id=c.id AND j.status IN ('running','queued')) AS active FROM conversations c WHERE c.user_id=? AND c.deleted=0 ORDER BY c.updated DESC LIMIT 200", (user['id'],))


@app.post('/api/conversations')
def create_conversation(user=Depends(auth.current)):
    now = time.time(); cid = uuid.uuid4().hex
    db.execute('INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)', (cid, user['id'], '新的对话', now, now))
    return own_conversation(cid, user)


@app.get('/api/conversations/{cid}')
def conversation(cid: str, user=Depends(auth.current)):
    row = own_conversation(cid, user)
    return {'conversation': row, 'memory': memory.state(user['id'],cid), 'turns': [public_turn(row) for row in db.query('SELECT * FROM jobs WHERE conversation_id=? ORDER BY created,id', (cid,))]}


@app.post('/api/conversations/{cid}/compact')
def compact_conversation(cid: str, user=Depends(auth.current)):
    own_conversation(cid,user)
    if db.query("SELECT id FROM jobs WHERE conversation_id=? AND status IN ('queued','running')", (cid,), one=True):
        raise HTTPException(409, '请等待本轮回答完成后整理记忆。')
    summary = memory.compact(user['id'],cid,db.settings())
    db.audit(user['id'],'compact_conversation:'+cid)
    return {'ok':True,'summary':{k:summary[k] for k in ('id','created','token_estimate','method')} if summary else None,
            'message':'记忆已重新整理，完整对话记录保留。' if summary else '还没有完成的对话，暂不需要整理。'}


@app.delete('/api/conversations/{cid}')
def delete_conversation(cid: str, user=Depends(auth.current)):
    own_conversation(cid, user)
    if db.query("SELECT id FROM jobs WHERE conversation_id=? AND status IN ('running','queued')", (cid,), one=True):
        raise HTTPException(409, '请先停止这个对话中的任务。')
    # Keep job accounting even when its UI conversation is removed (soft delete).
    db.execute('UPDATE conversations SET deleted=1 WHERE id=?', (cid,))
    return {'ok': True}


def file_owner(file_id, user):
    if not db.query('SELECT id FROM files WHERE id=? AND user_id=?', (file_id, user['id']), one=True):
        raise HTTPException(404, '附件不存在或无权访问。')


@app.post('/api/files')
async def upload(file: UploadFile = File(...), user=Depends(auth.current)):
    try:
        raw = await file.read(20 * 1048576 + 1)
        rag_enabled = (await db.run_sync(document_rag.config))['enabled']
        item = await attachments.save_file(file.filename or '附件', raw, document_rag=rag_enabled)
        registered = False
        try:
            def register_upload():
                with db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    used = conn.execute('SELECT COALESCE(SUM(size),0) FROM files WHERE user_id=?', (user['id'],)).fetchone()[0]
                    if used + item['size'] > db.settings(conn=conn)['storage_mb'] * 1048576: raise HTTPException(413, '个人附件空间已满，请联系管理员。')
                    conn.execute('INSERT INTO files VALUES(?,?,?,?)', (item['id'], user['id'], item['size'], time.time()))
            def registered_upload(_):
                nonlocal registered
                registered = True
            await db.run_sync_complete(register_upload, registered_upload)
        except BaseException:
            if not registered:
                for extension in ('.json', '.jpg', '.source'): (attachments.UPLOAD_DIR / (item['id'] + extension)).unlink(missing_ok=True)
            raise
        if rag_enabled and item['kind'] == 'document':
            await asyncio.to_thread(document_rag.ensure_index, user['id'], item['id'])
            await document_rag.schedule_index_async(user['id'], item['id'])
        return item
    except ValueError as exc: raise HTTPException(422, str(exc)) from None
    finally: await file.close()


@app.get('/api/files/{fid}/preview')
def preview(fid: str, user=Depends(auth.current)):
    file_owner(fid, user)
    item = attachments.read_file(fid)
    if item['kind'] != 'image': raise HTTPException(404)
    return FileResponse(attachments.UPLOAD_DIR / (fid + '.jpg'), media_type='image/jpeg')


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conversation_id: str = Field(max_length=64)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    message: str = Field(min_length=1, max_length=20000)
    file_ids: list[str] = Field(default_factory=list, max_length=5)
    thinking: bool = True
    network_search: bool = False
    memory_mode: Literal['auto','recent','fresh'] = 'auto'
    auto_compress: bool = True
    model_id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,47}$')


@app.post('/api/jobs')
async def submit(body: SubmitInput, user=Depends(auth.current)):
    cfg = (await db.run_sync(db.settings)); cid = body.conversation_id
    (await db.run_sync(own_conversation, cid, user))
    previous = (await db.run_sync(db.query, 'SELECT * FROM jobs WHERE id=?', (body.request_id,), one=True))
    if previous:
        if previous['user_id'] != user['id'] or previous['conversation_id'] != cid: raise HTTPException(409, '任务编号已使用。')
        return await db.run_sync(public_turn, previous)
    if not body.message.strip(): raise HTTPException(422, '请输入问题。')
    if body.model_id is None:
        body = body.model_copy(update={'model_id': model_profiles.default_model_id(cfg)})
    selected_model = model_profiles.resolve(cfg, body.model_id)
    if not selected_model: raise HTTPException(422, '所选模型已停用或不存在，请重新选择。')
    if not (await db.run_sync(db.secret, model_profiles.secret_name(body.model_id))): raise HTTPException(503, '所选模型尚未配置密钥，请联系管理员。')
    cfg = cfg | {k: selected_model[k] for k in ('display_name', 'base_url', 'model', 'context_window')}
    cfg['max_output_tokens'] = selected_model.get('max_output_tokens') or cfg['max_output_tokens']
    # The operator's global task cap is an upper bound. A smaller selected
    # model retains the same four-window allowance without reserving another
    # model's larger context. Freeze this value in the newly admitted job only.
    try:
        reservation = quota_limits.job_reservation(cfg)
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, '模型任务预算配置无效，请联系管理员。') from None
    if selected_model.get('temperature') is not None: cfg['temperature'] = selected_model['temperature']
    if selected_model.get('system_prompt'): cfg['system'] = selected_model['system_prompt']
    cfg['thinking_mode'] = model_profiles.thinking_mode(selected_model)
    cfg['tool_call_format'] = selected_model.get('tool_call_format', 'native')
    cfg['tls_public_key_pin'] = selected_model.get('tls_public_key_pin', '')
    cfg['reasoning_format'] = selected_model.get('reasoning_format', 'auto')
    if cfg['thinking_mode'] == 'none': body.thinking = False
    if body.network_search and not selected_model.get('supports_tools', True): raise HTTPException(422, '此模型未开启工具调用能力，请关闭联网工具或切换模型。')
    selected_tools = (await db.run_sync(tool_registry.available)) if body.network_search else []
    if body.network_search and not selected_tools: raise HTTPException(503, '暂无可用工具，请联系管理员。')
    if (await db.run_sync(db.query, "SELECT id FROM jobs WHERE conversation_id=? AND status IN ('queued','running')", (cid,), one=True)):
        raise HTTPException(409, '当前对话正在生成，可以新建另一段对话。')
    for fid in body.file_ids: (await db.run_sync(file_owner, fid, user))
    inherited_images = []
    # A direct follow-up can still refer to the immediately preceding images.
    # Older images are not repeatedly replayed through an ever-growing history.
    if not body.file_ids and body.memory_mode != 'fresh' and cfg['history_turns'] and selected_model.get('supports_vision', False):
        last = (await db.run_sync(db.query, "SELECT turn FROM jobs WHERE user_id=? AND conversation_id=? AND status='complete' ORDER BY created DESC,id DESC LIMIT 1", (user['id'],cid),one=True))
        if last:
            inherited_images = [f for f in json.loads(last['turn']).get('files',[]) if f.get('kind')=='image'][:5]
            for item in inherited_images: (await db.run_sync(file_owner, item['id'],user))
    rag_enabled = (await db.run_sync(document_rag.config))['enabled']
    if not selected_model.get('supports_vision', False) and any(attachments.read_file(fid)['kind'] == 'image' for fid in body.file_ids):
        raise HTTPException(422, '此模型未开启图片理解能力，请切换支持图片的模型。文档仍可解析为文本。')
    messages = [{'role': 'user', 'content': body.message.strip(), 'file_ids': body.file_ids or [f['id'] for f in inherited_images]}]
    request = ChatRequest(provider=selected_model['protocol'], messages=messages, system=cfg['system'], temperature=cfg['temperature'], max_tokens=cfg['max_output_tokens'], thinking=body.thinking, network_search=body.network_search)
    try:
        if rag_enabled:
            prepared, _ = await asyncio.to_thread(document_rag.prepare_base, request, selected_model.get('supports_vision', False))
        else:
            prepared, _ = await asyncio.to_thread(attachments.prepare_messages, request)
        history, memory_info = await asyncio.to_thread(memory.build,user['id'],cid,body.message,cfg,prepared,body.memory_mode,body.auto_compress,body.network_search)
        memory_info['inherited_images'] = [f['name'] for f in inherited_images]
    except ValueError as exc: raise HTTPException(422, str(exc)) from None
    # Tool rounds may grow the context after admission. Reserve the full window
    # for each active task, rather than under-reserving only its initial prompt.
    inflight = cfg['context_window']
    turn = {'id': body.request_id, 'user': body.message.strip(), 'answer': '', 'reasoning': '', 'toolCalls': [], 'citations': [],
            'files': [attachments.public_file(attachments.read_file(fid)) for fid in body.file_ids],
            'status': 'queued', 'created': int(time.time() * 1000), 'modelLabel': cfg['display_name'],
            'memory':memory_info,
            'request': {'model_id': body.model_id, 'thinking': body.thinking, 'network_search': body.network_search, 'max_tokens': cfg['max_output_tokens'], 'memory_mode':body.memory_mode,'auto_compress':body.auto_compress}}
    day = datetime.now(timezone(timedelta(hours=8))).date().isoformat(); now = time.time()
    def persist_submission():
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Recheck idempotency and account state after attachment parsing awaits.
            old = conn.execute('SELECT * FROM jobs WHERE id=?', (body.request_id,)).fetchone()
            if old:
                if old['user_id'] != user['id'] or old['conversation_id'] != cid: raise HTTPException(409, '任务编号已使用。')
                return ('existing', dict(old))
            account = conn.execute('SELECT active FROM users WHERE id=?', (user['id'],)).fetchone()
            if not account or not account[0]: raise HTTPException(403, '账号已停用。')
            if not conn.execute('SELECT id FROM conversations WHERE id=? AND user_id=? AND deleted=0', (cid,user['id'])).fetchone(): raise HTTPException(404, '对话不存在。')
            if conn.execute("SELECT id FROM jobs WHERE conversation_id=? AND status IN ('queued','running')", (cid,)).fetchone(): raise HTTPException(409, '当前对话正在生成，可以新建另一段对话。')
            pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND status IN ('queued','running')", (user['id'],)).fetchone()[0]
            queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if pending >= cfg['per_user_queue'] or queued >= cfg['max_queue']: raise HTTPException(429, '任务队列已满，请等待已有任务完成。')
            usage, count, held = conn.execute("SELECT COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN reserved ELSE charged END),0),COUNT(*),COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN reserved ELSE 0 END),0) FROM jobs WHERE user_id=? AND day=?", (user['id'], day)).fetchone()
            if quota_limits.daily_exceeded(user['id'], cfg, count, usage, reservation=reservation, conn=conn):
                raise HTTPException(429, quota_limits.daily_exceeded_message(cfg, count, usage, held, reservation=reservation))
            saved = {'request': request.model_dump(), 'memory_context':history, 'tools': selected_tools,
                     'rag_enabled': rag_enabled, 'rag_file_ids': document_rag.scope_files(user['id'], cid, body.file_ids, body.memory_mode, conn=conn) if rag_enabled else [],
                     'supports_vision': selected_model.get('supports_vision', False),
                     'config': {k: cfg[k] for k in ('model','base_url','context_window','display_name','thinking_mode','tool_call_format','tls_public_key_pin','reasoning_format')} | {'model_id': body.model_id}}
            conn.execute('INSERT INTO jobs(id,user_id,conversation_id,status,input,turn,created,reserved,inflight,day) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (body.request_id,user['id'],cid,'queued',json.dumps(saved,ensure_ascii=False),json.dumps(turn,ensure_ascii=False),now,reservation,inflight,day))
            conn.execute('UPDATE conversations SET title=CASE WHEN title=? THEN ? ELSE title END,updated=? WHERE id=?', ('新的对话',body.message[:40],now,cid))
            conn.execute('INSERT INTO memory_preferences(conversation_id,user_id,mode,auto_compress) VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET user_id=excluded.user_id,mode=excluded.mode,auto_compress=excluded.auto_compress', (cid,user['id'],body.memory_mode,body.auto_compress))
        return ('created', enrich_turn(turn, {'input': saved, 'user_id': user['id']}))
    outcome, result = await db.run_sync(persist_submission)
    return await db.run_sync(public_turn, result) if outcome == 'existing' else result


@app.get('/api/jobs/{jid}/events')
async def job_events(jid: str, request: Request, user=Depends(auth.current)):
    (await db.run_sync(own_job, jid, user))
    async def events():
        revision = -1
        while not await request.is_disconnected():
            try: (await db.run_sync(auth.current, request))
            except HTTPException: return
            row = (await db.run_sync(own_job, jid, user))
            if row['revision'] != revision:
                revision = row['revision']
                yield 'data: ' + json.dumps({'type': 'snapshot', 'turn': await db.run_sync(public_turn, row)}, ensure_ascii=False) + '\n\n'
            if row['status'] not in ('queued', 'running'): return
            yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'X-Accel-Buffering':'no'})


@app.post('/api/jobs/{jid}/cancel')
def cancel(jid: str, user=Depends(auth.current)):
    request_cancel(own_job(jid, user)); return {'ok': True}


@app.get('/api/conversations/{cid}/export')
def export(cid: str, format: Literal['markdown', 'json'] = 'markdown', user=Depends(auth.current)):
    chat = own_conversation(cid, user)
    if format == 'json':
        path = data_export.build_export('selected', [user['id']], True, user['id'], conversation_id=cid)
        return FileResponse(path, media_type='application/json', filename='wuyu-conversation-complete.json',
                            background=BackgroundTask(data_export.remove_export, path))
    parts = ['# ' + chat['title']]
    for row in db.query('SELECT * FROM jobs WHERE conversation_id=? ORDER BY created,id', (cid,)):
        turn = public_turn(row)
        parts.append('\n## 问题\n' + turn['user'] + '\n\n## 回答\n' + turn['answer'])
        if turn['reasoning']: parts.append('\n<details><summary>模型返回的思考内容</summary>\n\n' + turn['reasoning'] + '\n</details>')
        parts.append('\n引用来源：\n' + '\n'.join(f"- [{s['source_id']}] {s['title']} — {s['url']}" for s in turn['citations']))
    return Response('\n\n'.join(parts), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="wuyu-conversation.md"'})


@app.get('/api/admin/settings')
def admin_settings(user=Depends(auth.admin)):
    cfg = db.settings()
    cfg['additional_models'] = [p | {'key_configured': bool(db.secret(model_profiles.secret_name(p['id'])))} for p in cfg['additional_models']]
    return cfg | {name + '_configured': bool(db.secret(name)) for name in ('model','search')}


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str = Field(min_length=1,max_length=80)
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1,max_length=160)
    context_window: int = Field(ge=8192,le=model_profiles.MAX_CONFIG_INTEGER)
    max_output_tokens: int = Field(ge=512,le=model_profiles.MAX_CONFIG_INTEGER)
    temperature: float = Field(ge=0,le=1.5)
    history_turns: int = Field(ge=0,le=20)
    memory_tokens: int = Field(default=6144,ge=512,le=20000)
    memory_summary_tokens: int = Field(default=1024,ge=256,le=4096)
    memory_retrieval_count: int = Field(default=6,ge=1,le=12)
    memory_trigger_ratio: float = Field(default=.8,ge=.5,le=.95)
    system: str = Field(min_length=1,max_length=8000)
    search_enabled: bool
    max_parallel: int = Field(ge=1,le=32)
    per_user_parallel: int = Field(ge=1,le=4)
    max_queue: int = Field(ge=1,le=500)
    per_user_queue: int = Field(ge=1,le=10)
    inflight_token_budget: int = Field(ge=8192,le=model_profiles.MAX_CONFIG_INTEGER)
    job_token_budget: int = Field(ge=8192,le=model_profiles.MAX_CONFIG_INTEGER)
    daily_tokens: int = Field(ge=8192,le=model_profiles.MAX_CONFIG_INTEGER)
    daily_questions: int = Field(ge=1,le=10000)
    busy_output_tokens: int = Field(ge=512,le=model_profiles.MAX_CONFIG_INTEGER)
    job_timeout: int = Field(ge=30,le=model_profiles.MAX_CONFIG_INTEGER)
    registration_open: bool
    storage_mb: int = Field(ge=20,le=10000)
    model_key: str = Field(default='',max_length=4096)
    search_key: str = Field(default='',max_length=4096)
    additional_models: list[model_profiles.ModelProfile] = Field(default_factory=list, max_length=32)
    default_model_id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,47}$')
    supports_vision: bool = True
    tool_call_format: Literal['native', 'qwen3_coder'] = 'native'
    supports_tools: bool = True
    thinking_mode: Literal['chat_template','enable_thinking','both','none'] = 'chat_template'

    @model_validator(mode='after')
    def validate_all(self):
        url = urlsplit(self.base_url)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment: raise ValueError('模型 Base URL 无效。')
        self.base_url = self.base_url.rstrip('/')
        if self.per_user_parallel > self.max_parallel or self.per_user_parallel > self.per_user_queue: raise ValueError('个人并发不能超过总并发或个人任务上限。')
        if self.inflight_token_budget < self.context_window: raise ValueError('总在途 token 容量至少应容纳一个完整上下文。')
        if self.max_output_tokens >= self.context_window - 1024: raise ValueError('请为输入上下文预留空间。')
        if self.memory_summary_tokens > self.memory_tokens: raise ValueError('摘要预算不能大于历史记忆预算。')
        if self.busy_output_tokens > self.max_output_tokens: raise ValueError('繁忙输出上限不能超过常规上限。')
        if self.job_token_budget < self.context_window or self.daily_tokens < self.job_token_budget: raise ValueError('每任务预算需容纳一个上下文，日额度需容纳一个任务。')
        ids = [p.id for p in self.additional_models]
        if len(ids) != len(set(ids)): raise ValueError('模型标识不能重复。')
        if self.default_model_id is not None and self.default_model_id not in {'team', *(p.id for p in self.additional_models if p.enabled)}:
            raise ValueError('默认问答模型必须是当前已启用的模型。')
        for p in self.additional_models:
            if not p.enabled: continue
            if (p.max_output_tokens or self.max_output_tokens) >= p.context_window - 1024: raise ValueError(f'{p.display_name} 的上下文容量不足，请为输入预留空间。')
            if min(self.inflight_token_budget, self.job_token_budget) < p.context_window: raise ValueError(f'在途容量与每任务预算需容纳 {p.display_name} 的上下文。')
        return self


@app.put('/api/admin/settings')
def save_settings(body: SettingsInput, user=Depends(auth.admin)):
    active = db.query("SELECT MAX(inflight) AS capacity FROM jobs WHERE status IN ('queued','running')", one=True)
    if active['capacity'] and body.inflight_token_budget < active['capacity']: raise HTTPException(409, '请先等待当前任务结束，再降低在途容量。')
    data = body.model_dump()
    data['default_model_id'] = body.default_model_id if body.default_model_id is not None else db.settings().get('default_model_id', 'team')
    if data['default_model_id'] not in {'team', *(p.id for p in body.additional_models if p.enabled)}:
        raise HTTPException(422, '默认问答模型已停用或移除，请先选择新的默认模型。')
    data['additional_models'] = [p.model_dump(exclude={'api_key'}) for p in body.additional_models]
    for p in body.additional_models: db.save_secret(model_profiles.secret_name(p.id), p.api_key.get_secret_value())
    for field, name in (('model_key','model'),('search_key','search')): db.save_secret(name, data.pop(field))
    db.save_settings(data,user['id']); return {'ok': True}


@app.post('/api/admin/models/{model_id}/test')
async def test_model(model_id: str, user=Depends(auth.admin)):
    profile = model_profiles.resolve((await db.run_sync(db.settings)), model_id)
    if not profile: raise HTTPException(404, '请先保存并启用模型。')
    key = (await db.run_sync(db.secret, model_profiles.secret_name(model_id)))
    if not key: raise HTTPException(409, '请先配置模型密钥。')
    payload = {'model': profile['model'], 'messages': [{'role': 'user', 'content': '只回复：连接成功'}],
               'max_tokens': 128, 'temperature': 0, 'stream': False,
               **model_profiles.thinking_parameters(model_profiles.thinking_mode(profile), False)}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=35, trust_env=False, follow_redirects=False, **model_client_options(profile)) as client:
            response = await client.post(profile['base_url'] + '/chat/completions', headers={'Authorization': 'Bearer '+key}, json=payload)
        if response.status_code != 200: raise HTTPException(502, '接口返回 HTTP %s，请核对地址、模型名称和密钥。' % response.status_code)
        content = response.json()['choices'][0]['message'].get('content')
        if not isinstance(content, str) or not content.strip(): raise HTTPException(502, '连接已建立，但接口未返回文本回答。')
        (await db.run_sync(db.audit, user['id'], 'test_model:' + model_id))
        return {'ok': True, 'seconds': round(time.monotonic()-started, 2), 'message': '文本调用成功；图片和工具能力需使用对应任务验证。'}
    except HTTPException: raise
    except Exception: raise HTTPException(502, '接口连接失败、超时或返回格式不兼容。') from None


@app.get('/api/admin/overview')
def overview(user=Depends(auth.admin)):
    return {'users':db.query('SELECT * FROM users ORDER BY created DESC'),
            'states':db.query('SELECT status,COUNT(*) AS count,SUM(charged) AS tokens FROM jobs GROUP BY status'),
            'recent':db.query('SELECT j.id,j.user_id,j.status,j.created,j.started,j.finished,j.reserved,j.charged,u.name FROM jobs j JOIN users u ON u.id=j.user_id ORDER BY j.created DESC LIMIT 50')}


class ExportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['all','selected']
    user_ids: list[str] = Field(default_factory=list,max_length=500)
    include_attachments: bool = True

    @model_validator(mode='after')
    def validate_selection(self):
        if self.scope == 'selected' and not self.user_ids: raise ValueError('请至少选择一个用户。')
        if self.scope == 'all' and self.user_ids: raise ValueError('导出全部用户时无需指定用户。')
        return self


@app.post('/api/admin/export')
def export_users(body: ExportInput, user=Depends(auth.admin)):
    try: path = data_export.build_export(body.scope,body.user_ids,body.include_attachments,user['id'])
    except ValueError as exc: raise HTTPException(422,str(exc)) from None
    filename = 'wuyu-users-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json'
    return FileResponse(path,media_type='application/json',filename=filename,background=BackgroundTask(data_export.remove_export,path))


@app.post('/api/admin/exports')
def prepare_user_export(body: ExportInput, user=Depends(auth.admin)):
    try: path = data_export.build_export(body.scope,body.user_ids,body.include_attachments,user['id'])
    except ValueError as exc: raise HTTPException(422,str(exc)) from None
    export_id = uuid.uuid4().hex
    target = path.with_name('wuyu-export-'+user['id']+'-'+export_id+'.json')
    path.rename(target)
    return {'download_url':'/api/admin/exports/'+export_id,'size_bytes':target.stat().st_size,'expires_in':3600}


@app.get('/api/admin/exports/{export_id}')
def download_user_export(export_id: str, user=Depends(auth.admin)):
    if len(export_id)!=32 or any(c not in '0123456789abcdef' for c in export_id): raise HTTPException(404,'导出文件不存在。')
    path = db.DATA / 'exports' / ('wuyu-export-'+user['id']+'-'+export_id+'.json')
    if not path.is_file() or path.stat().st_mtime < time.time()-3600: raise HTTPException(404,'导出文件已过期，请重新导出。')
    filename='wuyu-users-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json'
    return FileResponse(path,media_type='application/json',filename=filename,background=BackgroundTask(data_export.remove_export,path))


class UserInput(BaseModel): active: bool


@app.patch('/api/admin/users/{uid}')
def change_user(uid: str, body: UserInput, user=Depends(auth.admin)):
    target = db.query('SELECT * FROM users WHERE id=?', (uid,), one=True)
    if not target: raise HTTPException(404)
    if target['role'] == 'admin': raise HTTPException(409, '此入口不能停用系统管理员。')
    db.execute('UPDATE users SET active=? WHERE id=?', (body.active,uid))
    if not body.active:
        db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        for row in db.query("SELECT * FROM jobs WHERE user_id=? AND status IN ('queued','running')", (uid,)): request_cancel(row)
    db.audit(user['id'],'user_active_change'); return {'ok':True}


@app.get('/')
def index(): return FileResponse(db.ROOT / 'dist/index.html')


@app.get('/favicon.svg')
def favicon():
    original = db.ROOT / 'web/favicon.svg'
    return FileResponse(original, media_type='image/svg+xml') if original.is_file() else FileResponse(db.ROOT / 'web/logo.png', media_type='image/png')


@app.get('/logo.png')
def logo(): return FileResponse(db.ROOT / 'web/logo.png', media_type='image/png')


app.mount('/assets',StaticFiles(directory=db.ROOT / 'dist/assets',check_dir=False),name='assets')
