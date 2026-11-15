"""Server-side AMap place candidates with explicit, approximate coordinate provenance.

The provider credential and endpoint selection never come from a model or a
browser. A place candidate is an area-of-interest hint, not a located waste site.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any

import httpx


AMAP_ORIGIN = "https://restapi.amap.com"
AMAP_GEOCODE_PATH = "/v3/geocode/geo"
AMAP_POI_PATH = "/v5/place/text"
MAX_RESPONSE_BYTES = 256 * 1024
LOCATION_NOTICE = "地名匹配仅用于选择检索区域中心，不代表疑似固废点的精确位置；请确认区域，必要时补充经纬度或在图上标点。"


class GeocodingError(RuntimeError):
    """Only credential-safe, application-owned messages reach the HTTP layer."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _text(value: Any, maximum: int = 300) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _input_text(value: str, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise GeocodingError("invalid_location_query", f"{field}需为不超过80字的单行文本。")
    value = value.strip()
    if not value and not allow_empty:
        raise GeocodingError("invalid_location_query", "请输入地名或详细地址。")
    return value


def _in_conversion_domain(lon: float, lat: float) -> bool:
    # A broad numerical domain, not a territorial polygon or accuracy warranty.
    return 72.004 < lon < 137.8347 and 0.8293 < lat < 55.8271


def _forward_gcj02(lon: float, lat: float) -> tuple[float, float]:
    x, y = lon - 105, lat - 35
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    dlat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    dlat += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    dlon += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlon += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    dlon += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    rad = lat * math.pi / 180
    magic = 1 - .00669342162296594323 * math.sin(rad)**2
    root = math.sqrt(magic)
    dlat = dlat*180/((6378245*(1-.00669342162296594323))/(magic*root)*math.pi)
    dlon = dlon*180/(6378245/root*math.cos(rad)*math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84_approx(lon: float, lat: float) -> dict:
    """Locally invert an approximate GCJ transform; never claim surveyed accuracy.

    AMap's documented conversion API converts *to* AMap coordinates. This local
    inverse is not an official AMap inverse service. Numerical round-trip error
    only measures this approximation's internal consistency.
    """
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lon, lat)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise GeocodingError("invalid_provider_location", "地点服务返回了无效坐标。")
    result = {"source_crs": "GCJ-02", "original_coordinates": [lon, lat],
              "target_crs": "EPSG:4326", "coordinates": None,
              "method": "local_iterative_gcj02_inverse_approximation",
              "official_inverse_service": False, "accuracy_m": None,
              "absolute_accuracy_verified": False, "roundtrip_residual_degrees": None}
    if not _in_conversion_domain(lon, lat):
        return {**result, "status": "unsupported_outside_conversion_domain"}
    wx, wy = lon, lat
    for _ in range(12):
        gx, gy = _forward_gcj02(wx, wy)
        dx, dy = gx - lon, gy - lat
        wx -= dx
        wy -= dy
        if max(abs(dx), abs(dy)) < 1e-9:
            break
    gx, gy = _forward_gcj02(wx, wy)
    residual = max(abs(gx-lon), abs(gy-lat))
    return {**result, "coordinates": [round(wx, 8), round(wy, 8)],
            "status": "approximate", "roundtrip_residual_degrees": residual}


def _candidate(row: dict, method: str) -> dict | None:
    location = row.get("location")
    if not isinstance(location, str) or not re.fullmatch(r"-?\d{1,3}(?:\.\d+)?,-?\d{1,2}(?:\.\d+)?", location):
        return None
    lon, lat = map(float, location.split(","))
    try:
        conversion = gcj02_to_wgs84_approx(lon, lat)
    except GeocodingError:
        return None
    address = _text(row.get("formatted_address") if method == "geocode" else row.get("address"))
    name = address if method == "geocode" else _text(row.get("name"))
    if not name:
        return None
    provider_id = _text(row.get("id"), 80)
    identity = json.dumps([name, location, provider_id], ensure_ascii=False)
    return {"id": hashlib.sha256(identity.encode()).hexdigest()[:24],
            "name": name, "address": address, "provider": "amap", "method": method,
            "provider_place_id": provider_id or None,
            "province": _text(row.get("province" if method == "geocode" else "pname")),
            "city": _text(row.get("city" if method == "geocode" else "cityname")),
            "district": _text(row.get("district" if method == "geocode" else "adname")),
            "adcode": _text(row.get("adcode"), 20),
            "match_level": _text(row.get("level"), 60) if method == "geocode" else "POI",
            "original_coordinates": [lon, lat], "original_crs": "GCJ-02",
            "center_wgs84": conversion["coordinates"], "coordinate_conversion": conversion,
            "requires_confirmation": True, "suspected_site_location_verified": False,
            "location_scope": "place_or_administrative_center_not_suspected_site",
            "notice": LOCATION_NOTICE}


