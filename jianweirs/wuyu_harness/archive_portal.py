"""Owner-scoped archive HTTP adapters, separate from model/task execution."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import PurePosixPath
import re
import uuid

from fastapi import HTTPException, Request

from .archive_ingest import ArchiveIngestError, ArchivePackage, ArchiveLimits
from .schemas import ImageMetadata, UploadRequest
from .store import now


def _summary(plan: dict, archive_id: str) -> dict:
    return {"image_count": plan["image_count"], "supported_file_count": plan["supported_file_count"],
            "unsupported_file_count": plan["unsupported_file_count"], "invalid_file_count": plan["invalid_file_count"],
            "batch_required": plan["batch_required"], "task_image_limit": plan["task_image_limit"],
            "requires_image_selection": plan["image_count"] > 0,
            "manifest_url": f"/portal/api/uploads/{archive_id}/archive",
            "original_download_url": f"/portal/api/assets/{archive_id}",
            "model_analysis_started": False, "content_trust": "user_supplied_unverified"}


def register_archive_upload(state, user: dict, filename: str, content: bytes) -> dict:
    """Validate and preserve the original ZIP; caller runs this in ``to_thread``.

    The generated storage filename is independent of every untrusted ZIP name.
    No image is automatically added to a task or submitted to a model.
    """
    package = ArchivePackage(content, filename)
    plan, store, identity = package.plan(), state.service.store, uuid.uuid4().hex
    counts = {kind: sum(row.get("status") == "ready" and row.get("import_kind") == kind for row in plan["files"])
              for kind in ("display_image", "numeric_raster", "metadata_candidate")}
    record = {"id": identity, "name": filename, "sha256": plan["archive_sha256"], "bytes": len(content),
        "created_at": now(), "kind": "document", "document_kind": "zip", "mime": "application/zip",
        "metadata": {"source": "user_upload", "metadata_scope": "archive_manifest_unverified"},
        "text": (f"用户上传 ZIP 影像资料。已验证显示影像{counts['display_image']}张，数值栅格{counts['numeric_raster']}个，"
                 f"位置资料候选{counts['metadata_candidate']}个，未支持文件{plan['unsupported_file_count']}个。"
                 "需要用户从清单选择影像后分批添加任务；当前尚未进行模型判读或配准。"),
        "archive_summary": _summary(plan, identity), "file": identity + ".zip"}
    path = store.asset_path(record)
    written, stored = False, False
    try:
        with path.open("xb") as output:
            written = True
            output.write(content)
        path.chmod(0o600)
        store.put("document", record)
        stored = True
        state.db.own("document", identity, user["id"])
    except Exception:
        if stored:
            with store.connect() as db:
                db.execute("DELETE FROM records WHERE id=? AND kind='document'", (identity,))
        if written:
            path.unlink(missing_ok=True)
        raise
    return {**store.public_asset(record), "url": f"/portal/api/assets/{identity}"}


def _owned_package(state, user: dict, document_id: str) -> tuple[dict, ArchivePackage]:
    if not isinstance(document_id, str) or not re.fullmatch(r"[a-f0-9]{32}", document_id):
        raise HTTPException(404, "资源不存在")
    state.db.require_owner("document", document_id, user["id"])
    try:
        record = state.service.store.get(document_id, "document")
    except KeyError:
        raise HTTPException(404, "资源不存在") from None
    if record.get("document_kind") != "zip":
        raise HTTPException(404, "资源不存在")
    path = state.service.store.asset_path(record)
    try:
        with path.open("rb") as source:
            content = source.read(ArchiveLimits().max_zip_bytes+1)
    except OSError:
        raise HTTPException(404, "资源不存在") from None
    if hashlib.sha256(content).hexdigest() != record.get("sha256"):
        raise HTTPException(409, "原始 ZIP 校验不一致，请重新上传。")
    try:
        return record, ArchivePackage(content, record["name"])
    except ArchiveIngestError as error:
        raise HTTPException(422, str(error)) from None


def _register_selected(state, user: dict, record: dict, package: ArchivePackage, members: list[str]) -> dict:
    store, plan = state.service.store, package.plan()
    by_id = {row["id"]: row for row in plan["files"]}
    if any(member not in by_id or by_id[member].get("status") != "ready"
           or by_id[member].get("import_kind") not in {"display_image", "numeric_raster"} for member in members):
        raise HTTPException(422, "请选择本次 ZIP 清单中验证通过的影像。")
    assets, pending = [], []
    cached = record.setdefault("archive_image_assets", {})
    for member in members:
        row = by_id[member]
        if row["import_kind"] == "numeric_raster":
            pending.append({"member_id": member, "original_filename": row["original_filename"],
                "reason": "needs_band_and_modality_configuration",
                "message": "数值 TIFF 原始文件已保留；需先确认波段、传感器模态与分析工具，本入口暂不添加为高分判读影像。"})
            continue
        previous = cached.get(member)
        if previous:
            state.db.require_owner("image", previous, user["id"])
            try:
                asset = store.get(previous, "image")
            except KeyError:
                raise HTTPException(409, "已导入影像记录缺失，请重新上传资料包。") from None
            provenance = asset.get("provenance", {})
            if provenance.get("source_archive_id") != record["id"] or provenance.get("original_member_sha256") != row["sha256"]:
                raise HTTPException(409, "已导入影像来源校验不一致。")
        else:
            try:
                preview, display = package.preview(member)
            except ArchiveIngestError as error:
                raise HTTPException(422, str(error)) from None
            filename = PurePosixPath(row["original_filename"]).stem[:100] + "-preview.png"
            public = store.upload(UploadRequest(filename=filename, content_base64=base64.b64encode(preview).decode("ascii"),
                metadata=ImageMetadata(source="user_upload", metadata_scope="archive_display_preview_unverified")))
            asset = store.get(public["id"], "image")
            asset.update(derived_display_only=True, sensor_modality="unclassified",
                provenance={"source_archive_id": record["id"], "source_archive_sha256": record["sha256"],
                    "source_member_id": member, "source_member_filename": row["original_filename"],
                    "original_member_sha256": row["sha256"], "display_transform": display,
                    "content_trust": "user_supplied_unverified"})
            store.put("image", asset)
            state.db.own("image", asset["id"], user["id"])
            cached[member] = asset["id"]
        assets.append(store.public_asset(asset))
    store.put("document", record)
    return {"archive_id": record["id"], "assets": assets, "pending": pending,
            "status": "partial" if assets and pending else "needs_configuration" if pending else "completed",
            "model_analysis_started": False, "task_image_limit": 8,
            "original_download_url": f"/portal/api/assets/{record['id']}",
            "notice": "已添加的是派生显示影像；模型尚未分析，拍摄日期、位置与传感器类型仍需核实。"}


def install_archive_routes(app, current, runtime, claim_asset) -> None:
    """Install owner/CSRF-aware routes using the portal's supplied auth callbacks."""
    import_lock = asyncio.Lock()

    @app.get("/portal/api/uploads/{document_id}/archive")
    async def archive_manifest(document_id: str, request: Request):
        user, state = current(request), runtime(request)
        record, package = await asyncio.to_thread(_owned_package, state, user, document_id)
        return {**package.plan(), "archive_id": record["id"],
                "original_download_url": f"/portal/api/assets/{record['id']}", "model_analysis_started": False}

    @app.post("/portal/api/uploads/{document_id}/archive/images")
    async def archive_images(document_id: str, request: Request):
        user, state = current(request), runtime(request)
        # Check ownership even for malformed submissions, avoiding existence leaks.
        if not re.fullmatch(r"[a-f0-9]{32}", document_id):
            raise HTTPException(404, "资源不存在")
        state.db.require_owner("document", document_id, user["id"])
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "请求需要JSON对象。") from None
        members = body.get("member_ids") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or set(body) != {"member_ids"} or not isinstance(members, list)
                or not 1 <= len(members) <= 8 or any(not isinstance(member, str) or not re.fullmatch(r"[a-f0-9]{32}", member) for member in members)
                or len(members) != len(set(members))):
            raise HTTPException(422, "请选择1至8个不重复的清单影像编号，不支持自定义路径或额外参数。")
        async with import_lock:
            record, package = await asyncio.to_thread(_owned_package, state, user, document_id)
            result = await asyncio.to_thread(_register_selected, state, user, record, package, members)
        result["assets"] = [claim_asset(state, asset, user["id"]) for asset in result["assets"]]
        return result
