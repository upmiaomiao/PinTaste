"""Authenticated streaming speech input. PCM stays in memory, keys stay in vault."""
import asyncio
import json
import time
import uuid
from pathlib import Path
from contextlib import AsyncExitStack, suppress

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from typing import Literal
from . import auth
from . import store as db
from . import xfyun_asr
from . import omni_asr

router = APIRouter()
DEFAULT = {'enabled': False, 'provider': 'xfyun', 'max_seconds': 120, 'concurrency': 2, 'daily_sessions': 60}
active = set()


def init():
    db.execute('CREATE TABLE IF NOT EXISTS voice_calls(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,created REAL NOT NULL,seconds REAL DEFAULT 0,status TEXT NOT NULL)')
    with db.connect() as conn:
        columns = db.table_columns(conn, 'voice_calls')
        for name, declaration in {'purpose': "TEXT NOT NULL DEFAULT 'question'", 'target_job_id': 'TEXT',
                                  'field': 'TEXT', 'transcript': 'TEXT', 'model_label': 'TEXT',
                                  'credit_user_id': 'TEXT', 'credit_reservation_id': 'TEXT', 'credit_status': 'TEXT'}.items():
            if name not in columns: conn.execute('ALTER TABLE voice_calls ADD COLUMN ' + name + ' ' + declaration)


def check_purpose(browser):
    purpose = browser.query_params.get('purpose', 'question')
    if purpose == 'question': return {'purpose': purpose, 'job_id': None, 'field': None}
    if purpose == 'work': return {'purpose': purpose, 'job_id': None, 'field': None}
    if purpose != 'expert_feedback': raise ValueError('语音用途无效。')
    from . import expert_feedback
    jid = browser.query_params.get('job_id', '')
    field = browser.query_params.get('field', '')
    if field not in ('comment', 'suggestion', 'corrected_answer'): raise ValueError('评审输入位置无效。')
    try: expert_feedback.review_job(jid, auth.current(browser), submit=True, voice=True)
    except HTTPException as exc: raise ValueError(exc.detail) from exc
    return {'purpose': purpose, 'job_id': jid, 'field': field}


def config(*, conn=None):
    sql = "SELECT value FROM settings WHERE key='voice_config'"
    row = db.query(sql, one=True) if conn is None else conn.execute(sql).fetchone()
    return DEFAULT | (json.loads(row['value']) if row else {})


def public_config(*, conn=None):
    cfg = config() if conn is None else config(conn=conn)
    def secret(name):
        return db.secret(name) if conn is None else db.secret(name, conn=conn)
    omni = cfg['provider'] == 'qwen_omni'
    ready = bool(secret('voice_omni')) if omni else all(secret('voice_' + key) for key in ('app_id', 'api_key', 'api_secret'))
    return {'ready': cfg['enabled'] and ready, 'provider': cfg['provider'],
            'model_label': 'Qwen3.8 Omni' if omni else '讯飞实时语音',
            'max_seconds': cfg['max_seconds'], 'streaming': not omni,
            'recognition_timeout': omni_asr.TIMEOUT_SECONDS if omni else 30}


@router.get('/api/voice/work/config')
def work_voice_config(user=Depends(auth.current)):
    from . import integrated_identity
    try:
        integrated_identity.require_feature(user, 'work')
    except integrated_identity.IdentityError as error:
        raise HTTPException(error.status, error.code) from None
    return public_config()


@router.get('/api/voice/recorder-worklet.js')
def work_voice_worklet(user=Depends(auth.current)):
    from . import integrated_identity
    try:
        integrated_identity.require_feature(user, 'work')
    except integrated_identity.IdentityError as error:
        raise HTTPException(error.status, error.code) from None
    return FileResponse(Path(__file__).parent / 'web' / 'recorder-worklet.js',
                        media_type='application/javascript', headers={'Cache-Control': 'private, max-age=3600'})


class VoiceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    provider: Literal['xfyun', 'qwen_omni'] = 'xfyun'
    max_seconds: int = Field(default=120, ge=10, le=120)
    concurrency: int = Field(default=2, ge=1, le=8)
    daily_sessions: int = Field(default=60, ge=1, le=1000)
    app_id: SecretStr = Field(default_factory=lambda: SecretStr(''), max_length=256)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(''), max_length=256)
    api_secret: SecretStr = Field(default_factory=lambda: SecretStr(''), max_length=256)
    omni_api_key: SecretStr = Field(default_factory=lambda: SecretStr(''), max_length=4096)


