"""Server-side AMap reverse geocoding and nearby facilities for a saved task point.

The point always comes from the owned task record, never from a model or a
browser. AMap distances are straight-line estimates between provider points;
an enterprise or park POI is its registered centre, not the stockpile itself.
"""
from __future__ import annotations

import asyncio
import json
import math
import re

import httpx

from .geocoding import AMAP_ORIGIN, MAX_RESPONSE_BYTES, GeocodingError, _forward_gcj02, _in_conversion_domain

AMAP_REGEO_PATH = "/v3/geocode/regeo"
AMAP_AROUND_PATH = "/v5/place/around"
DEFAULT_RADIUS_M = 2000
NOTICE = ("周边设施来自高德地图兴趣点，距离为坐标直线估算，不是实测；企业、园区和小区标注的是登记中心点，"
          "不等于堆存点位置。行政区和功能区仅供背景参考，不构成土地用途或保护区边界的法定认定。")

# Fixed category plan. Each group is one bounded AMap request, chosen by the
# server, never by the model. Codes follow AMap's POI classification table.
# Schools, healthcare and housing are separate so dense housing cannot crowd
# schools out of the ten nearest results.
CATEGORIES = (
    {"id": "school", "label": "学校与幼儿园", "types": "141200", "hint": "中小学、幼儿园、职业及成人教育"},
    {"id": "medical", "label": "医疗机构", "types": "090100|090200", "hint": "综合医院、专科医院、卫生院"},
    {"id": "residential", "label": "居民区", "types": "120300", "hint": "住宅小区与住宅区"},
    {"id": "water", "label": "河湖水体", "keywords": "河|湖|水库|湿地|渠|江", "hint": "河流、湖泊、水库、湿地与沟渠",
     "keep": r"(?:河|湖|水库|湿地|渠|江|溪|塘)(?:[（(].*)?$", "keep_types": ("190204", "190205", "110200"),
     "drop_types": ("1509", "1203", "06", "05", "07", "10")},
    {"id": "scenic", "label": "景区与公园", "types": "110000|110100|110200", "hint": "旅游景点、风景名胜、公园"},
    {"id": "industry", "label": "企业与园区", "types": "170300|170200|120100|170100", "hint": "工厂、公司、产业园区"},
    {"id": "waste_related", "label": "固废相关设施", "keywords": "废品回收|再生资源|垃圾|污水处理|砂石|建材|渣土|搅拌站|堆场",
     "hint": "废品回收、再生资源、垃圾和污水处理、砂石建材、渣土消纳"},
)
SENSITIVE = {"school", "medical", "residential", "water", "scenic"}
REQUEST_GAP_SECONDS = 0.35  # AMap Web services enforce a per-second limit (infocode 10021)
_ZONE_HINTS = (
    (r"自然保护区|湿地|森林公园|风景名胜区", "周边出现保护地或风景名胜区名称，边界需查官方图件"),
    (r"水源|水库", "周边出现水源或水库名称"),
    (r"景区|公园|旅游", "周边有公园或景点，不代表点位在旅游功能区内"),
    (r"工业园|产业园|开发区|工业区|工厂", "周边有工业或产业园区"),
    (r"合作社|种植|养殖|农场", "周边有农业生产主体"),
    (r"住宅|小区|社区|学校|医院", "周边以居住和公共服务设施为主"),
)


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else ""


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def task_point(task) -> tuple[float, float] | None:
    """WGS84 centre of the saved scope: explicit centre, else bbox midpoint."""
    record = task if isinstance(task, dict) else task.model_dump(mode="json")
    centre, bbox = record.get("center"), record.get("bbox")
    if isinstance(centre, (list, tuple)) and len(centre) == 2:
        lon, lat = centre
    elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    else:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lon, lat)):
        return None
    return (float(lon), float(lat)) if -180 <= lon <= 180 and -90 <= lat <= 90 else None


def bearing_label(origin: tuple[float, float], location) -> str | None:
    """Eight-point direction from the query point; both points are GCJ-02."""
    if not isinstance(location, str) or not re.fullmatch(r"-?\d{1,3}(?:\.\d+)?,-?\d{1,2}(?:\.\d+)?", location):
        return None
    lon, lat = map(float, location.split(","))
    east = (lon - origin[0]) * math.cos(math.radians(origin[1]))
    north = lat - origin[1]
    if east == 0 and north == 0:
        return None
    index = round(math.degrees(math.atan2(east, north)) / 45) % 8
    return ("北", "东北", "东", "东南", "南", "西南", "西", "西北")[index]


