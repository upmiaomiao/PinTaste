"""Same-origin portal transport, bounded uploads and idempotent submission delivery."""
from __future__ import annotations
import asyncio
import secrets
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect
from .auth_portal import COOKIE
from .request_replay import protected_submission


def install_security(app, allowed_origins):
    @app.middleware("http")
    async def security(request, call_next):
        actual_origin = f'{request.url.scheme}://{request.url.netloc}'
        if actual_origin not in allowed_origins:
            return JSONResponse({"detail":"门户来源不匹配"},status_code=403)
        # Uvicorn includes root_path in scope.path; route matching removes it.
        # Apply the same boundary here so subpath APIs validate central sessions
        # and use the upload limit rather than the small JSON request limit.
        route_path = request.scope.get('path', '')
        mount_path = request.scope.get('root_path', '').rstrip('/')
        if mount_path and (route_path == mount_path or route_path.startswith(mount_path + '/')):
            route_path = route_path[len(mount_path):] or '/'
        if app.state.portal.identity.enabled and route_path.startswith('/portal/api/'):
            try:
                request.state.identity_session = await app.state.portal.identity.session(request, app.state.portal.db)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, status_code=error.status_code)
        state = app.state.portal
        replay_key = request.headers.get('idempotency-key') if request.method == 'POST' and protected_submission(route_path) else None
        is_upload = request.method == 'POST' and route_path == '/portal/api/uploads'
        user_id = None
        if replay_key is not None or is_upload:
            session = (getattr(request.state, 'identity_session', None) if state.identity.enabled
                       else state.db.session(request.cookies.get(COOKIE, '')))
            if not session:
                return JSONResponse({'detail': '请先登录'}, status_code=401)
            if not secrets.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf_token']):
                return JSONResponse({'detail': '会话校验失败，请刷新后重试'}, status_code=403)
            user_id = session['user']['id']
        if is_upload and state.incoming_uploads >= 2:
            return JSONResponse({'detail': '资料上传正在处理中，请稍后重试'}, status_code=429, headers={'Retry-After': '5'})
        if is_upload:
            state.incoming_uploads += 1
        try:
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                if request.headers.get('origin') not in allowed_origins:
                    return JSONResponse({'detail':'写操作需要同源Origin'}, status_code=403)
                if request.headers.get('sec-fetch-site') in ('cross-site', 'same-site'):
                    return JSONResponse({'detail':'不接受跨站写操作'}, status_code=403)
                limit = 70 * 1024 * 1024 if is_upload else 1024 * 1024
                try:
                    length = int(request.headers.get('content-length', '0'))
                    if length < 0:
                        raise ValueError()
                    if length > limit:
                        return JSONResponse({'detail':'请求体超过上限'}, status_code=413)
                except ValueError:
                    return JSONResponse({'detail':'请求长度无效'}, status_code=400)
                content = bytearray()
                try:
                    async with asyncio.timeout(60 if is_upload else 20):
                        async for chunk in request.stream():
                            if len(content) + len(chunk) > limit:
                                return JSONResponse({'detail':'请求体超过上限'}, status_code=413)
                            content.extend(chunk)
                except TimeoutError:
                    return JSONResponse({'detail':'上传超时，请检查网络后重试'}, status_code=408)
                except ClientDisconnect:
                    return JSONResponse({'detail':'请求连接已中断'}, status_code=400)
                request._body = bytes(content)
                del content
            if replay_key is not None:
                try:
                    cached = state.replays.begin(user_id, replay_key, route_path, await request.body())
                except HTTPException as error:
                    return JSONResponse({'detail':error.detail}, status_code=error.status_code, headers=error.headers)
                if cached:
                    return JSONResponse(cached[1], status_code=cached[0], headers={'Idempotency-Replayed': 'true', 'Cache-Control': 'no-store'})
            response = await call_next(request)
            if replay_key is not None:
                # Submission responses are bounded JSON; no images or stream are accepted here.
                raw = b''.join([part async for part in response.body_iterator])
                import json
                state.replays.finish(user_id, replay_key, response.status_code, json.loads(raw))
                response = Response(raw, status_code=response.status_code, headers=dict(response.headers), background=response.background)
        finally:
            if is_upload:
                state.incoming_uploads -= 1
        response.headers.update({"X-Content-Type-Options":"nosniff","Referrer-Policy":"same-origin",
                                 "X-Frame-Options":"DENY"})
        # 用户数据一律不缓存；公开的静态文件（脚本、样式、logo）单独设置缓存，见 static_cache_control。
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if response.headers.get("content-type","").startswith("text/html"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://ai.wuyullm.com; frame-ancestors 'none'; base-uri 'none'"
        return response