@router.get('/api/admin/voice')
def admin_config(user=Depends(auth.admin)):
    return config() | {'configured': {k: bool(db.secret('voice_'+k)) for k in ('app_id','api_key','api_secret','omni')},
                       'usage': db.query('SELECT COUNT(*) AS calls,COALESCE(SUM(seconds),0) AS seconds FROM voice_calls', one=True)}


@router.put('/api/admin/voice')
def save_config(body: VoiceInput, user=Depends(auth.admin)):
    for key in ('app_id','api_key','api_secret'):
        value = getattr(body, key).get_secret_value().strip()
        if value and (len(value) < 4 or any(ord(c) < 33 or ord(c) > 126 for c in value)): raise HTTPException(422, '请填写原始讯飞凭据，不含空格或换行。')
    omni_key = body.omni_api_key.get_secret_value().strip()
    if omni_key and (len(omni_key) < 4 or any(ord(c) < 33 or ord(c) > 126 for c in omni_key)):
        raise HTTPException(422, '请填写原始 DMXAPI 密钥，不含空格或换行。')
    if body.enabled:
        if body.provider == 'qwen_omni':
            if not (omni_key or db.secret('voice_omni')): raise HTTPException(422, '启用前请配置 DMXAPI 密钥。')
        elif not all(getattr(body, k).get_secret_value().strip() or db.secret('voice_'+k) for k in ('app_id','api_key','api_secret')):
            raise HTTPException(422, '启用前请完整配置 App ID、API Key 和 API Secret。')
    for key in ('app_id','api_key','api_secret'): db.save_secret('voice_'+key, getattr(body,key).get_secret_value().strip())
    db.save_secret('voice_omni', omni_key)
    db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('voice_config', json.dumps(body.model_dump(exclude={'app_id','api_key','api_secret','omni_api_key'}))))
    db.audit(user['id'], 'update_voice')
    return {'ok': True}


async def omni_recognize(browser, cfg, stats):
    """Reuse authenticated PCM recording; transcribe only after the user ends it."""
    await browser.send_json({'type': 'ready', 'max_seconds': cfg['max_seconds']})
    pcm = bytearray(); checked = 0
    while True:
        packet = await asyncio.wait_for(browser.receive(), 15)
        if packet['type'] == 'websocket.disconnect': raise WebSocketDisconnect()
        if time.monotonic() - checked > 2:
            (await db.run_sync(auth.current, browser))
            (await db.run_sync(check_purpose, browser))
            if not (await db.run_sync(config))['enabled'] or (await db.run_sync(config))['provider'] != cfg['provider']:
                raise ValueError('语音配置已更新，请重新录制。')
            checked = time.monotonic()
        if packet.get('bytes') is not None:
            audio = packet['bytes']
            if not audio or len(audio) > 1280 or len(audio) % 2: raise ValueError('录音数据格式无效。')
            if len(pcm) + len(audio) > cfg['max_seconds'] * 32000: raise ValueError('录音超过时长上限。')
            pcm.extend(audio); stats['seconds'] = round(len(pcm) / 32000, 3)
        elif packet.get('text'):
            if len(packet['text']) > 100 or json.loads(packet['text']) != {'type': 'end'} or not pcm:
                raise ValueError('请录音后结束，或取消本次录音。')
            break

    async def partial(text):
        await browser.send_json({'type': 'partial', 'text': text})

    async def watch_configuration():
        while True:
            (await db.run_sync(auth.current, browser))
            (await db.run_sync(check_purpose, browser))
            latest = (await db.run_sync(config))
            if not latest['enabled'] or latest['provider'] != cfg['provider']:
                raise ValueError('语音配置已更新，请重新录制。')
            await asyncio.sleep(2)

    transcription = asyncio.create_task(omni_asr.transcribe_pcm(bytes(pcm), (await db.run_sync(db.secret, 'voice_omni')), partial))
    pcm.clear()
    disconnect = asyncio.create_task(browser.receive())
    guard = asyncio.create_task(watch_configuration())
    tasks = [transcription, disconnect, guard]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            if disconnect.result()['type'] == 'websocket.disconnect': raise WebSocketDisconnect()
            raise ValueError('录音已结束，请等待识别结果。')
        if guard in done: await guard
        text = await transcription
        (await db.run_sync(auth.current, browser))
        (await db.run_sync(check_purpose, browser))
        return text
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)



