"""Open scientific optical data: real STAC/CMR discovery and bounded COG subsets.

Multispectral and hyperspectral products are explicitly distinguished. Anonymous
catalogue discovery does not imply authenticated science-file download access.
No local Ovital settings, browser credentials or user supplied URLs are read.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .planetary_signing import PC_TOKEN_ROOT, TOKEN_CACHE, sign_href

PC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
DLR_ROOT = "https://geoservice.dlr.de/eoc/ogc/stac/v1"
CMR_SEARCH = "https://cmr.earthdata.nasa.gov/search/granules.json"
SUBSET_DIR = Path(os.getenv("OMAP_SPECTRAL_SUBSET_DIR", str(Path(__file__).resolve().parent.parent / "outputs" / "spectral" / "subsets")))
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,220}$")
FILE_RE = re.compile(r"^[0-9a-f]{32}\.tif$")
SUBSET_SEMAPHORE = asyncio.Semaphore(2)
SCENE_CACHE: OrderedDict = OrderedDict()
SCENE_CACHE_TTL_SECONDS = 300
SourceID = Literal["sentinel2", "landsat", "hls_s30", "hls_l30", "emit", "enmap"]

SOURCES: dict[str, dict[str, Any]] = {
    "sentinel2": {
        "id": "sentinel2", "name": "Sentinel-2 L2A surface reflectance",
        "spectral_type": "multispectral", "provider": "Microsoft Planetary Computer",
        "catalogue": "stac", "collection": "sentinel-2-l2a", "root": PC_ROOT,
        "native_resolution_m": [10, 20, 60], "science_format": "COG GeoTIFF",
        "download_authentication_required": False, "download_access": "anonymous_sas",
        "roi_subset_available": True,
        "documentation": "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a",
        "limitations": ["多光谱，不是高光谱。", "L2A 未提供 L1C 的 B10 卷云波段。", "子集保留原始 DN，反射率换算须依据产品元数据。"],
    },
    "landsat": {
        "id": "landsat", "name": "Landsat Collection 2 Level-2 surface reflectance",
        "spectral_type": "multispectral", "provider": "Microsoft Planetary Computer / USGS",
        "catalogue": "stac", "collection": "landsat-c2-l2", "root": PC_ROOT,
        "native_resolution_m": [30], "science_format": "COG GeoTIFF",
        "download_authentication_required": False, "download_access": "anonymous_sas",
        "roi_subset_available": True,
        "documentation": "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2",
        "limitations": ["此集合包含 Landsat 4–9，不应把所有场景都称为 Landsat 8/9。", "Level-2 反射率不含 15 m 全色波段。", "热红外原生分辨率与分发栅格间距不同；本接口子集仅选反射率波段。"],
    },
    "hls_s30": {
        "id": "hls_s30", "name": "HLS Sentinel-2 S30 v2.0",
        "spectral_type": "multispectral", "provider": "NASA LP DAAC",
        "catalogue": "cmr", "short_name": "HLSS30", "version": "2.0",
        "native_resolution_m": [30], "science_format": "COG GeoTIFF",
        "resolution_semantics": "harmonized_product_grid", "product_grid_spacing_m": 30,
        "native_sensor_resolution_m": None,
        "download_authentication_required": True, "download_access": "earthdata_login",
        "roi_subset_available": False,
        "documentation": "https://hls.gsfc.nasa.gov/data-access-and-tools/",
        "limitations": ["30 m 表示统一产品网格，不是所有原始传感器波段的原生分辨率。", "目录可匿名检索，科学数据下载需要 NASA Earthdata Login。"],
    },
    "hls_l30": {
        "id": "hls_l30", "name": "HLS Landsat L30 v2.0",
        "spectral_type": "multispectral", "provider": "NASA LP DAAC",
        "catalogue": "cmr", "short_name": "HLSL30", "version": "2.0",
        "native_resolution_m": [30], "science_format": "COG GeoTIFF",
        "resolution_semantics": "harmonized_product_grid", "product_grid_spacing_m": 30,
        "native_sensor_resolution_m": None,
        "download_authentication_required": True, "download_access": "earthdata_login",
        "roi_subset_available": False,
        "documentation": "https://hls.gsfc.nasa.gov/data-access-and-tools/",
        "limitations": ["30 m 表示统一产品网格，不是所有原始传感器波段的原生分辨率。", "目录可匿名检索，科学数据下载需要 NASA Earthdata Login。"],
    },
    "emit": {
        "id": "emit", "name": "EMIT L2A reflectance, uncertainty and masks v001",
        "spectral_type": "hyperspectral", "provider": "NASA LP DAAC / JPL",
        "catalogue": "cmr", "short_name": "EMITL2ARFL", "version": "001",
        "native_resolution_m": [60], "band_count": 285,
        "wavelength_range_nm": [381, 2493], "science_format": "NetCDF-4",
        "download_authentication_required": True, "download_access": "earthdata_login",
        "roi_subset_available": False,
        "documentation": "https://doi.org/10.5067/EMIT/EMITL2ARFL.001",
        "limitations": ["ISS 覆盖约 52°S–52°N，按实际轨道影像检索，不保证任意地点有数据。", "反射率以非正射的原始几何存储，科学分析需要使用文件内 GLT/经纬度完成定位。", "RGB 浏览图不包含 285 波段，不能替代光谱数据。"],
    },
    "enmap": {
        "id": "enmap", "name": "EnMAP HSI L2A analysis-ready data",
        "spectral_type": "hyperspectral", "provider": "DLR EOC Geoservice",
        "catalogue": "stac", "collection": "ENMAP_HSI_L2A", "root": DLR_ROOT,
        "native_resolution_m": [30], "band_count": 224,
        "science_format": "multiband COG GeoTIFF",
        "download_authentication_required": True, "download_access": "enmap_registered_account",
        "roi_subset_available": False,
        "documentation": "https://web.geoservice.dlr.de/web/datasets/enmap_l2_hsi",
        "limitations": ["当前 L2A 分发集合的波段数按具体资产元数据返回；不能用早期设计指标替代产品波段数。", "公开 STAC 目录可检索，科学文件下载需要已注册的 EnMAP 账户。", "不同处理器版本的辐射和几何质量可能不同。"],
    },
}

router = APIRouter(prefix="/api/spectral", tags=["光谱：高光谱与多光谱"])


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _check_bbox(bbox: tuple[float, float, float, float], max_span: float) -> None:
    w, s, e, n = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ValueError("bbox 必须是有限的 WGS84 经度、纬度。")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("bbox 顺序为 [west,south,east,north]，不支持跨越日期变更线。")
    if e - w > max_span or n - s > max_span:
        raise ValueError(f"bbox 每个方向最多 {max_span}°；请分区查询。")


def _normal_datetime_interval(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError("datetime 必须为 start/end 日期区间，例如 2025-01-01/2026-10-10。")
    normalized = []
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            part += "T00:00:00Z" if index == 0 else "T23:59:59.999999Z"
        try:
            dt = datetime.fromisoformat(part.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("datetime 必须包含有效 ISO 8601 日期。") from exc
        if dt.tzinfo is None:
            raise ValueError("时间必须包含时区，例如 Z 或 +08:00。")
        normalized.append(dt.astimezone(timezone.utc))
    if normalized[0] > normalized[1]:
        raise ValueError("datetime 起始时间不能晚于结束时间。")
    return "/".join(v.isoformat().replace("+00:00", "Z") for v in normalized)


class SpectralSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceID = "emit"
    bbox: tuple[float, float, float, float]
    input_crs: Literal['EPSG:4326'] = 'EPSG:4326'
    datetime: str = Field(default_factory=lambda: "2022-01-01/" + datetime.now(timezone.utc).date().isoformat())
    limit: int = Field(default=10, ge=1, le=50)
    max_cloud_cover: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_search(self) -> "SpectralSearchRequest":
        _check_bbox(self.bbox, 5.0)
        self.datetime = _normal_datetime_interval(self.datetime)
        return self


class SpectralSubsetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceID = "sentinel2"
    scene_id: str = Field(min_length=1, max_length=220)
    bbox: tuple[float, float, float, float]
    input_crs: Literal['EPSG:4326'] = 'EPSG:4326'
    assets: list[str] = Field(default_factory=lambda: ["B02", "B03", "B04", "B08"], min_length=1, max_length=8)
    width: int = Field(default=256, ge=32, le=512)
    height: int = Field(default=256, ge=32, le=512)

    @model_validator(mode="after")
    def validate_subset(self) -> "SpectralSubsetRequest":
        _check_bbox(self.bbox, 0.1)
        if not ID_RE.fullmatch(self.scene_id):
            raise ValueError("scene_id 含有不支持的字符。")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("assets 不能重复。")
        if any(not ID_RE.fullmatch(key) for key in self.assets):
            raise ValueError("asset 名称含有不支持的字符。")
        return self


def _safe_https(href: Any) -> str | None:
    if not isinstance(href, str):
        return None
    parsed = urlparse(href)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return href


def _public_source(source: dict) -> dict:
    return {key: value for key, value in source.items() if key not in {"root", "short_name", "version"}}


def _asset_auth(source: str, href: str) -> bool:
    if source in {"emit", "hls_s30", "hls_l30"}:
        return "/lp-prod-protected/" in href or "opendap.earthdata.nasa.gov" in href
    return bool(SOURCES[source]["download_authentication_required"])


def normalize_stac_item(source_id: str, item: dict) -> dict:
    source = SOURCES[source_id]
    props = item.get("properties", {})
    assets = {}
    for key, asset in item.get("assets", {}).items():
        href = _safe_https(asset.get("href"))
        if not href:
            continue
        assets[key] = {
            "href": href, "media_type": asset.get("type"),
            "title": asset.get("title", key), "roles": asset.get("roles", []),
            "authentication_required": _asset_auth(source_id, href),
            "requires_anonymous_signing": source["root"] == PC_ROOT and urlparse(href).hostname.endswith(".blob.core.windows.net"),
            "bands": asset.get("eo:bands", []), "raster_bands": asset.get("raster:bands", []),
            "gsd": asset.get("gsd"),
            "epsg": asset.get("proj:epsg", props.get("proj:epsg")),
            "shape": asset.get("proj:shape", props.get("proj:shape")),
            "transform": asset.get("proj:transform", props.get("proj:transform")),
        }
    return {
        "id": item.get("id"), "source": source_id, "spectral_type": source["spectral_type"],
        "collection": item.get("collection", source.get("collection")),
        "acquired_at": props.get("datetime") or props.get("start_datetime"),
        "platform": props.get("platform"), "instruments": props.get("instruments") or props.get("eo:instrument"),
        "cloud_cover": props.get("eo:cloud_cover"), "bbox": item.get("bbox"), "geometry": item.get("geometry"),
        "epsg": props.get("proj:epsg"), "processing_version": props.get("version") or props.get("s2:processing_baseline"),
        "native_resolution_m": source["native_resolution_m"], "assets": assets,
        "authentication_required_for_science_data": source["download_authentication_required"],
    }


def normalize_cmr_granule(source_id: str, granule: dict) -> dict:
    source = SOURCES[source_id]
    assets = {}
    for link in granule.get("links", []):
        if link.get("inherited"):
            continue
        href = _safe_https(link.get("href"))
        rel = link.get("rel", "")
        if not href or not any(token in rel for token in ("data#", "browse#", "service#")):
            continue
        basename = urlparse(href).path.rsplit("/", 1)[-1]
        if "service#" in rel:
            key, roles, mime = "opendap", ["service"], None
        else:
            key = basename
            roles = ["overview"] if "browse#" in rel else ["data"]
            mime = "image/png" if basename.lower().endswith(".png") else "application/x-netcdf" if basename.lower().endswith(".nc") else "image/tiff" if basename.lower().endswith(".tif") else None
        assets[key] = {"href": href, "media_type": mime, "title": link.get("title", key), "roles": roles,
                       "authentication_required": _asset_auth(source_id, href), "requires_anonymous_signing": False}
    geometry = None
    coords = []
    for polygon_set in granule.get("polygons", []):
        for polygon in polygon_set:
            try:
                values = [float(v) for v in polygon.split()]
                ring = [[values[i + 1], values[i]] for i in range(0, len(values), 2)]
            except (TypeError, ValueError, IndexError):
                continue
            if len(ring) >= 4:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                coords.append([ring])
    if coords:
        geometry = {"type": "MultiPolygon", "coordinates": coords}
    flat = [point for poly in coords for ring in poly for point in ring]
    bbox = [min(p[0] for p in flat), min(p[1] for p in flat), max(p[0] for p in flat), max(p[1] for p in flat)] if flat else None
    try:
        cloud = float(granule["cloud_cover"])
    except (KeyError, ValueError, TypeError):
        cloud = None
    return {
        "id": granule.get("id"), "producer_granule_id": granule.get("producer_granule_id"),
        "source": source_id, "spectral_type": source["spectral_type"],
        "collection": granule.get("collection_concept_id"), "dataset_name": granule.get("dataset_id"),
        "acquired_at": granule.get("time_start"), "end_datetime": granule.get("time_end"),
        "cloud_cover": cloud, "bbox": bbox, "geometry": geometry,
        "native_resolution_m": source["native_resolution_m"],
        "band_count": source.get("band_count"), "assets": assets,
        "authentication_required_for_science_data": source["download_authentication_required"],
    }


async def _json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    try:
        # A fresh overseas connection can occasionally fail while the same
        # exact query succeeds immediately afterwards. Retry a transport failure
        # once, retain the existing maximum three attempts for 429/503, and keep
        # one 45-second budget covering I/O and backoff. TLS verification is not
        # modified; persistent provider HTTP 500 errors are never retried here.
        async with asyncio.timeout(45):
            network_retries = 0
            for attempt in range(3):
                try:
                    response = await client.get(url, params=params)
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError):
                    if network_retries or attempt == 2:
                        raise
                    network_retries += 1
                    await asyncio.sleep(0.5)
                    continue
                if response.status_code not in {429, 503} or attempt == 2:
                    break
                try:
                    delay = float(response.headers.get("Retry-After", 1 + attempt))
                except ValueError:
                    delay = 1 + attempt
                await asyncio.sleep(min(5.0, max(0.5, delay)))
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise _error(504, "spectral_provider_timeout", "光谱数据提供商请求超时，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        status = 404 if exc.response.status_code == 404 else 503 if exc.response.status_code == 429 else 502
        raise _error(status, "spectral_provider_error", f"光谱提供商返回 HTTP {exc.response.status_code}。") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise _error(502, "spectral_provider_unavailable", "无法读取提供商有效的 JSON 响应。") from exc


async def search_spectral(request: SpectralSearchRequest, client: httpx.AsyncClient | None = None) -> dict:
    if client is None:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False,
                                     trust_env=SOURCES[request.source].get('root') != PC_ROOT) as own:
            return await search_spectral(request, own)
    source = SOURCES[request.source]
    bbox = ",".join(str(v) for v in request.bbox)
    if source["catalogue"] == "stac":
        params = {"collections": source["collection"], "bbox": bbox, "datetime": request.datetime,
                  "limit": request.limit, "sortby": "-datetime"}
        if request.source == "enmap":
            params["f"] = "application/geo+json"
            if request.max_cloud_cover is not None:
                params.update({"filter": f"eo:cloud_cover <= {request.max_cloud_cover}", "filter-lang": "cql2-text"})
        elif request.max_cloud_cover is not None:
            params["query"] = json.dumps({"eo:cloud_cover": {"lte": request.max_cloud_cover}})
        raw = await _json(client, source["root"] + "/search", params)
        scenes = [normalize_stac_item(request.source, item) for item in raw.get("features", [])]
        for scene in scenes:
            _cache_scene(request.source, scene)
        more = any(link.get("rel") == "next" for link in raw.get("links", []))
    else:
        params = {"short_name": source["short_name"], "bounding_box": bbox,
                  "temporal": request.datetime.replace("/", ","), "page_size": request.limit,
                  "sort_key": "-start_date", "provider": "LPCLOUD", "version": source["version"]}
        if request.max_cloud_cover is not None:
            params["cloud_cover"] = f"0,{request.max_cloud_cover}"
        raw = await _json(client, CMR_SEARCH, params)
        scenes = [normalize_cmr_granule(request.source, item) for item in raw.get("feed", {}).get("entry", [])]
        more = len(scenes) == request.limit
    return {"source": _public_source(source), "query": request.model_dump(mode="json"), "count": len(scenes),
            "scenes": scenes, "more_available": more, "returned_limit": request.limit,
            "result_type": "scientific_scene_catalogue_and_assets", "files_downloaded": False,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "notes": ["时间为卫星实际拍摄时间；云量是整景元数据，不能保证 ROI 无云。", "只返回一页；扩大 limit 或缩小时间区间，不能据此声称枚举完整历史。", "下载地址和波段元数据与 RGB 底图不同。"]}


async def get_spectral_scene(source_id: SourceID, scene_id: str, client: httpx.AsyncClient) -> dict:
    if not ID_RE.fullmatch(scene_id):
        raise _error(422, "invalid_scene_id", "scene_id 含有不支持的字符。")
    source = SOURCES[source_id]
    if source["catalogue"] == "stac":
        cached = SCENE_CACHE.get((source_id, scene_id))
        if cached and time.monotonic() - cached[0] < SCENE_CACHE_TTL_SECONDS:
            SCENE_CACHE.move_to_end((source_id, scene_id))
            return cached[1]
        raw = await _json(client, f"{source['root']}/collections/{source['collection']}/items/{quote(scene_id, safe='')}")
        scene = normalize_stac_item(source_id, raw)
        _cache_scene(source_id, scene)
        return scene
    if not re.fullmatch(r"G\d+-LPCLOUD", scene_id):
        raise _error(422, "invalid_cmr_id", "NASA scene_id 应使用搜索返回的 G...-LPCLOUD 概念 ID。")
    raw = await _json(client, CMR_SEARCH, {"concept_id": scene_id, "short_name": source["short_name"], "page_size": 1})
    items = raw.get("feed", {}).get("entry", [])
    if not items:
        raise _error(404, "spectral_scene_not_found", "该图源未找到此场景。")
    return normalize_cmr_granule(source_id, items[0])


def _cache_scene(source_id, scene):
    if not isinstance(scene.get('id'), str):
        return
    SCENE_CACHE[(source_id, scene['id'])] = (time.monotonic(), scene)
    SCENE_CACHE.move_to_end((source_id, scene['id']))
    while len(SCENE_CACHE) > 64:
        SCENE_CACHE.popitem(last=False)


async def resolve_spectral_asset(source_id: SourceID, scene_id: str, asset_id: str, client: httpx.AsyncClient) -> dict:
    scene = await get_spectral_scene(source_id, scene_id, client)
    return await _resolve_asset_from_scene(source_id, scene_id, asset_id, scene, client)


async def _resolve_asset_from_scene(source_id: SourceID, scene_id: str, asset_id: str, scene: dict,
                                    client: httpx.AsyncClient) -> dict:
    asset = scene.get("assets", {}).get(asset_id)
    if not asset:
        raise _error(404, "spectral_asset_not_found", "该场景不存在此资产；请先查看搜索返回的 assets。")
    result = {"source": source_id, "scene_id": scene_id, "asset_id": asset_id, "acquired_at": scene.get("acquired_at"), **asset}
    if asset.get("requires_anonymous_signing"):
        # The official Python SDK obtains a container-scoped anonymous SAS token
        # and reuses it across band assets. Avoid one signing request per band.
        result.update(await sign_href(asset["href"], client))
    result["files_downloaded"] = False
    return result


def _read_subset(request: SpectralSubsetRequest, resolved: list[dict], scene: dict) -> tuple[bytes, dict]:
    # rasterio is imported lazily: catalogue discovery does not depend on GDAL.
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.io import MemoryFile
        from rasterio.transform import from_bounds
        from rasterio.vrt import WarpedVRT
        from rasterio.warp import transform_bounds
        from .range_reader import HttpRangeReader
    except ImportError as exc:
        raise _error(503, "rasterio_missing", "ROI 读取需要安装 rasterio；目录和下载地址接口仍可用。") from exc

    arrays, masks, band_meta = [], [], []
    target_crs, transform = None, None
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT=35,
                          GDAL_HTTP_MAX_RETRY=1, GDAL_HTTP_RETRY_DELAY=1,
                          CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF", VSI_CACHE=False):
            for asset in resolved:
                path, open_kwargs = asset["href"], {}
                if path.startswith("https://"):
                    # Windows GDAL/cURL transport can differ from the already
                    # verified httpx transport. A bounded Python range reader
                    # also prevents accidental full-scene downloads.
                    virtual_name = "spectral-" + uuid.uuid4().hex + ".tif"
                    signed_href = path
                    def opener(fp, mode="rb", _name=virtual_name, _href=signed_href):
                        if fp != _name:
                            raise FileNotFoundError("Sidecar access disabled")
                        return HttpRangeReader(_href)
                    path, open_kwargs = virtual_name, {"opener": opener}
                with rasterio.open(path, **open_kwargs) as src:
                    if src.count != 1 or src.crs is None:
                        raise _error(422, "unsupported_science_asset", "子集只接受有坐标系的单波段反射率资产。")
                    if target_crs is None:
                        target_crs = src.crs
                        bounds = transform_bounds("EPSG:4326", src.crs, *request.bbox, densify_pts=21)
                        transform = from_bounds(*bounds, request.width, request.height)
                    with WarpedVRT(src, crs=target_crs, transform=transform, width=request.width,
                                   height=request.height, resampling=Resampling.nearest) as vrt:
                        data = vrt.read(1, masked=True)
                        mask = np.ma.getmaskarray(data)
                        if bool(mask.all()):
                            raise _error(422, "no_valid_pixels", "此 ROI 在所选场景/波段内没有有效像元，请换场景或区域。")
                        arrays.append(data.astype("float32").filled(np.nan))
                        masks.append(~mask)
                    band_meta.append({"asset_id": asset["asset_id"], "bands": asset.get("bands", []),
                                      "raster_bands": asset.get("raster_bands", []),
                                      "native_pixel_size": list(src.res), "native_crs": str(src.crs),
                                      "dataset_scale": src.scales[0], "dataset_offset": src.offsets[0]})
            stack = np.stack(arrays)
            with MemoryFile() as mem:
                with mem.open(driver="GTiff", height=request.height, width=request.width, count=len(arrays),
                              dtype="float32", crs=target_crs, transform=transform,
                              nodata=float("nan"), compress="deflate") as dst:
                    dst.write(stack)
                    dst.descriptions = tuple(request.assets)
                    dst.update_tags(source=request.source, scene_id=request.scene_id,
                                    acquired_at=str(scene.get("acquired_at")), values="raw_DN_scale_not_applied",
                                    resampling="nearest")
                payload = mem.read()
    except HTTPException:
        raise
    except Exception as exc:
        # GDAL exceptions can contain signed SAS URLs; never reflect their text.
        raise _error(502, "spectral_raster_read_failed", "无法读取云端科学栅格；可能为网络故障或文件访问权限问题。") from exc
    metadata = {
        "source": request.source, "scene_id": request.scene_id, "spectral_type": SOURCES[request.source]["spectral_type"],
        "acquired_at": scene.get("acquired_at"), "cloud_cover_scene": scene.get("cloud_cover"),
        "bbox_wgs84": list(request.bbox), "width": request.width, "height": request.height,
        "band_count": len(arrays), "bands": band_meta, "crs": str(target_crs),
        "transform": list(transform)[:6], "georeferenced": True,
        "output_pixel_spacing_crs_units": [abs(transform.a), abs(transform.e)],
        "values": "raw_DN", "scale_and_offset_applied": False, "resampling": "nearest",
        "valid_pixel_fraction_per_band": [float(mask.mean()) for mask in masks],
        "native_resolution_m": SOURCES[request.source]["native_resolution_m"],
        "quality_caveats": ["输出网格间距由 bbox 与 width/height 决定，重采样不会提高原生空间分辨率。", "有效像元包含有云像元；实际分析应另读 SCL/QA 掩膜。", "未应用辐射缩放或偏移，计算反射率前必须读取原产品元数据。"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    from .spatial_metadata import scientific_spatial_metadata
    metadata.update(scientific_spatial_metadata(target_crs, transform, request.width,
                    request.height, kind='multispectral', resampling='nearest'))
    return payload, metadata


async def subset_spectral(request: SpectralSubsetRequest, client: httpx.AsyncClient | None = None) -> dict:
    if request.source not in {"sentinel2", "landsat"}:
        raise _error(409, "provider_authentication_required", "此提供商科学文件需要 Earthdata 或 EnMAP 账户；请使用 asset 接口获取官方访问地址。本机未配置该认证。")
    allowed = {"B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09", "B11", "B12", "B8A"} if request.source == "sentinel2" else {"coastal", "blue", "green", "red", "nir08", "swir16", "swir22"}
    if not set(request.assets) <= allowed:
        raise _error(422, "unsupported_reflectance_band", "子集仅接受该传感器已支持的单波段科学反射率资产。")
    if client is None:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False,
                                     trust_env=SOURCES[request.source].get('root') != PC_ROOT) as own:
            return await subset_spectral(request, own)
    scene = await get_spectral_scene(request.source, request.scene_id, client)
    resolved = await asyncio.gather(*(_resolve_asset_from_scene(request.source, request.scene_id, key, scene, client) for key in request.assets))
    async with SUBSET_SEMAPHORE:
        payload, metadata = await asyncio.to_thread(_read_subset, request, resolved, scene)
    SUBSET_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    path = SUBSET_DIR / (file_id + ".tif")
    temp = path.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(path)
    path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file_id": file_id, "file_url": f"/api/spectral/files/{file_id}.tif", "media_type": "image/tiff",
            "bytes": len(payload), "metadata": metadata}


@router.get("/sources")
async def sources_endpoint() -> dict:
    return {"sources": [_public_source(source) for source in SOURCES.values()],
            "distinction": "EMIT 与 EnMAP 是高光谱；Sentinel-2、Landsat 和 HLS 是多光谱。"}


@router.post("/search")
async def search_endpoint(request: SpectralSearchRequest) -> dict:
    return await search_spectral(request)


@router.get("/asset")
async def asset_endpoint(source: SourceID, scene_id: str = Query(min_length=1, max_length=220),
                         asset_id: str = Query(min_length=1, max_length=255)) -> dict:
    async with httpx.AsyncClient(timeout=45, follow_redirects=False,
                                 trust_env=SOURCES[source].get('root') != PC_ROOT) as client:
        return await resolve_spectral_asset(source, scene_id, asset_id, client)


@router.post("/subset")
async def subset_endpoint(request: SpectralSubsetRequest) -> dict:
    return await subset_spectral(request)


@router.get("/files/{filename}")
async def file_endpoint(filename: str) -> FileResponse:
    if not FILE_RE.fullmatch(filename):
        raise _error(404, "spectral_file_not_found", "文件不存在。")
    path = SUBSET_DIR / filename
    if path.is_symlink() or not path.is_file():
        raise _error(404, "spectral_file_not_found", "文件不存在。")
    return FileResponse(path, media_type="image/tiff", filename=filename, headers={"Cache-Control": "no-store"})
