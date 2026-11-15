"""Authenticated adapter for the OMAP PNG API, separate from science jobs.

An edition or a requested date never becomes an acquisition date. Credentials
stay in server configuration; downloaded bytes are handed to the existing store.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid
import weakref

import httpx
from PIL import Image, UnidentifiedImageError

from .data import DataClientError


# 不内置任何默认地址：免费隧道域名会失效，也不该写进产品代码。由管理员配置或 WUYU_UNIFIED_BASE_URL 提供。
DEFAULT_BASE_URL = ""
from .source_catalog import CENTURY_YEARS, EXCLUSIVE_SOURCES, valid_zoom  # noqa: E402,F401
CATALOG_SCHEMA = "2.0-declared-zoom"
SOURCE_PROFILES = {
    "gaofen2026": {"name": "2026 高清影像", "latest": True, "zoom": 20},
    "century": {"name": "世纪空间年度影像", "years": list(CENTURY_YEARS), "zoom": 18},
    "jilin2024": {"name": "吉林一号 2024 版", "years": [2024], "zoom": 18},
    "siwei2025": {"name": "四维地球 2025 版", "years": [2025], "zoom": 18},
    "tianditu": {"name": "天地图影像", "latest": True, "zoom": 18},
    "history246": {"name": "谷歌历史时序影像", "history": True, "zoom": 18},
    "bing": {"name": "Bing 影像", "remote_source": "bing337", "latest": True, "zoom": 18},
    "tencent": {"name": "腾讯影像", "remote_source": "tencent220", "latest": True, "zoom": 20},
    "amap": {"name": "高德影像", "remote_source": "amap221", "latest": True, "zoom": 20},
    "mapquest": {"name": "MapQuest 影像", "remote_source": "mapquest330", "latest": True, "zoom": 20},
    "mapbox": {"name": "Mapbox 影像", "remote_source": "mapbox329", "latest": True, "zoom": 20},
}
_REMOTE_ALIASES = {profile["remote_source"]: source for source, profile in SOURCE_PROFILES.items() if profile.get("remote_source")}
_IMAGE_PATH = re.compile(r"/api/unified/images/([0-9a-f]{32})\.png")
_LOOP_LIMITS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SCOPES = {"whole_scene", "full_scene"}


def _zoom(value):
    return valid_zoom(value)


def source_zoom_metadata(row: dict, profile: dict) -> dict:
    """A source's declared render cap is separate from native resolution.

    The API's generic parameter ceiling (22) is only a transport limit. It is
    never evidence that this provider publishes native imagery at that level.
    """
    nested=row.get('map_metadata') if isinstance(row.get('map_metadata'),dict) else {}
    def field(*names):
        for body in (row,nested):
            for name in names:
                if name in body:return body[name]
        return None
    maximum=_zoom(field('max_zoom','zoom_max'))
    declared=field('max_zoom_declared')
    basis=field('max_zoom_basis')
    if declared is False or (basis in {'transport_upper_bound','parameter_upper_bound','configured_default'} and declared is not True):
        maximum=None
    native=_zoom(field('native_max_zoom','max_native_zoom'))
    if field('native_max_zoom_declared') is False:native=None
    preferred=min(native,maximum) if native is not None and maximum is not None else native or maximum or profile['zoom']
    mapping={'max_zoom':maximum if maximum is not None else 22,'max_zoom_declared':maximum is not None,
             'max_zoom_basis':'provider_declared' if maximum is not None else 'transport_upper_bound',
             'native_max_zoom':native,'native_max_zoom_declared':native is not None,
             'native_max_zoom_basis':'provider_declared' if native is not None else 'unknown'}
    return {'map_metadata':mapping,'preferred_zoom':preferred,
            'preferred_zoom_basis':('declared_native_maximum' if native is not None else
                                    'declared_provider_cap_native_unknown' if maximum is not None else
                                    'configured_fallback_maximum_unknown')}


def _exact_date(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?", value):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def read_access_key(path: str | Path) -> str:
    """Read only the named key, without exposing configuration text in errors."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    for line in text.splitlines():
        if line.strip().startswith("API_KEY="):
            value = line.strip().partition("=")[2].strip().strip('"\'')
            if value and len(value) <= 8192 and not any(ord(c) < 32 for c in value):
                return value
    return ""


