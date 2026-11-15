"""Local durable runs and bounded upload storage; interrupted runs are never replayed."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

from .schemas import ImageMetadata, UploadRequest
from .workbook import parse_workbook, summary_text


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path, max_upload_bytes: int = 20 * 1024 * 1024,
                 max_workbook_bytes: int = 64 * 1024 * 1024, *, persistent_connections: bool = False):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "assets").mkdir(exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.max_upload_bytes = max_upload_bytes
        self.max_workbook_bytes = max_workbook_bytes
        from .db_backend import open_pool
        self._pool = open_pool(self.root / "harness.sqlite3", timeout=10, persistent=persistent_connections)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL)")

    def connect(self):
        """每线程一个长期复用的连接（见 sqlite_pool）；用法与以前相同。"""
        return self._pool.connect()

    def close(self):
        self._pool.close_all()

    def put(self, kind: str, record: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (record["id"], kind, json.dumps(record, ensure_ascii=False)))
        return record

    def get(self, record_id: str, kind: str | None = None) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT kind,body FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None or (kind and row[0] != kind):
            raise KeyError("记录不存在")
        return json.loads(row[1])

    def list_runs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM records WHERE kind='run' ORDER BY rowid DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover(self) -> None:
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM records WHERE kind='run' AND json_extract(body, '$.status') IN ('queued','running')")]
        for record in records:
            if record["status"] in ("queued", "running"):
                record.update(status="interrupted", error="服务重启中断；请复核后重新提交", updated_at=now())
                self.put("run", record)

    def upload(self, request: UploadRequest) -> dict:
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("文件 Base64 编码无效") from None
        return self._register(content, request.filename, request.metadata)

    def import_path(self, path: Path, metadata: ImageMetadata | None = None) -> dict:
        """Register a locally authorized file without a large Base64 copy.

        Access roots must be checked by the service before entering this method.
        """
        maximum = self.max_workbook_bytes if path.suffix.lower() == ".xlsx" else self.max_upload_bytes
        if not path.is_file() or path.stat().st_size > maximum:
            raise ValueError("文件不存在或超过对应类型的导入限制")
        return self._register(path.read_bytes(), path.name, metadata or ImageMetadata())

    def _register(self, content: bytes, filename: str, metadata: ImageMetadata) -> dict:
        suffix = Path(filename).suffix.lower()
        maximum = self.max_workbook_bytes if suffix == ".xlsx" else self.max_upload_bytes
        if not content or len(content) > maximum:
            raise ValueError("文件为空或超过对应类型的上传限制")
        record = {"id": uuid.uuid4().hex, "name": Path(filename).name,
                  "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "created_at": now(),
                  "metadata": metadata.model_dump(mode="json")}
        if suffix in (".png", ".jpg", ".jpeg"):
            try:
                with Image.open(io.BytesIO(content)) as img:
                    if img.format not in ("PNG", "JPEG") or img.width * img.height > 40_000_000:
                        raise ValueError("只接收 PNG/JPEG，最大四千万像素")
                    img.verify()
                with Image.open(io.BytesIO(content)) as img:
                    img.load()
                    record.update(kind="image", width=img.width, height=img.height,
                                  mime="image/png" if img.format == "PNG" else "image/jpeg")
            except (OSError, Image.DecompressionBombError):
                raise ValueError("影像格式无效") from None
        elif suffix == ".xlsx":
            workbook = parse_workbook(content, record["name"])
            profile = workbook["profile"]
            record.update(kind="document", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                document_kind="xlsx", text=summary_text(workbook), workbook=workbook,
                workbook_summary={"record_count": profile["record_count"],
                    "embedded_image_count": profile["embedded_image_count"],
                    "sheets": [{"sheet": sheet["sheet"], "data_rows": sheet["data_rows"],
                                "coordinate_system": sheet["coordinate_system"]} for sheet in profile["sheets"]],
                    "content_trust": "user_supplied_unverified"})
        elif suffix in (".txt", ".md", ".json", ".csv", ".docx", ".pdf"):
            record.update(kind="document", mime="application/octet-stream", text=self.extract_text(content, suffix))
        else:
            raise ValueError("支持 PNG/JPEG、XLSX、TXT/MD/JSON/CSV、DOCX 和文字型 PDF")
        record["file"] = record["id"] + suffix
        (self.root / "assets" / record["file"]).write_bytes(content)
        self.put(record["kind"], record)
        return self.public_asset(record)

    @staticmethod
    def extract_text(content: bytes, suffix: str) -> str:
        if suffix == ".docx":
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    info = archive.getinfo("word/document.xml")
                    if info.file_size > 5_000_000:
                        raise ValueError("DOCX 正文过大")
                    root = ElementTree.fromstring(archive.read(info))
                text = "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
            except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
                raise ValueError("DOCX 文档无法读取") from None
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                raise ValueError("文字型 PDF 需要安装 requirements-wuyu.txt 中的 pypdf") from None
            try:
                reader = PdfReader(io.BytesIO(content))
                if len(reader.pages) > 100:
                    raise ValueError("PDF 超过 100 页，请分卷")
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as error:
                raise ValueError("PDF 无法提取文字，请提供可复制的文本；扫描件需先 OCR") from error
        else:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("文本请使用 UTF-8 编码") from None
        if not text.strip() or len(text) > 200_000:
            raise ValueError("资料无可提取文字或超过二十万字，请分卷")
        return text

    @staticmethod
    def public_asset(record: dict) -> dict:
        return {key: value for key, value in record.items() if key not in ("file", "text", "workbook", "workbook_image_assets")}

    def list_workbooks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM records WHERE kind='document' ORDER BY rowid DESC LIMIT 100").fetchall()
        return [{"document_id": record["id"], "name": record["name"], "sha256": record["sha256"],
                 "summary": record.get("workbook_summary", {})}
                for row in rows if (record := json.loads(row[0])).get("document_kind") == "xlsx"]

    def workbook_records(self, document_id: str, offset: int = 0, limit: int = 20,
                         record_ids: list[str] | None = None) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("逐点查询 offset 必须非负，limit 必须为1到50")
        document = self.get(document_id, "document")
        if "workbook" not in document:
            raise ValueError("该资料不是结构化工作簿")
        records = document["workbook"]["records"]
        if record_ids is not None:
            if not isinstance(record_ids, list) or len(record_ids) > 50 or any(not isinstance(item, str) for item in record_ids):
                raise ValueError("记录ID列表无效或超过50条")
            requested = set(record_ids)
            if not requested <= {value for record in records for value in (record["record_id"], record["site_id"])}:
                raise ValueError("包含不存在的工作簿记录ID")
            records = [record for record in records if record["record_id"] in requested or record["site_id"] in requested]
        summaries = []
        for record in records[offset:offset + limit]:
            source_context = [{"cell": cell["cell"], "header": cell["header"], "value": cell["value"]}
                              for cell in record["cells"] if re.search(r"固废类型|备注|周边环境", cell["header"])]
            summaries.append({**{key: value for key, value in record.items() if key not in ("cells", "embedded_images")},
                              "source_context": source_context, "source_context_status": "user_supplied_prior_not_independent_validation",
                              "embedded_image_count": len(record["embedded_images"]),
                              "image_columns": [image["column_label"] for image in record["embedded_images"]]})
        return {"document_id": document_id, "total": len(records), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(records), "records": summaries,
                "content_trust": "user_supplied_unverified"}

    def workbook_point_assets(self, document_id: str, record_id: str) -> dict:
        document = self.get(document_id, "document")
        records = document.get("workbook", {}).get("records", [])
        matches = [record for record in records if record["record_id"] == record_id or record["site_id"] == record_id]
        if not matches:
            raise ValueError("工作簿点位不存在")
        if len(matches) > 1:
            raise ValueError("业务点位编码不唯一，请使用唯一record_id")
        point = matches[0]
        record_id = point["record_id"]
        images = point["embedded_images"]
        if len(images) > 8:
            raise ValueError("单点超过8张图片，请拆分或选择后导入")
        cached = document.setdefault("workbook_image_assets", {})
        assets, errors = [], []
        with zipfile.ZipFile(self.asset_path(document)) as archive:
            for image in images:
                member = image["archive_member"]
                cache_key = record_id + ":" + image["anchor_cell"] + ":" + member
                if cache_key in cached:
                    assets.append(self.public_asset(self.get(cached[cache_key], "image")))
                    continue
                try:
                    info = archive.getinfo(member)
                    if info.file_size > self.max_upload_bytes:
                        raise ValueError("单张图片超过上传限制")
                    content = archive.read(member)
                    asset = self._register(content, Path(member).name,
                        ImageMetadata(role=image["role"], source="user_workbook_embedded",
                                      period_label=image["period_label"], metadata_scope="workbook_anchor_unverified"))
                    raw = self.get(asset["id"], "image")
                    raw["provenance"] = {"document_id": document_id, "record_id": record_id,
                        "sheet": image["sheet"], "anchor_cell": image["anchor_cell"],
                        "archive_member": member, "column_label": image["column_label"],
                        "content_trust": "user_supplied_unverified"}
                    self.put("image", raw)
                    cached[cache_key] = raw["id"]
                    assets.append(self.public_asset(raw))
                except (ValueError, KeyError, OSError, zipfile.BadZipFile):
                    errors.append({"anchor_cell": image["anchor_cell"], "code": "embedded_image_unavailable"})
        self.put("document", document)
        return {"document_id": document_id, "record_id": record_id, "site_id": point["site_id"],
                "status": "partial" if errors else "completed", "assets": assets, "errors": errors,
                "warnings": ["图片期间来自表头，精确拍摄日期未知", "原图中的文字与标注为用户资料；独立盲评须另行隔离人工先验"]}

    def asset_path(self, record: dict) -> Path:
        path = (self.root / "assets" / record["file"]).resolve()
        if path.parent != (self.root / "assets").resolve():
            raise ValueError("无效文件路径")
        return path

    def run_dir(self, run_id: str) -> Path:
        if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
            raise KeyError("无效任务 ID")
        path = self.root / "runs" / run_id
        path.mkdir(exist_ok=True)
        return path