def _poi(row: dict, group: str, origin: tuple[float, float] | None = None) -> dict | None:
    name = _text(row.get("name"), 120)
    if not name:
        return None
    distance = _number(row.get("distance"))
    return {"name": name, "type": _text(row.get("type"), 120), "group": group,
            "distance_m": round(distance) if distance is not None else None,
            # /v5/place/around returns no direction; derive it from coordinates.
            "direction": _text(row.get("direction"), 10) or (bearing_label(origin, row.get("location")) if origin else None),
            "address": _text(row.get("address"), 160) or None,
            "district": _text(row.get("adname"), 40) or None}


def _kept(row: dict, category: dict) -> bool:
    """Keyword categories match names like "河滨鹭岛停车场"; keep real features only."""
    if "keep" not in category:
        return True
    code = str(row.get("typecode") or "")
    if any(code.startswith(prefix) for prefix in category.get("drop_types", ())):
        return False
    return code.startswith(category.get("keep_types", ())) or bool(re.search(category["keep"], _text(row.get("name"), 120)))


def zone_hints(admin: dict, aois: list[dict], groups: dict) -> list[str]:
    """Descriptive area characteristics from nearby names, never a legal zoning."""
    texts = [admin.get("township") or "", *(a.get("name", "") + a.get("type", "") for a in aois)]
    for rows in groups.values():
        texts.extend(f"{p['name']} {p['type']}" for p in rows if (p.get("distance_m") or 99999) <= 1000)
    joined = " ".join(texts)
    return [label for pattern, label in _ZONE_HINTS if re.search(pattern, joined)]