def reserve_work_voice(user_id, call_id):
    from . import integrated_identity, identity_bridge
    work_id = integrated_identity.quota_target(user_id)
    result = identity_bridge.request('POST', '/api/auth/internal/identity/credits',
        {'action': 'reserve', 'work_user_id': work_id, 'operation_key': call_id}, timeout=10)['data']
    try:
        db.execute('UPDATE voice_calls SET credit_user_id=?,credit_reservation_id=?,credit_status=? WHERE id=?',
            (work_id, result['id'], 'reserved', call_id))
    except Exception:
        identity_bridge.request('POST', '/api/auth/internal/identity/credits', {'action': 'refund', 'work_user_id': work_id, 'reservation_id': result['id']}, timeout=10)
        raise
    return work_id, result['id']


def finish_work_voice(call_id, credit, accepted):
    from . import identity_bridge
    identity_bridge.request('POST', '/api/auth/internal/identity/credits',
        {'action': 'settle' if accepted else 'refund', 'work_user_id': credit[0], 'reservation_id': credit[1]}, timeout=10)
    db.execute('UPDATE voice_calls SET credit_status=? WHERE id=?', ('settled' if accepted else 'refunded', call_id))


def recover_voice_receipts(user_id):
    # Only terminal records are recoverable. Running/uncertain calls keep their
    # hold for investigation rather than receiving a speculative refund.
    rows = db.query("SELECT * FROM voice_calls WHERE user_id=? AND credit_status='reserved' AND status!='running' LIMIT 20", (user_id,))
    for row in rows:
        finish_work_voice(row['id'], (row['credit_user_id'], row['credit_reservation_id']), row['status'] == 'complete')