class AmapGeocoder:
    """Two bounded official requests; redirects and arbitrary endpoint URLs rejected."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 8, client: httpx.AsyncClient | None = None):
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 256 or any(ord(c) < 33 for c in api_key):
            raise GeocodingError("geocoding_not_configured", "管理员尚未配置高德 Web 服务密钥。")
        if type(timeout_seconds) is not int or not 2 <= timeout_seconds <= 20:
            raise GeocodingError("invalid_configuration", "地点查询超时需为2至20秒的整数。")
        self._key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _request(self, path: str, params: dict) -> dict:
        if path not in (AMAP_GEOCODE_PATH, AMAP_POI_PATH):
            raise GeocodingError("invalid_configuration", "地点服务接口无效。")
        try:
            async with self._client.stream("GET", AMAP_ORIGIN + path,
                    params={**params, "key": self._key, "output": "json"},
                    follow_redirects=False, timeout=self.timeout_seconds) as response:
                if 300 <= response.status_code < 400:
                    raise GeocodingError("provider_redirect_rejected", "地点服务发生了不允许的跳转。")
                if response.status_code != 200:
                    raise GeocodingError("provider_unavailable", "地点服务暂不可用，请稍后重试。")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise GeocodingError("provider_response_too_large", "地点服务响应超过大小限制。")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
                if str(data.get("status")) != "1" or str(data.get("infocode", "10000")) != "10000":
                    code = str(data.get("infocode", ""))
                    if code in {"10001", "10005", "10006", "10007", "10008", "10009", "10012", "10013"}:
                        raise GeocodingError("provider_key_rejected", "地点服务密钥或访问权限未通过验证，请管理员检查 Web 服务配置。")
                    if code in {"10003", "10004", "10010", "10014", "10019", "10020", "10021", "10029", "10044"}:
                        raise GeocodingError("provider_quota_exceeded", "地点服务调用额度或频率受限，请稍后重试。")
                    raise GeocodingError("provider_rejected", "地点服务未完成查询，请检查地址或稍后重试。")
                return data
        except (httpx.TimeoutException, TimeoutError):
            raise GeocodingError("geocoding_timeout", "地点查询超时，请重试或直接输入经纬度。") from None
        except httpx.HTTPError:
            raise GeocodingError("geocoding_unavailable", "无法连接地点服务，请重试或直接输入经纬度。") from None
        except (ValueError, UnicodeError):
            raise GeocodingError("provider_invalid_response", "地点服务返回格式无效。") from None

    async def search(self, query: str, *, city: str = "", limit: int = 5) -> dict:
        query, city = _input_text(query, "地名"), _input_text(city, "城市", allow_empty=True)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise GeocodingError("invalid_location_query", "候选地点数量需为1至10。")
        geo = {"address": query, **({"city": city} if city else {})}
        poi = {"keywords": query, "page_size": limit, "page_num": 1,
               **({"region": city, "city_limit": "true"} if city else {})}

        async def request(method, path, params):
            try:
                data = await self._request(path, params)
                rows = data.get("geocodes" if method == "geocode" else "pois", [])
                if not isinstance(rows, list):
                    raise GeocodingError("provider_invalid_response", "地点服务返回格式无效。")
                return method, rows, None
            except GeocodingError as exc:
                return method, [], {"method": method, "code": exc.code, "message": str(exc)}

        try:
            async with asyncio.timeout(self.timeout_seconds):
                results = await asyncio.gather(request("geocode", AMAP_GEOCODE_PATH, geo), request("poi", AMAP_POI_PATH, poi))
        except TimeoutError:
            raise GeocodingError("geocoding_timeout", "地点查询超时，请重试或直接输入经纬度。") from None
        candidates, errors, seen = [], [], set()
        for method, rows, error in results:
            if error:
                errors.append(error)
            for row in rows[:25]:
                candidate = _candidate(row, method) if isinstance(row, dict) else None
                if candidate:
                    # Same provider point/name may appear in both API responses.
                    token = (candidate["name"], tuple(candidate["original_coordinates"]))
                    if token not in seen:
                        seen.add(token)
                        candidates.append(candidate)
        if len(errors) == 2:
            raise GeocodingError(errors[0]["code"], errors[0]["message"])
        return {"provider": "amap", "query": query, "city": city,
                "candidates": candidates[:limit], "requires_confirmation": True,
                "status": "candidates" if candidates else "no_matches", "errors": errors,
                "notice": LOCATION_NOTICE}


def confirm_location(search_result: dict, candidate_id: str) -> dict:
    """Select only from a server-owned response, retained per authenticated user.

    The HTTP adapter must load ``search_result`` from its own task/session store;
    it must never accept a browser-supplied candidate object as authoritative.
    """
    if not isinstance(candidate_id, str) or not re.fullmatch(r"[a-f0-9]{24}", candidate_id):
        raise GeocodingError("invalid_location_selection", "候选地点编号无效。")
    candidate = next((c for c in search_result.get("candidates", []) if c.get("id") == candidate_id), None)
    if not candidate or not candidate.get("center_wgs84"):
        raise GeocodingError("invalid_location_selection", "请选择本次查询返回且支持坐标转换的地点。")
    return {"center": list(candidate["center_wgs84"]), "coordinate_system": "wgs84",
            "geographic_basis": {**deepcopy(candidate), "requires_confirmation": False,
                "user_confirmed_search_area": True, "suspected_site_location_verified": False,
                "spatial_registration_verified": False}, "notice": LOCATION_NOTICE}
