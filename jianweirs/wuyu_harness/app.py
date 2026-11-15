"""Loopback-first HTTP API for tasks, uploads, evidence and report downloads."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import Field

from .config import Settings
from .data import DataClientError
from .schemas import Record, TaskRequest, UploadRequest, TASK_LABELS
from .service import Service


class DataOperation(Record):
    operation: str = Field(min_length=1, max_length=80)
    parameters: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        service = Service(settings)
        application.state.service = await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="无隅见微 · 多星遥感 Harness", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        service = request.app.state.service
        expected = service.settings.service_key
        if request.url.path != "/health" and expected:
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + expected):
                return JSONResponse({"detail": "需要有效的服务 Bearer 凭证"}, status_code=401)
        if request.method == "POST":
            # Reject browser cross-site writes even when the local API has no service key.
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不接受跨站任务或上传"}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效 Content-Length"}, status_code=400)
            if size > 29_000_000:
                return JSONResponse({"detail": "请求体过大"}, status_code=413)
            chunks, received = [], 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > 29_000_000:
                    return JSONResponse({"detail": "请求体过大"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, error):
        return JSONResponse({"detail": [{"loc": item["loc"], "type": item["type"], "message": item["msg"]} for item in error.errors()]}, status_code=422)

    @app.exception_handler(DataClientError)
    async def data_error(request, error):
        return JSONResponse({"detail": str(error), "code": error.code}, status_code=502)

    @app.exception_handler(KeyError)
    async def missing_error(request, error):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.exception_handler(ValueError)
    async def validation_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    def service(request: Request) -> Service:
        return request.app.state.service

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "wuyu_harness"}

    @app.get("/api/status")
    async def status(request: Request):
        return service(request).settings.public_status()

    @app.get("/api/task-types")
    async def task_types():
        return {kind.value: label for kind, label in TASK_LABELS.items()}

    @app.get("/api/catalog")
    async def catalog(request: Request):
        return await service(request).data.catalog()

    @app.post("/api/data/execute")
    async def execute_data(operation: DataOperation, request: Request):
        return await service(request).data.execute(operation.operation, operation.parameters)

    @app.post("/api/uploads", status_code=201)
    async def upload(upload: UploadRequest, request: Request):
        return service(request).store.upload(upload)

    @app.post("/api/plan")
    async def plan(task: TaskRequest, request: Request):
        return await service(request).plan(task)

    @app.post("/api/runs", status_code=202)
    async def submit(task: TaskRequest, request: Request):
        record = service(request).pipeline.submit(task)
        return {**record, "status_url": f"/api/runs/{record['id']}"}

    @app.get("/api/runs")
    async def list_runs(request: Request):
        return service(request).store.list_runs()

    @app.get("/api/runs/{run_id}")
    async def run_status(run_id: str, request: Request):
        return service(request).store.get(run_id, "run")

    @app.get("/api/runs/{run_id}/artifacts/{filename}")
    async def artifact(run_id: str, filename: str, request: Request):
        current = service(request)
        record = current.store.get(run_id, "run")
        if record["status"] != "succeeded":
            raise HTTPException(409, "任务尚未生成可用报告")
        folder = current.store.run_dir(run_id)
        path = (folder / filename).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'"})

    return app


app = create_app()
