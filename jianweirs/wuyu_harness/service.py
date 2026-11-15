"""Application lifecycle shared by the HTTP API and the stdio MCP provider."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import httpx

from .config import Settings
from .data import DataClient
from .models import ModelClient
from .pipeline import Pipeline, task_plan
from .search import SearchClient
from .schemas import ImageMetadata, TaskRequest, UploadRequest
from .store import Store


class Service:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.store = Store(self.settings.output_dir, self.settings.max_upload_bytes, self.settings.max_workbook_bytes,
                           persistent_connections=True)   # 服务进程长期运行，复用连接
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.data = DataClient()
        self.models = ModelClient(self.http)
        self.policy = SearchClient(self.http, self.settings)
        self.pipeline = Pipeline(self.settings, self.store, self.data, self.models, self.policy)
        self.lock_file = None

    async def start(self) -> Service:
        self.lock_file = (self.store.root / ".runtime.lock").open("a+b")
        self.lock_file.seek(0)
        self.lock_file.write(b"0")
        self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            await self.http.aclose()
            raise RuntimeError("此输出目录已有无隅服务运行；请复用该服务或设置独立 WUYU_OUTPUT_DIR") from None
        self.store.recover()
        return self

    async def close(self) -> None:
        from .complaint_service import close_scan_dispatchers
        await close_scan_dispatchers(self)
        pending = list(self.pipeline.pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.data.aclose()
        await self.http.aclose()
        close_store = getattr(self.store, 'close', None)
        if close_store:
            close_store()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    async def plan(self, request: TaskRequest) -> dict:
        from .data import DataClientError
        try:
            catalog = await self.data.catalog()
        except DataClientError as error:
            catalog = {"groups": {}, "errors": [{"code": error.code}]}
        return task_plan(request, catalog)

    def import_file(self, path: str, metadata: dict | None = None) -> dict:
        root = Path(os.getenv("WUYU_PROJECT_ROOT", str(Path.cwd()))).resolve()
        candidate = Path(path).resolve()
        extra = os.getenv("WUYU_IMPORT_ROOTS", "").strip()
        if extra:
            try:
                configured = json.loads(extra) if extra.startswith("[") else extra.split(os.pathsep)
                if not isinstance(configured, list) or any(not isinstance(item, str) or not item.strip() for item in configured):
                    raise ValueError()
                roots = [root, *(Path(item).resolve() for item in configured)]
                if any(not allowed.is_dir() or allowed == Path(allowed.anchor) for allowed in roots):
                    raise ValueError()
            except (ValueError, TypeError, OSError):
                raise ValueError("WUYU_IMPORT_ROOTS 必须是已有目录的 JSON 列表或分隔列表") from None
        else:
            roots = [root]
        if not any(candidate.is_relative_to(allowed) for allowed in roots) or not candidate.is_file():
            raise ValueError("仅可导入项目或 WUYU_IMPORT_ROOTS 明确授权资料目录内的文件")
        if candidate.suffix.lower() not in (".png", ".jpg", ".jpeg", ".xlsx", ".txt", ".md", ".json", ".csv", ".docx", ".pdf"):
            raise ValueError("不支持此文件类型")
        return self.store.import_path(candidate, ImageMetadata.model_validate(metadata or {}))

    def workbook_records(self, document_id: str, offset: int = 0, limit: int = 20,
                         record_ids: list[str] | None = None) -> dict:
        return self.store.workbook_records(document_id, offset, limit, record_ids)

    def workbook_point_assets(self, document_id: str, record_id: str) -> dict:
        return self.store.workbook_point_assets(document_id, record_id)

    def crop_image(self, asset_id: str, pixel_box, padding: float = .15) -> dict:
        from .crops import crop_registered_image
        return crop_registered_image(self,asset_id,pixel_box,padding)
