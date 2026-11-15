"""Administrative configuration and catalogue routes, with injected auth guards."""
import asyncio
import json
import httpx
from fastapi import HTTPException, Request
from .data import DataClient, DataClientError
from .store import now


def install_admin_routes(app, *, current, runtime, object_body):
    @app.get("/portal/api/users")
    async def users(request: Request):
        current(request,True)
        return {"items":runtime(request).db.users()}

    @app.post("/portal/api/users",status_code=201)
    async def create_user(request: Request):
        current(request,True)
        if runtime(request).identity.enabled:
            raise HTTPException(403, '账号注册由无隅 AI 平台统一管理')
        body = await object_body(request)
        return runtime(request).db.create_user(body.get("username",""),body.get("password",""),body.get("display_name",""),body.get("role","user"))

    @app.patch("/portal/api/users/{user_id}")
    async def patch_user(user_id: str, request: Request):
        current(request,True)
        body = await object_body(request)
        if runtime(request).identity.enabled and set(body) - {'active','role'}:
            raise HTTPException(422, '见微仅管理本系统启用状态和角色；账号资料请在无隅 AI 修改')
        return runtime(request).db.update_user(user_id,body)

    @app.get('/portal/api/users/{user_id}/settings')
    async def user_settings(user_id: str, request: Request):
        current(request, True)
        rt = runtime(request)
        if not any(row['id'] == user_id for row in rt.db.users()):
            raise HTTPException(404, '用户不存在')
        return rt.limits.settings(user_id, rt.config.read()['appearance']['default_font_size'])

    @app.patch('/portal/api/users/{user_id}/settings')
    async def update_user_settings(user_id: str, request: Request):
        current(request, True)
        rt = runtime(request)
        return rt.limits.update(user_id, await object_body(request), rt.config.read()['appearance']['default_font_size'])

    @app.get("/portal/api/config")
    async def config(request: Request):
        current(request,True)
        return runtime(request).config.public()

    @app.get("/portal/api/admin/overview")
    async def admin_overview(request: Request):
        current(request,True)
        return runtime(request).admin_overview()

    @app.get("/portal/api/admin/data-health")
    async def admin_data_health(request: Request):
        current(request,True)
        data=runtime(request).config.read()["data"]
        if not data["base_url"]:
            return {"status":"not_configured","checked_at":now(),"message":"尚未配置数据服务地址"}
        async def probe():
            headers={"Authorization":"Bearer "+data["api_key"]} if data.get("api_key") else {}
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=2) as session:
                async with session.stream("GET",data["base_url"].rstrip("/")+"/health",headers=headers) as response:
                    if response.status_code!=200:
                        return {"status":"authentication_required" if response.status_code in (401,403) else "unavailable",
                                "http_status":response.status_code,"message":"数据服务未通过本次连接检查"}
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>32768:
                            raise ValueError("bounded health response")
                    health=json.loads(body)
                    if not isinstance(health,dict) or health.get("status")!="ok":
                        return {"status":"unavailable","message":"数据服务未返回正常健康状态"}
                    return {"status":"connected","message":"本次数据服务连接正常；各图源需在实际任务中验证"}
        try:
            result=await asyncio.wait_for(probe(),timeout=3)
        except (httpx.HTTPError,TimeoutError,ValueError):
            result={"status":"unavailable","message":"本次无法连接数据服务，请检查本机服务、地址与凭据"}
        from .unified_imagery import UnifiedImageryClient
        unified_config = runtime(request).config.read()['unified_data']
        unified = {'status':'not_configured','message':'新影像接口尚未启用'}
        if unified_config['enabled'] and unified_config.get('api_key'):
            client = UnifiedImageryClient(unified_config)
            try:
                catalog = await asyncio.wait_for(client.catalog(), timeout=10)
                unified = {'status':'connected','message':'新影像接口鉴权与目录已连接；各日期影像需逐次取图验证',
                    'source_count':len(catalog.get('sources',[]))}
            except (DataClientError,ValueError,TimeoutError):
                unified = {'status':'unavailable','message':'新影像接口本次未连接，请检查入口和API密钥'}
            finally:
                await client.aclose()
        return {**result,"checked_at":now(),'services':{'science':result,'unified':unified}}

    @app.get("/portal/api/admin/metrics")
    async def admin_metrics(request: Request):
        current(request,True)
        return runtime(request).metrics()

    @app.post("/portal/api/admin/drain")
    async def admin_drain(request: Request):
        """发布/维护前先排空：不再接收新任务，已有任务照常完成。"""
        current(request,True)
        body = await object_body(request)
        if type(body.get('draining')) is not bool:
            raise HTTPException(422,"draining必须为布尔值")
        state = runtime(request)
        state.draining = body['draining']
        return {"draining":state.draining,"active_tasks":state.metrics()["active_tasks"]}

    @app.put("/portal/api/config")
    async def put_config(request: Request):
        current(request,True)
        state = runtime(request)
        patch = await object_body(request)
        if patch and set(patch) <= {'appearance'}:
            async with state.configuration_lock:
                return state.config.update(patch)
        live_keys = {'concurrency','qa_concurrency','max_pending_tasks','max_job_groups','max_user_interactive','max_user_batch',
                     'optical_review_timeout','cross_review_timeout','recognition_concurrency'}
        if patch and set(patch) == {'task_policy'} and isinstance(patch['task_policy'],dict) and set(patch['task_policy']) <= live_keys:
            # 容量与超时可以在有任务运行时实时调整：只改通道大小和限额，不重启引擎、不中断任务。
            async with state.configuration_lock:
                result = state.config.update(patch)
                state.apply_capacity()
                return result
        if 'sources' in patch:
            from .source_choices import validate_source_policy
            try:
                data = state.config.read()['data']
                async with DataClient(data['base_url'], data['api_key'], data['mode'], unified_config=state.config.read()['unified_data']) as raw_client:
                    catalog = await raw_client.catalog(force_refresh=True)
                validate_source_policy(patch['sources'], catalog)
            except ValueError:
                raise HTTPException(422, '请从当前图源目录中选择来源') from None
        async with state.configuration_lock:
            if state.jobs or state.service.pipeline.pending:
                raise HTTPException(409,"当前有任务执行，请完成后更新配置")
            previous = state.config.read()
            result = state.config.update(patch)
            await state.service.close()
            try:
                await state.start_engine()
            except Exception:
                state.config.save(previous)
                await state.start_engine()
                raise HTTPException(422,"配置未能应用，已恢复原配置，请核对服务地址与参数") from None
        return result

    @app.get('/portal/api/source-choices')
    async def available_sources(request: Request):
        user, state = current(request), runtime(request)
        from .source_choices import source_choices
        # Source settings are applied by the data adapter; admins can inspect
        # the raw provider catalogue without changing routing or permissions.
        data = state.config.read()['data']
        async with DataClient(data['base_url'], data['api_key'], data['mode'], unified_config=state.config.read()['unified_data']) as client:
            catalog = await client.catalog()
        rows = source_choices(catalog, state.config.read()['sources'])
        if user['role'] != 'admin':
            rows = [row for row in rows if row['enabled']]
        return {'sources': rows, 'errors': catalog.get('errors', []),
                'note': '光学条目是底图供应商；可选目录不保证本地点一定有清晰影像。'}

