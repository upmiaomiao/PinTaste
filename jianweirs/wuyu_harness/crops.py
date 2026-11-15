"""Original-pixel crops of registered images; no filesystem paths from callers.

Public Service calls enforce the current portal ownership scope. The internal
pipeline helper accepts its already-authorized Store and optional callbacks.
Cropping never creates a new satellite observation or new native detail.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
import math
import re
from threading import RLock

from PIL import Image, UnidentifiedImageError

from .schemas import ImageMetadata, UploadRequest
from .strategy import source_family

_CROP_LOCK = RLock()
_MAX_CROPS_PER_ROOT = 16
_MAX_MODEL_SIDE = 4096
_TEMPORAL_KEYS = {"provider", "source", "map_name", "remote_source", "product_year", "requested_date", "actual_date",
                  "actual_dates", "acquisition_time", "acquisition_time_scope", "metadata_scope", "actual_date_scope",
                  "actual_date_method", "date_semantics", "edition_is_capture_date", "native_resolution_m",
                  "temporal_evidence", "retrieved_at", "attribution", "copyright", "year", "edition", "product"}


def crop_from_store(store, asset_id: str, pixel_box, padding: float = .15, *, authorize_asset=None,
                    register_derived_owner=None) -> dict:
    """Return the raw registered crop for an already-authorized internal caller.

    Coordinates refer to the parent's original pixel raster, [left, top, right,
    bottom], with right and bottom exclusive. Padding adds existing surrounding
    pixels and is clipped to the original image; no interpolation is performed.
    """
    if not isinstance(asset_id,str) or not re.fullmatch(r'[0-9a-f]{32}',asset_id):
        raise ValueError("裁剪需要当前会话已登记的影像ID，不接收文件路径或URL")
    if authorize_asset is not None:
        authorize_asset(asset_id)
    if (not isinstance(pixel_box,(list,tuple)) or len(pixel_box)!=4
            or any(type(value) is not int for value in pixel_box)):
        raise ValueError("裁剪框必须为原图整数像素[left,top,right,bottom]，右下边界不包含在内")
    if type(padding) not in {float,int} or not math.isfinite(padding) or not 0<=padding<=1:
        raise ValueError("裁剪上下文扩展比例需为0至1")
    source=store.get(asset_id,"image")
    if source.get("metadata",{}).get("modality","optical") != "optical" or source.get("science_preview"):
        raise ValueError("此工具只裁剪已登记高分光学原图；科学数值影像请使用对应裁剪接口")
    width,height=source.get("width"),source.get("height")
    if type(width) is not int or type(height) is not int or width*height>40_000_000:
        raise ValueError("原图尺寸记录无效或超过四千万像素")
    left,top,right,bottom=pixel_box
    if not 0<=left<right<=width or not 0<=top<bottom<=height:
        raise ValueError("裁剪框超出原图范围；不会静默改动所选目标框")
    dx,dy=round((right-left)*padding),round((bottom-top)*padding)
    window=[max(0,left-dx),max(0,top-dy),min(width,right+dx),min(height,bottom+dy)]
    if max(window[2]-window[0],window[3]-window[1])>_MAX_MODEL_SIDE:
        raise ValueError("裁剪边长超过4096像素，请选择更小的原图窗口")
    raw_path=store.root/"assets"/source["file"]
    path=store.asset_path(source)
    if raw_path.is_symlink() or not path.is_file() or path.stat().st_size>store.max_upload_bytes:
        raise ValueError("原图文件不可用或超过当前存储限制")
    source_bytes=path.read_bytes()
    fingerprint=hashlib.sha256(source_bytes).hexdigest()
    if source.get("sha256") and source["sha256"]!=fingerprint:
        raise ValueError("原图文件与已登记哈希不一致，停止裁剪以保留证据一致性")
    parent_provenance=source.get("crop_provenance") or {}
    root_id=parent_provenance.get("root_asset_id") or asset_id
    with _CROP_LOCK:
        with store.connect() as db:
            existing=[json.loads(row[0]) for row in db.execute(
                "SELECT body FROM records WHERE kind='image' AND json_extract(body,'$.crop_provenance.root_asset_id')=?",(root_id,))]
        for item in existing:
            if item.get("derived_from_asset_id")==asset_id and item.get("source_pixel_window")==window and item.get("source_sha256")==fingerprint:
                if not store.asset_path(item).is_file():continue
                if register_derived_owner is not None:register_derived_owner(item["id"])
                return item
        if len(existing)>=_MAX_CROPS_PER_ROOT:
            raise ValueError("同一原始影像的局部裁剪已达16个上限，请复用已有局部图或整理后继续")
        try:
            with Image.open(io.BytesIO(source_bytes)) as image:
                if image.format not in {"PNG","JPEG"} or image.size!=(width,height):
                    raise ValueError("原图格式或实际尺寸与登记记录不一致")
                if image.getexif().get(274,1) not in {None,1}:
                    raise ValueError("原图含旋转方向标记，需先规范像素方向再使用裁剪框")
                image.load()
                crop=image.crop(tuple(window))
                # Preserve transparent/nodata pixels. Dropping alpha would
                # expose hidden RGB values as if they were visible evidence.
                crop=crop.convert("RGBA" if image.mode in {"RGBA","LA"} or "transparency" in image.info else "RGB")
                stream=io.BytesIO();crop.save(stream,"PNG")
        except (OSError,UnidentifiedImageError,Image.DecompressionBombError):
            raise ValueError("原图无法安全解码，未生成裁剪结果") from None
        metadata=ImageMetadata.model_validate({**source["metadata"],"role":"target_crop",
            "georeferenced":False,"registration_verified":False,"radiometric_comparability_verified":False,
            "metadata_scope":"derived_original_pixel_window"})
        registered=store.upload(UploadRequest(filename="见微局部复核.png",content_base64=base64.b64encode(stream.getvalue()).decode(),metadata=metadata))
        asset=store.get(registered["id"],"image")
        original_api=source.get("data_api_metadata") or {}
        api={key:deepcopy(value) for key,value in original_api.items() if key in _TEMPORAL_KEYS}
        parent_registration=(source.get("spatial_registration") or original_api.get("spatial_registration") or {})
        eligible=parent_registration.get("status")=="verified" and parent_registration.get("strict_same_point_eligible") is True
        registration={"status":"verified" if eligible else "unknown", "strict_same_point_eligible":eligible,
            "method":"original_pixel_subset_of_parent", "scope":"parent_scene_correspondence_only",
            "parent_asset_id":asset_id,"crop_geographic_transform_verified":False,
            "reason":"继承父图已核验的场景对应关系；没有将父图整幅范围当作裁剪后地理范围"}
        api.update(width=asset["width"],height=asset["height"],georeferenced=False,
                   spatial_registration=registration,actual_bbox_wgs84=None,
                   requested_bbox_is_returned_footprint=False)
        root_window=parent_provenance.get("root_source_pixel_window")
        root_window=[window[0]+root_window[0],window[1]+root_window[1],window[2]+root_window[0],window[3]+root_window[1]] if root_window else list(window)
        asset.update(derived_from_asset_id=asset_id,source_pixel_window=window,source_sha256=fingerprint,
            independence_group=source.get("independence_group") or source_family(metadata.source),
            acquisition_phase="adaptive_local_crop",independent_evidence=False,
            display_resampled=False,native_detail_increased=False,
            crop_provenance={"root_asset_id":root_id,"root_source_pixel_window":root_window,
                "requested_pixel_box":list(pixel_box),"padding":padding,"pixel_convention":"left_top_right_bottom_exclusive",
                "method":"original_pixels_no_interpolation","source_sha256":fingerprint},
            data_api_metadata=api,spatial_registration=registration,
            analysis_scope=source.get("analysis_scope","target_screening"))
        if source.get("target_pixel_sufficiency_reason"):
            asset["target_pixel_sufficiency_reason"]=source["target_pixel_sufficiency_reason"]
        store.put("image",asset)
        if register_derived_owner is not None:register_derived_owner(asset["id"])
        return asset


def crop_registered_image(service, asset_id: str, pixel_box, padding: float = .15) -> dict:
    """Public tool boundary: enforce current portal owner, or local MCP Store."""
    from .usage_context import _scope
    scope=_scope.get()
    authorize,register=None,None
    if scope is not None:
        limits,user_id=scope
        def authorize(identity):
            from fastapi import HTTPException
            try:limits.db.require_owner("image",identity,user_id)
            except HTTPException:raise KeyError("记录不存在") from None
        register=lambda identity:limits.db.own("image",identity,user_id)
    asset=crop_from_store(service.store,asset_id,pixel_box,padding,
                         authorize_asset=authorize,register_derived_owner=register)
    return {**service.store.public_asset(asset),"analysis_status":"not_analyzed",
            "independent_evidence":False,"native_detail_increased":False,
            "next_action":"将局部图与父图一并交给独立复核；裁图本身不产生新的判断、原生细节或独立来源。",
            "recommended_asset_ids":[asset["id"],asset_id]}