def unified_configuration(config: dict | None = None) -> dict:
    if config is not None:
        body = dict(config)
    else:
        key = os.getenv("WUYU_UNIFIED_API_KEY", "")
        access_file = os.getenv("WUYU_UNIFIED_ACCESS_FILE", "")
        if not key and access_file:
            key = read_access_key(access_file)
        body = {"base_url": os.getenv("WUYU_UNIFIED_BASE_URL", DEFAULT_BASE_URL), "api_key": key,
                "enabled": os.getenv("WUYU_UNIFIED_ENABLED", "1" if key else "0").lower() in {"1", "true"},
                "timeout_seconds": 120, "max_concurrency": 2}
    return body


def normalize_metadata(raw: dict, *, source: str, params: dict) -> dict:
    """Keep provider capture dates separate from all selection/edition fields."""
    actual_date = _exact_date(raw.get("actual_date"))
    values = raw.get("actual_dates", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    dates = sorted({value for item in values[:4096] if (value := _exact_date(item))})
    if actual_date and actual_date not in dates:
        dates.append(actual_date)
        dates.sort()
    scope = raw.get("metadata_scope", raw.get("acquisition_time_scope", raw.get("actual_date_scope", raw.get("scope", "unknown"))))
    scope = scope if isinstance(scope, str) and scope in {*_SCOPES, "point", "center_point", "tile", "displayed_view_date", "unknown"} else "unknown"
    if not actual_date and len(dates) == 1:
        actual_date = dates[0]
    method = raw.get("actual_date_method") if isinstance(raw.get("actual_date_method"), str) else None
    verified = len(dates) == 1 and scope in _SCOPES and method not in {"visible_date_ocr", "ocr", "edition_year"}
    safe_provider = {}
    for key in ("source", "source_name", "product", "edition", "year", "actual_date", "actual_dates",
                "metadata_scope", "acquisition_time_scope", "attribution", "copyright", "zoom",
                "width", "height", "crs", "coordinate_system", "date_selection", "date_requested",
                "actual_date_method", "actual_date_scope", "date_semantics", "transport", "projection",
                "native_coordinate_system", "coordinate_transform", "native_max_zoom", "config_group_year",
                "dataset_year", "requested_year"):
        value = raw.get(key)
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 2000:
            safe_provider[key] = value
        elif key == "actual_dates":
            safe_provider[key] = dates
    return {"provider": "omap_unified", "source": source, "product_year": params.get("year"),
            "requested_date": params.get("date"), "actual_date": actual_date, "actual_dates": dates,
            "acquisition_time": actual_date if verified else None,
            "acquisition_time_scope": scope, "metadata_scope": scope,
            "edition_is_capture_date": False, "native_resolution_m": None, "georeferenced": False,
            "raster_crs": None, "input_crs": "EPSG:4326", "center": [params["lon"], params["lat"]],
            "zoom": params["zoom"], "width": params["width"], "height": params["height"], "labels": False,
            "date_semantics": raw.get("date_semantics") if isinstance(raw.get("date_semantics"), str) else "unknown",
            "actual_date_method": method if isinstance(method, str) else None,
            "actual_date_scope": scope,
            "provider_metadata": safe_provider,
            "spatial_registration": {"state": "unverified", "strict_same_point_eligible": False,
                                     "reason": "PNG API supplies no validated image-to-reference registration"},
            "temporal_evidence": {"actual_capture_date_verified": verified,
                                  "date_scope": scope, "same_point_registration_verified": False,
                                  "edition_year_is_capture_date": False}}


class UnifiedImageryClient:
    def __init__(self, config: dict | None = None, *, client: httpx.AsyncClient | None = None,
                 max_download_bytes: int = 64 * 1024 * 1024):
        cfg = unified_configuration(config)
        self.enabled = cfg.get("enabled") is True
        self.base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base_url:
            # 尚未配置：保持"未启用"，而不是因空地址报错；目录/取图会明确返回"未配置"。
            self.enabled = False
            self.ready = False
            self.fingerprint = hashlib.sha256(b"unified-unconfigured").hexdigest()
            self._headers = {}
            self.timeout, self.concurrency, self.max_download_bytes = 120, 2, max_download_bytes
            self._http = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
            self._configured = False
            return
        self._configured = True
        try:
            parsed = urlsplit(self.base_url)
            invalid_port = parsed.port == 0
        except ValueError:
            raise DataClientError("invalid_configuration", "Unified imagery API requires a valid host and port.") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or invalid_port):
            raise DataClientError("invalid_configuration", "Unified imagery API requires an HTTP(S) origin without credentials.")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise DataClientError("invalid_configuration", "Public unified imagery API requires HTTPS.")
        key = cfg.get("api_key", "")
        if not isinstance(key, str) or any(ord(c) < 32 for c in key):
            raise DataClientError("invalid_configuration", "Unified imagery credential is invalid.")
        self.ready = bool(self.enabled and key)
        self.fingerprint = hashlib.sha256((self.base_url + "\0" + key + "\0" + str(self.enabled)).encode()).hexdigest()
        self._headers = {"X-API-Key": key, "ngrok-skip-browser-warning": "1"}
        self.timeout = cfg.get("timeout_seconds", 120)
        if not isinstance(self.timeout, (float, int)) or isinstance(self.timeout, bool) or not 5 <= self.timeout <= 120:
            raise DataClientError("invalid_configuration", "Unified imagery timeout must be between 5 and 120 seconds.")
        concurrency = cfg.get("max_concurrency", 2)
        if type(concurrency) is not int or concurrency not in {1, 2}:
            raise DataClientError("invalid_configuration", "Unified imagery concurrency may not exceed two.")
        self.concurrency = concurrency
        self.max_download_bytes = max_download_bytes
        self._http = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_http = client is None
        self._images: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._cached_bytes = 0
        self._known_sources = set(SOURCE_PROFILES)
        self._source_zoom = {}

    def adopt_catalog_sources(self, sources):
        """Hydrate only normalized, versioned private catalogue cache entries."""
        rows=[row for row in sources if isinstance(row,dict) and row.get('provider')=='omap_unified'
              and row.get('source') in SOURCE_PROFILES]
        self._known_sources={row['source'] for row in rows}
        self._source_zoom={row['source']:{key:row[key] for key in ('map_metadata','preferred_zoom','preferred_zoom_basis') if key in row}
                           for row in rows}

    async def aclose(self):
        self._images.clear()
        self._cached_bytes = 0
        if self._owns_http:
            await self._http.aclose()

    def _limits(self):
        loop = asyncio.get_running_loop()
        slots = _LOOP_LIMITS.setdefault(loop, {})
        # All configured users in one service process share this limit, even
        # when separate DataClient instances have been built for their tasks.
        return slots.setdefault(self.fingerprint, (asyncio.Semaphore(self.concurrency), asyncio.Lock()))

    def _safe_trace_id(self,value):
        return value if (isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value)
                         and self._headers['X-API-Key'] not in value) else None

    async def _get(self, path: str, params: dict | None = None, *, annual: bool = False):
        if not self.ready:
            raise DataClientError("unified_credentials_required", "新影像接口尚未配置服务端 API key。", 401)
        semaphore, annual_lock = self._limits()
        async def request():
            async with semaphore:
                try:
                    async with asyncio.timeout(self.timeout):
                        for attempt in range(3):
                            async with self._http.stream("GET", self.base_url + path, params=params,
                                    headers=self._headers, timeout=self.timeout, follow_redirects=False) as response:
                                if response.status_code == 429:
                                    if attempt == 2:
                                        raise DataClientError("unified_rate_limited", "影像接口繁忙，请按稍后的额度重试。", 429)
                                    retry = self._retry_after(response.headers.get("Retry-After", "1"))
                                else:
                                    if response.status_code in {401, 403}:
                                        raise DataClientError("unified_unauthorized", "影像接口 API key 或图源权限未通过。", response.status_code)
                                    if 300 <= response.status_code < 400:
                                        raise DataClientError("redirect_rejected", "影像接口重定向已拒绝。", response.status_code)
                                    if not response.is_success:
                                        code = "unified_api_rejected"
                                        upstream_code = None
                                        trace_id=self._safe_trace_id(response.headers.get('x-trace-id'))
                                        body = bytearray()
                                        async for chunk in response.aiter_bytes():
                                            body.extend(chunk)
                                            if len(body) > 8192:
                                                break
                                        if len(body) <= 8192:
                                            try:
                                                detail = json.loads(body).get("detail", {})
                                                candidate = detail.get("code") if isinstance(detail, dict) else None
                                                if isinstance(detail,dict):
                                                    trace_id=self._safe_trace_id(detail.get('trace_id')) or trace_id
                                                if type(candidate) is int:candidate=str(candidate)
                                                if (isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate)
                                                        and self._headers['X-API-Key'] not in candidate):
                                                    if path=='/image' and response.status_code>=500 and re.fullmatch(r'-?\d+',candidate):
                                                        code='unified_provider_render_failed'
                                                        upstream_code=candidate
                                                    else:code = candidate
                                            except (ValueError, AttributeError):
                                                pass
                                        message=(f'图源服务未完成出图（上游代码 {upstream_code}）；尚未取得影像，请核查图源服务状态。'
                                                 if upstream_code is not None else '影像接口未完成请求，请查看服务状态。')
                                        raise DataClientError(code,message,response.status_code,upstream_code=upstream_code,trace_id=trace_id)
                                    content = bytearray()
                                    async for chunk in response.aiter_bytes():
                                        content.extend(chunk)
                                        if len(content) > self.max_download_bytes:
                                            raise DataClientError("response_too_large", "影像接口响应超过大小限制。")
                                    return bytes(content), dict(response.headers)
                            await asyncio.sleep(retry)
                except (TimeoutError, httpx.TimeoutException):
                    raise DataClientError("unified_timeout", "影像接口未在 120 秒预算内完成。") from None
                except httpx.HTTPError:
                    raise DataClientError("unified_unavailable", "无法连接新影像接口，其他科学数据接口可独立使用。") from None
        if annual:
            async with annual_lock:
                return await request()
        return await request()

    @staticmethod
    def _retry_after(value: str) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                stamp = parsedate_to_datetime(value)
                seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 1
        # Never ignore an upstream long cooldown by retrying early.
        if not math.isfinite(seconds) or seconds > 120:
            raise DataClientError("unified_rate_limited", "影像接口要求较长等待，请稍后重试。", 429)
        return max(0, seconds)

    async def catalog(self) -> dict:
        content, _ = await self._get("/sources")
        try:
            raw = json.loads(content)
        except ValueError:
            raise DataClientError("invalid_catalog", "新影像接口未返回有效图源目录。") from None
        values = raw if isinstance(raw, list) else raw.get("sources") if isinstance(raw, dict) else None
        if isinstance(values, dict):
            values = [{"source": name, **(body if isinstance(body, dict) else {})} for name, body in values.items()]
        if not isinstance(values, list):
            raise DataClientError("invalid_catalog", "新影像接口图源目录不含来源列表。")
        sources = []
        for value in values:
            row = value if isinstance(value, dict) else {"source": value}
            name = row.get("source", row.get("id", row.get("key")))
            if isinstance(name, str):
                name = _REMOTE_ALIASES.get(name, name)
            if not isinstance(name, str) or name not in SOURCE_PROFILES:
                continue
            profile = SOURCE_PROFILES[name]
            zoom_metadata=source_zoom_metadata(row,profile)
            configured = (row.get("configured", row.get("enabled", row.get("available", True))) is not False
                          and row.get("status") not in {"upstream_notice", "unavailable", "disabled", "not_configured"})
            sources.append({"source": name, "name": profile["name"], "configured": configured,
                "configured_nodes": ["unified_http"] if configured else [], "provider": "omap_unified",
                **zoom_metadata,
                "supports_year": bool(profile.get("years")), "product_years": profile.get("years", []),
                "supports_date": bool(profile.get("history")), "supports_time_catalog": bool(profile.get("history")),
                "acquisition_time": None, "edition_is_capture_date": False, "coverage_verified": False,
                "spatial_registration_verified": False})
        self.adopt_catalog_sources(sources)
        return {"sources": sources, "provider": "omap_unified", "max_concurrency": 2,
                "catalog_schema":CATALOG_SCHEMA,
                "date_policy": "edition and requested dates never prove capture dates; use actual_date/actual_dates with scope"}

    def _parameters(self, payload: dict) -> dict:
        source = payload.get("source", "gaofen2026")
        if not isinstance(source, str) or source not in SOURCE_PROFILES:
            raise DataClientError("source_group_mismatch", "所选来源不属于新高分影像接口。")
        if source not in self._known_sources:
            raise DataClientError("unified_source_unpublished", "新影像接口未发布此图源。")
        bbox = payload.get("bbox")
        if bbox is not None:
            if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
                    any(type(v) not in {int, float} or not math.isfinite(v) for v in bbox)
                    or not -180 <= bbox[0] < bbox[2] <= 180 or not -85.05112878 <= bbox[1] < bbox[3] <= 85.05112878):
                raise DataClientError("invalid_parameters", "新影像接口需要有效 WGS84 范围。")
            lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        else:
            lon, lat = payload.get("lon"), payload.get("lat")
        if (type(lon) not in {int, float} or type(lat) not in {int, float}
                or not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -85.05112878 <= lat <= 85.05112878):
            raise DataClientError("invalid_parameters", "新影像接口需要经度在前的 WGS84 经纬度。")
        if payload.get("input_crs", "EPSG:4326") not in {"EPSG:4326", "WGS84"}:
            raise DataClientError("invalid_parameters", "新影像接口输入坐标必须是 WGS84。")
        zoom_info=self._source_zoom.get(source,source_zoom_metadata({},SOURCE_PROFILES[source]))
        params = {"source": source, "lon": lon, "lat": lat, "zoom": payload.get("zoom", zoom_info['preferred_zoom']),
                  "width": payload.get("width", 512), "height": payload.get("height", 512), "labels": False}
        for key, lower, upper in (("zoom", 1, 22), ("width", 64, 4096), ("height", 64, 4096)):
            if type(params[key]) is not int or not lower <= params[key] <= upper:
                raise DataClientError("invalid_parameters", "新影像接口的图幅或级别超出范围。")
        mapping=zoom_info.get('map_metadata',{})
        if mapping.get('max_zoom_declared') is True and params['zoom']>mapping['max_zoom']:
            raise DataClientError('unsupported_source_zoom','请求层级超过图源声明的服务上限；未擅自降低层级或改变请求范围。')
        year, requested_date = payload.get("year"), payload.get("date")
        if 'refresh' in payload:
            if type(payload['refresh']) is not bool:
                raise DataClientError('invalid_parameters','实时刷新参数必须是布尔值。')
            params['refresh']=payload['refresh']
        profile = SOURCE_PROFILES[source]
        if year is not None:
            if type(year) is not int or year not in profile.get("years", []):
                raise DataClientError("invalid_parameters", "该来源不支持所选年度版次。")
            params["year"] = year
        elif source in {"jilin2024", "siwei2025"}:
            params["year"] = profile["years"][0]
        if requested_date is not None:
            if source != "history246" or _exact_date(requested_date) != requested_date:
                raise DataClientError("invalid_parameters", "历史影像日期必须是 YYYY-MM-DD。")
            params["date"] = requested_date
        return params

    async def image(self, payload: dict) -> dict:
        params = self._parameters(payload)
        remote_source = SOURCE_PROFILES[params["source"]].get("remote_source", params["source"])
        content, headers = await self._get("/image", {**params, "source": remote_source}, annual="year" in params)
        media = headers.get("content-type", "").split(";", 1)[0].lower()
        if media not in {"image/png", "image/jpeg"}:
            raise DataClientError("invalid_preview", "影像接口成功响应不是 PNG/JPEG 图片。")
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                if image.size != (params["width"], params["height"]):
                    raise DataClientError("invalid_preview", "影像接口返回图幅与请求不一致。")
        except (UnidentifiedImageError, OSError, ValueError):
            raise DataClientError("invalid_preview", "影像接口返回的图片字节无效。") from None
        raw = {}
        try:
            candidate = json.loads(headers.get("x-imagery-metadata", "{}"))
            if isinstance(candidate, dict):
                raw = candidate
        except (ValueError, TypeError):
            pass
        if raw.get("source") not in {None, params["source"], remote_source}:
            raise DataClientError("source_response_mismatch", "影像接口响应来源与请求不同。")
        metadata = normalize_metadata(raw, source=params["source"], params=params)
        zoom_info=self._source_zoom.get(params['source'],source_zoom_metadata({},SOURCE_PROFILES[params['source']]))
        metadata['source_zoom_limits']=dict(zoom_info.get('map_metadata',{}))
        metadata['zoom_selection_basis']='user_requested_zoom' if 'zoom' in payload else zoom_info.get('preferred_zoom_basis')
        metadata['refresh_requested']=params.get('refresh',False)
        trace_id=self._safe_trace_id(raw.get('trace_id')) or self._safe_trace_id(headers.get('x-trace-id'))
        if trace_id:metadata['provider_trace_id']=trace_id
        metadata["remote_source"] = remote_source
        metadata["requested_bbox"] = list(payload["bbox"]) if payload.get("bbox") is not None else None
        metadata["requested_bbox_is_returned_footprint"] = False
        identifier = uuid.uuid4().hex
        while self._images and (len(self._images) >= 16 or self._cached_bytes + len(content) > self.max_download_bytes):
            _, (previous, _) = self._images.popitem(last=False)
            self._cached_bytes -= len(previous)
        self._images[identifier] = (content, media)
        self._cached_bytes += len(content)
        return {"image_url": "/api/unified/images/" + identifier + ".png", "metadata": metadata}

    async def times(self, payload: dict) -> dict:
        if payload.get("source", "history246") != "history246":
            raise DataClientError("invalid_parameters", "日期目录当前仅支持谷歌历史时序来源。")
        params = self._parameters({**payload, "source": "history246"})
        content, _ = await self._get("/times", {key: params[key] for key in ("source", "lon", "lat", "zoom")})
        try:
            raw = json.loads(content)
        except ValueError:
            raise DataClientError("invalid_response", "历史影像日期目录不是有效 JSON。") from None
        values = raw if isinstance(raw, list) else raw.get("dates", raw.get("times", [])) if isinstance(raw, dict) else []
        if not isinstance(values, list):
            raise DataClientError("invalid_response", "历史影像日期目录格式无效。")
        dates = set()
        for value in values[:4096]:
            candidate = value.get("date", value.get("actual_date")) if isinstance(value, dict) else value
            if exact := _exact_date(candidate):
                dates.add(exact)
        return {"source": "history246", "dates": sorted(dates), "count": len(dates),
                "scope": {key: params[key] for key in ("lon", "lat", "zoom")},
                "date_catalog_is_downloaded_evidence": False}

    def download(self, path: str) -> tuple[bytes, str]:
        match = _IMAGE_PATH.fullmatch(path) if isinstance(path, str) else None
        if not match or match[1] not in self._images:
            raise DataClientError("unified_artifact_missing", "影像缓存不属于当前取图会话，请重新取得所需影像。", 404)
        return self._images[match[1]]