class PlaceContextClient:
    """One reverse geocode plus fixed nearby searches; redirects rejected."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 8, client: httpx.AsyncClient | None = None):
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 256 or any(ord(c) < 33 for c in api_key):
            raise GeocodingError("geocoding_not_configured", "管理员尚未配置高德 Web 服务密钥。")
        self._key, self.timeout_seconds = api_key, timeout_seconds
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        if path not in (AMAP_REGEO_PATH, AMAP_AROUND_PATH):
            raise GeocodingError("invalid_configuration", "地点服务接口无效。")
        try:
            async with self._client.stream("GET", AMAP_ORIGIN + path, params={**params, "key": self._key, "output": "json"},
                                           follow_redirects=False, timeout=self.timeout_seconds) as response:
                if response.status_code != 200:
                    raise GeocodingError("provider_unavailable", "地点服务暂不可用。")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise GeocodingError("provider_response_too_large", "地点服务响应超过大小限制。")
            data = json.loads(raw)
            if not isinstance(data, dict) or str(data.get("status")) != "1":
                if isinstance(data, dict) and str(data.get("infocode")) in ("10021", "10019", "10020", "10014"):
                    raise GeocodingError("provider_rate_limited", "地点服务调用频率受限。")
                raise GeocodingError("provider_rejected", "地点服务未完成查询。")
            return data
        except (httpx.TimeoutException, TimeoutError):
            raise GeocodingError("geocoding_timeout", "地点服务查询超时。") from None
        except httpx.HTTPError:
            raise GeocodingError("geocoding_unavailable", "无法连接地点服务。") from None
        except (ValueError, UnicodeError):
            raise GeocodingError("provider_invalid_response", "地点服务返回格式无效。") from None

    async def lookup(self, lon: float, lat: float, *, radius_m: int = DEFAULT_RADIUS_M) -> dict:
        if not _in_conversion_domain(lon, lat):
            return {"status": "outside_service_area", "notice": "该坐标不在高德国内服务范围，未查询周边设施。"}
        radius_m = max(200, min(int(radius_m), 5000))
        glon, glat = _forward_gcj02(lon, lat)
        location = f"{glon:.6f},{glat:.6f}"

        async def regeo():
            return await self._get(AMAP_REGEO_PATH, {"location": location, "extensions": "all", "radius": 1000, "roadlevel": 0})

        async def around(category):
            params = {"location": location, "radius": radius_m, "page_size": 15 if "keep" in category else 10,
                      "page_num": 1, "sortrule": "distance"}
            params.update({"types": category["types"]} if "types" in category else {"keywords": category["keywords"]})
            data = await self._get(AMAP_AROUND_PATH, params)
            rows = data.get("pois") if isinstance(data.get("pois"), list) else []
            return [p for p in (_poi(r, category["id"], (glon, glat)) for r in rows[:15] if isinstance(r, dict) and _kept(r, category)) if p][:10]

        # One request at a time with a short gap: AMap enforces a per-second
        # quota, and a burst would silently drop whole categories.
        jobs = [regeo, *(lambda c=c: around(c) for c in CATEGORIES)]
        replies = []
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds + 12
        for index, job in enumerate(jobs):
            if index:
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            if asyncio.get_running_loop().time() > deadline:
                replies.append(GeocodingError("geocoding_timeout", "地点服务查询超时。"))
                continue
            for attempt in range(2):
                try:
                    replies.append(await job())
                    break
                except GeocodingError as exc:
                    if exc.code == "provider_rate_limited" and attempt == 0:
                        await asyncio.sleep(1.1)
                        continue
                    replies.append(exc)
                    break
        errors = [r for r in replies if isinstance(r, BaseException)]
        if len(errors) == len(replies):
            first = errors[0]
            raise first if isinstance(first, GeocodingError) else GeocodingError("geocoding_unavailable", "地点服务暂不可用。")
        admin, aois, roads = {}, [], []
        if not isinstance(replies[0], BaseException):
            info = replies[0].get("regeocode") if isinstance(replies[0].get("regeocode"), dict) else {}
            component = info.get("addressComponent") if isinstance(info.get("addressComponent"), dict) else {}
            city = component.get("city")
            admin = {"formatted_address": _text(info.get("formatted_address"), 200) or None,
                     "province": _text(component.get("province"), 40) or None,
                     "city": _text(city, 40) or None,  # AMap returns [] for municipalities
                     "district": _text(component.get("district"), 40) or None,
                     "township": _text(component.get("township"), 60) or None,
                     "adcode": _text(component.get("adcode"), 12) or None}
            aois = [{"name": _text(a.get("name"), 120), "type": _text(a.get("type"), 40),
                     "distance_m": round(_number(a.get("distance")) or 0)}
                    for a in (info.get("aois") or [])[:5] if isinstance(a, dict) and a.get("name")]
            roads = [{"name": _text(r.get("name"), 80), "distance_m": round(_number(r.get("distance")) or 0),
                      "direction": _text(r.get("direction"), 10) or None}
                     for r in (info.get("roads") or [])[:3] if isinstance(r, dict) and r.get("name")]
        groups, failed = {}, []
        for category, reply in zip(CATEGORIES, replies[1:]):
            if isinstance(reply, BaseException):
                failed.append(category["id"])
                continue
            groups[category["id"]] = reply
        return {"status": "partial" if errors else "completed", "provider": "amap",
                "query_point_wgs84": [round(lon, 6), round(lat, 6)], "radius_m": radius_m,
                "coordinate_note": "按本地近似算法把 WGS84 转为高德坐标后查询，未做实测校正。",
                "administrative": admin, "aois": aois, "roads": roads,
                "categories": [{"id": c["id"], "label": c["label"], "hint": c["hint"],
                                "status": "failed" if c["id"] in failed else "completed",
                                "items": groups.get(c["id"], [])} for c in CATEGORIES],
                "zone_characteristics": zone_hints(admin, aois, groups),
                "notice": NOTICE, "content_trust": "provider_poi_data_not_field_verified"}


def model_view(context: dict) -> dict:
    """Compact projection for model prompts; keeps provenance and limits."""
    if not isinstance(context, dict) or context.get("status") not in ("completed", "partial"):
        return {"status": (context or {}).get("status", "not_available")}
    return {"status": context["status"], "administrative": context.get("administrative"),
            "aois": context.get("aois"), "roads": context.get("roads"), "radius_m": context.get("radius_m"),
            "nearby": {c["label"]: [f"{p['name']}（{p['type'].split(';')[-1] if p['type'] else '类型未知'}，约{p['distance_m']}米{('，' + p['direction']) if p.get('direction') else ''}）"
                                    for p in c["items"][:6]] for c in context.get("categories", []) if c.get("items")},
            "zone_characteristics": context.get("zone_characteristics"), "limits": NOTICE}