@router.websocket('/api/voice/stream')
async def stream(browser: WebSocket):
    # HTTP middleware doesn't cover WebSockets. Repeat origin/session enforcement.
    from .app_main import ORIGINS, HOSTS
    try: user = (await db.run_sync(auth.current, browser))
    except HTTPException:
        await browser.close(code=4401); return
    if browser.headers.get('origin') not in ORIGINS or browser.headers.get('host') not in HOSTS:
        await browser.close(code=4403); return
    await browser.accept()
    cfg = (await db.run_sync(config)); uid = user['id']; claimed = False; call_id = None; credit = None; accepted = False
    stats = {'seconds': 0, 'status': 'interrupted', 'transcript': None}
    try:
        if not (await db.run_sync(public_config))['ready']: raise ValueError('语音服务尚未启用，请联系管理员。')
        purpose = (await db.run_sync(check_purpose, browser))
        if uid in active or len(active) >= cfg['concurrency']: raise ValueError('语音服务正在使用中，请稍后再试。')
        active.add(uid); claimed = True
        call_id = uuid.uuid4().hex
        # Sliding 24-hour limit includes failed attempts, preventing handshake abuse.
        def admit_voice():
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                count = conn.execute('SELECT COUNT(*) FROM voice_calls WHERE user_id=? AND created>?', (uid,time.time()-86400)).fetchone()[0]
                if count >= cfg['daily_sessions']: raise ValueError('已达到每日语音次数上限，请稍后再试。')
                conn.execute('INSERT INTO voice_calls(id,user_id,created,status,purpose,target_job_id,field,model_label) VALUES(?,?,?,?,?,?,?,?)',
                    (call_id, uid, time.time(), 'running', purpose['purpose'], purpose['job_id'], purpose['field'], public_config(conn=conn).get('model_label')))
        await db.run_sync(admit_voice)
        if purpose['purpose'] == 'work':
            await asyncio.to_thread(recover_voice_receipts, uid)
            credit = await asyncio.to_thread(reserve_work_voice, uid, call_id)
        async def accept_transcript(transcript):
            nonlocal accepted
            stats['transcript'] = transcript; stats['status'] = 'complete'
            def mark_accepted(_):
                nonlocal accepted
                accepted = True
            await db.run_sync_complete(db.execute, mark_accepted,
                'UPDATE voice_calls SET seconds=?,status=?,transcript=? WHERE id=?',
                (stats['seconds'], 'complete', transcript, call_id))
            if credit:
                await asyncio.to_thread(finish_work_voice, call_id, credit, True)
        if cfg['provider'] == 'qwen_omni':
            async with asyncio.timeout(cfg['max_seconds'] + omni_asr.TIMEOUT_SECONDS + 15):
                transcript = await omni_recognize(browser, cfg, stats)
                await accept_transcript(transcript)
                await browser.send_json({'type': 'final', 'text': transcript, 'seconds': stats['seconds'], 'call_id': call_id})
                stats['status'] = 'complete'
            return
        credentials = {k: (await db.run_sync(db.secret, 'voice_'+k)) for k in ('app_id','api_key','api_secret')}
        async with asyncio.timeout(cfg['max_seconds']+45), AsyncExitStack() as stack:
            tasks = []
            try:
                waiting = asyncio.create_task(browser.receive())
                opening = asyncio.create_task(stack.enter_async_context(xfyun_asr.open_session(credentials)))
                tasks = [waiting, opening]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if waiting in done:
                    if waiting.result()['type'] == 'websocket.disconnect': raise WebSocketDisconnect()
                    raise ValueError('请等待语音连接就绪后再发送录音。')
                upstream, session_id = await opening
                waiting.cancel(); await asyncio.gather(waiting, return_exceptions=True)
                await browser.send_json({'type': 'ready', 'max_seconds': cfg['max_seconds']})
                ended = asyncio.Event(); segments = xfyun_asr.Segments()

                async def upload():
                    pacer = xfyun_asr.PCMSender(upstream); sent = 0; checked = 0
                    while True:
                        packet = await asyncio.wait_for(browser.receive(), 25 if ended.is_set() else 15)
                        if packet['type'] == 'websocket.disconnect': raise WebSocketDisconnect()
                        if time.monotonic() - checked > 2:
                            (await db.run_sync(auth.current, browser))
                            (await db.run_sync(check_purpose, browser))
                            if not (await db.run_sync(config))['enabled']: raise ValueError('管理员已关闭语音服务。')
                            checked = time.monotonic()
                        if ended.is_set(): raise ValueError('录音已结束，请等待识别结果。')
                        if packet.get('bytes') is not None:
                            audio = packet['bytes']
                            if not audio or len(audio) > 1280 or len(audio)%2: raise ValueError('录音数据格式无效。')
                            if sent+len(audio) > cfg['max_seconds']*32000: raise ValueError('录音超过时长上限。')
                            await pacer.send(audio); sent += len(audio); stats['seconds'] = round(sent/32000, 3)
                        elif packet.get('text'):
                            if len(packet['text']) > 100 or json.loads(packet['text']) != {'type':'end'} or not sent: raise ValueError('请录音后结束，或取消本次录音。')
                            await pacer.drain_time(); ended.set()
                            await upstream.send(json.dumps({'end': True, 'sessionId': session_id}))

                async def receive():
                    received = 0
                    while True:
                        raw = await upstream.recv(); received += len(raw)
                        if received > 2*1024*1024: raise ValueError('语音服务返回数据过大。')
                        if not segments.update(raw): continue
                        value = segments.text() if segments.final else segments.preview()
                        for secret in credentials.values(): value = value.replace(secret, '[凭据已隐藏]')
                        if segments.final:
                            if not ended.is_set(): raise ValueError('转写提前结束，请重新录制。')
                            return value[:20000]
                        await browser.send_json({'type':'partial', 'text':value[:20000]})

                sender, receiver = asyncio.create_task(upload()), asyncio.create_task(receive())
                tasks = [sender, receiver]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if sender in done: await sender
                transcript = await receiver
                (await db.run_sync(auth.current, browser))
                (await db.run_sync(check_purpose, browser))
                await accept_transcript(transcript)
                await browser.send_json({'type':'final','text':transcript,'seconds':stats['seconds'],'call_id':call_id})
                stats['status'] = 'complete'
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, asyncio.CancelledError): pass
    except Exception as exc:
        if not accepted: stats['status'] = 'error'
        message = str(exc) if type(exc) is ValueError or isinstance(exc, (xfyun_asr.ASRError, omni_asr.ASRError)) else '语音连接中断或超时，请重试。'
        from .integrated_identity import IdentityError
        if isinstance(exc, IdentityError):
            if exc.status == 429: message = '积分不足或达到使用上限，请查看积分余额后重试。'
            elif exc.status == 403: message = '当前账号暂不能使用语音，请联系管理员核对权限与积分状态。'
        with suppress(Exception): await browser.send_json({'type':'error','detail':message})
    finally:
        if credit and not accepted:
            with suppress(Exception): await asyncio.to_thread(finish_work_voice, call_id, credit, False)
        if call_id: (await db.run_sync(db.execute, 'UPDATE voice_calls SET seconds=?,status=?,transcript=? WHERE id=?',
                              (stats['seconds'],stats['status'],stats['transcript'],call_id)))
        if claimed: active.discard(uid)
        with suppress(Exception): await browser.close()
