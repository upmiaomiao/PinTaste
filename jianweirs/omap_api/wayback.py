"""Bounded Esri World Imagery Wayback client using its public documented archive.

Archive dates are BASEMAP RELEASE dates. Point acquisition metadata is queried
separately and is never silently replaced with the release date. Services and
field mapping are documented in Esri/wayback-core (Apache-2.0); this Python
implementation is independent. Imagery remains subject to Esri's imagery terms.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import io
import json
import math
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

CONFIG_URL = "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
ATTRIBUTION = "Esri, Maxar, Earthstar Geographics, and the GIS User Community"
TERMS_URL = "https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_TILE_BYTES = 2 * 1024 * 1024
_TILE_SIZE = 256
_MAX_TILES = 64
_EARTH_RADIUS = 6378137.0


class WaybackError(RuntimeError):
    """Error safe to display through the local API (never includes remote URLs)."""

    def __init__(self, message: str, *, status_code: int = 502, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _day(value: str | date | None, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise WaybackError(f"{label} must use YYYY-MM-DD.", status_code=422) from None


def _service_url(url: Any, purpose: str) -> str:
    if not isinstance(url, str):
        raise WaybackError("The Wayback configuration contains an invalid service.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WaybackError("The Wayback configuration contains an invalid service URL.") from None
    allowed_hosts = ({"wayback.maptiles.arcgis.com", "wayback-a.maptiles.arcgis.com", "wayback-b.maptiles.arcgis.com"}
                     if purpose == "tile" else {"metadata.maptiles.arcgis.com"})
    if (parts.scheme != "https" or parts.hostname not in allowed_hosts or port not in (None, 443)
            or parts.username or parts.password or parts.query or parts.fragment):
        raise WaybackError("The Wayback configuration references an unapproved service.")
    if purpose == "tile":
        expression = r"/arcgis/rest/services/World_Imagery/WMTS/1\.0\.0/default028mm/MapServer/tile/\d+/\{level\}/\{row\}/\{col\}"
    else:
        expression = r"/arcgis/rest/services/World_Imagery_Metadata_\d{4}_r\d+/MapServer"
    if not re.fullmatch(expression, parts.path, flags=re.I):
        raise WaybackError("The Wayback configuration references an unsupported service path.")
    return url


def _global_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    scale = (1 << zoom) * _TILE_SIZE
    return ((lon + 180.0) / 360.0 * scale,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * scale)


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    return (_EARTH_RADIUS * math.radians(lon),
            _EARTH_RADIUS * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)))


def _validate_image_request(bbox: Any, width: int, height: int, zoom: int) -> list[float]:
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bbox)):
        raise WaybackError("bbox requires four finite coordinates.", status_code=422)
    west, south, east, north = map(float, bbox)
    if not (-180 <= west < east <= 180 and -85.05112878 <= south < north <= 85.05112878):
        raise WaybackError("bbox must be valid WGS84 within Web Mercator coverage and cannot cross the antimeridian.", status_code=422)
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (width, height, zoom))
            or not (64 <= width <= 2048 and 64 <= height <= 2048 and width * height <= 4_000_000)
            or not 0 <= zoom <= 23):
        raise WaybackError("Invalid image size or zoom; at most 4 million output pixels are allowed.", status_code=422)
    return [west, south, east, north]


class WaybackClient:
    def __init__(self, timeout: float = 45, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout = float(timeout)
        self.transport = transport
        self._items: dict[int, dict[str, Any]] = {}
        self._config_expires = 0.0
        self._config_lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport, follow_redirects=False,
                                 headers={"User-Agent": "OmapLocalImageryAPI/1.0"})

    async def _get(self, client: httpx.AsyncClient, url: str, *, maximum: int,
                   params: dict[str, Any] | None = None) -> bytes:
        for attempt in range(2):
            try:
                return await self._get_once(client, url, maximum=maximum, params=params)
            except WaybackError as exc:
                if not exc.retryable or attempt:
                    raise
                # The encompassing search/capture timeout includes this retry.
                await asyncio.sleep(0.3)

    async def _get_once(self, client: httpx.AsyncClient, url: str, *, maximum: int,
                        params: dict[str, Any] | None = None) -> bytes:
        try:
            # Wayback deduplicates identical tiles with same-host 301 redirects
            # to an earlier release. Follow at most three while preserving the
            # requested logical release in metadata. Do not follow arbitrary hosts.
            original_host = urlsplit(url).hostname
            for redirect in range(4):
                async with client.stream("GET", url, params=params) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        destination = urljoin(str(response.url), response.headers.get("location", ""))
                        parts = urlsplit(destination)
                        if (redirect == 3 or parts.scheme != "https" or parts.hostname != original_host
                                or parts.port not in (None, 443) or parts.username or parts.password
                                or parts.fragment or not response.headers.get("location")):
                            raise WaybackError("The Wayback service returned an unsafe or excessive redirect.")
                        url, params = destination, None
                        continue
                    if response.status_code in (401, 403):
                        raise WaybackError("The Wayback service denied access.", status_code=403)
                    if response.status_code == 404:
                        raise WaybackError("This Wayback image tile or metadata resource is unavailable.")
                    if response.status_code != 200:
                        raise WaybackError("The Wayback service returned an unsuccessful status.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > maximum:
                            raise WaybackError("The Wayback response exceeds the local size limit.")
                    return bytes(body)
        except httpx.TimeoutException:
            raise WaybackError("The Wayback service timed out.", status_code=504, retryable=True) from None
        except httpx.HTTPError:
            raise WaybackError("Cannot communicate with the Wayback service.", retryable=True) from None

    async def _config(self, client: httpx.AsyncClient) -> dict[int, dict[str, Any]]:
        if self._items and time.monotonic() < self._config_expires:
            return self._items
        async with self._config_lock:
            if self._items and time.monotonic() < self._config_expires:
                return self._items
            data = await self._get(client, CONFIG_URL, maximum=_MAX_JSON_BYTES)
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise WaybackError("The Wayback version catalog is not valid JSON.") from None
            if not isinstance(payload, dict) or len(payload) > 2000:
                raise WaybackError("The Wayback version catalog has an unexpected structure.")
            items = {}
            for identifier, raw in payload.items():
                if not str(identifier).isdigit() or not isinstance(raw, dict):
                    continue
                title = raw.get("itemTitle", "")
                match = re.search(r"Wayback (\d{4}-\d{2}-\d{2})", title) if isinstance(title, str) else None
                if not match:
                    continue
                try:
                    released = date.fromisoformat(match[1])
                except ValueError:
                    continue
                if released > datetime.now(timezone.utc).date():
                    continue  # Do not publish future-dated records as current imagery.
                item = {"release_id": int(identifier), "release_date": released.isoformat(),
                        "title": title[:150], "item_id": raw.get("itemID"), "acquisition_time": None,
                        "_tile_url": _service_url(raw.get("itemURL"), "tile"),
                        "_metadata_url": _service_url(raw.get("metadataLayerUrl"), "metadata")}
                tile_id = int(re.search(r"/tile/(\d+)/", item["_tile_url"])[1])
                if tile_id != item["release_id"]:
                    raise WaybackError("A Wayback catalog record has inconsistent release identifiers.")
                items[item["release_id"]] = item
            if not items:
                raise WaybackError("The Wayback version catalog contains no usable releases.")
            self._items, self._config_expires = items, time.monotonic() + 3600
            return self._items

    async def search(self, start_date: str | date | None = None, end_date: str | date | None = None,
                     limit: int = 50) -> dict[str, Any]:
        start, end = _day(start_date, "start_date"), _day(end_date, "end_date")
        if start and end and start > end:
            raise WaybackError("start_date must be on or before end_date.", status_code=422)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise WaybackError("limit must be an integer from 1 to 200.", status_code=422)
        try:
            async with asyncio.timeout(self.timeout), self._http() as client:
                items = await self._config(client)
        except TimeoutError:
            raise WaybackError("The Wayback search timed out.", status_code=504) from None
        versions = sorted((item for item in items.values()
                           if (not start or item["release_date"] >= start.isoformat())
                           and (not end or item["release_date"] <= end.isoformat())),
                          key=lambda item: item["release_date"], reverse=True)
        return {"provider": "esri_wayback", "versions": [{k: v for k, v in item.items() if not k.startswith("_")}
                                                         for item in versions[:limit]],
                "total": len(versions), "returned": min(len(versions), limit),
                "date_filter_basis": "release_date", "local_changes_filtered": False,
                "warnings": ["Release dates are map publication dates, not image acquisition dates.",
                             "Different releases can contain identical imagery at the requested location."]}

    async def _metadata(self, client: httpx.AsyncClient, item: dict, center: list[float], zoom: int) -> dict[str, Any] | None:
        url = item["_metadata_url"] + f"/{23 - max(10, zoom)}/query"
        raw = await self._get(client, url, maximum=_MAX_JSON_BYTES, params={
            "f": "json", "where": "1=1", "outFields": "SRC_DATE2,NICE_DESC,SRC_DESC,SAMP_RES,SRC_ACC",
            "geometry": json.dumps({"spatialReference": {"wkid": 4326}, "x": center[0], "y": center[1]}),
            "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        })
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise WaybackError("Wayback acquisition metadata has an unexpected structure.")
            if "error" in result:
                raise WaybackError("Wayback acquisition metadata query failed.")
            features = result.get("features", [])
            if not features:
                return None
            attributes = features[0]["attributes"]
            stamp = attributes.get("SRC_DATE2")
            acquired = (datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
                        if not isinstance(stamp, bool) and isinstance(stamp, (float, int))
                        and math.isfinite(stamp) and stamp >= 0 else None)
            def numeric(key):
                value = attributes.get(key)
                return value if not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and value > 0 else None
            def label(key):
                value = attributes.get(key)
                return value[:200] if isinstance(value, str) else None
            return {"acquisition_time": acquired, "provider": label("NICE_DESC"), "sensor_or_source": label("SRC_DESC"),
                    "native_resolution_m": numeric("SAMP_RES"), "reported_accuracy_m": numeric("SRC_ACC"),
                    "scope": "bbox_center_point_only", "query_point": center}
        except (ValueError, TypeError, KeyError, OverflowError, OSError, AttributeError):
            raise WaybackError("Wayback acquisition metadata has an unexpected structure.") from None

    async def capture(self, bbox: list[float], release_id: int, width: int = 1024, height: int = 1024,
                      zoom: int = 18, timeout_seconds: float = 45) -> dict[str, Any]:
        bounds = _validate_image_request(bbox, width, height, zoom)
        if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
            raise WaybackError("release_id must be a positive integer from the version search.", status_code=422)
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise WaybackError("timeout_seconds must be positive.", status_code=422)
        west, south, east, north = bounds
        left, top = _global_pixel(west, north, zoom)
        right, bottom = _global_pixel(east, south, zoom)
        first_col, first_row = int(left // _TILE_SIZE), int(top // _TILE_SIZE)
        last_col = math.ceil(right / _TILE_SIZE) - 1
        last_row = math.ceil(bottom / _TILE_SIZE) - 1
        count = (last_col - first_col + 1) * (last_row - first_row + 1)
        if count > _MAX_TILES:
            raise WaybackError("This area requires over 64 source tiles; reduce bbox or zoom.", status_code=422)
        center = [(west + east) / 2, (south + north) / 2]
        warnings = ["Release date is a map publication date; point metadata provides acquisition time separately.",
                    "RGB display imagery is not the original multispectral data.",
                    "Acquisition metadata describes the bbox center and may not describe every pixel."]
        budget = min(float(timeout_seconds), self.timeout)
        try:
            async with asyncio.timeout(budget), self._http() as client:
                items = await self._config(client)
                item = items.get(release_id)
                if item is None:
                    raise WaybackError("release_id is absent from the official Wayback catalog.", status_code=404)
                semaphore = asyncio.Semaphore(4)
                async def tile(row: int, col: int) -> tuple[int, int, Image.Image]:
                    async with semaphore:
                        url = item["_tile_url"].format(level=zoom, row=row, col=col)
                        blob = await self._get(client, url, maximum=_MAX_TILE_BYTES)
                        try:
                            with Image.open(io.BytesIO(blob)) as source:
                                if source.size != (_TILE_SIZE, _TILE_SIZE) or source.format not in {"PNG", "JPEG"}:
                                    raise WaybackError("A Wayback tile has an unexpected image format or size.")
                                source.load()
                                image = source.convert("RGB")
                        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                            raise WaybackError("A Wayback tile did not contain a decodable image.") from None
                        return row, col, image
                tiles = await asyncio.gather(*(tile(row, col) for row in range(first_row, last_row + 1)
                                               for col in range(first_col, last_col + 1)))
                mosaic = Image.new("RGB", ((last_col - first_col + 1) * _TILE_SIZE,
                                          (last_row - first_row + 1) * _TILE_SIZE))
                for row, col, image in tiles:
                    mosaic.paste(image, ((col - first_col) * _TILE_SIZE, (row - first_row) * _TILE_SIZE))
                out = mosaic.transform((width, height), Image.Transform.EXTENT,
                                       (left - first_col * _TILE_SIZE, top - first_row * _TILE_SIZE,
                                        right - first_col * _TILE_SIZE, bottom - first_row * _TILE_SIZE),
                                       resample=Image.Resampling.BILINEAR)
                buffer = io.BytesIO()
                out.save(buffer, format="PNG")
                point_metadata = None
                # Metadata is optional; leave time unknown if it fails and preserve
                # the already fetched image. This nested budget leaves the overall
                # capture deadline intact.
                try:
                    async with asyncio.timeout(min(8.0, budget / 3)):
                        point_metadata = await self._metadata(client, item, center, zoom)
                except (WaybackError, TimeoutError):
                    warnings.append("Center point acquisition metadata is currently unavailable.")
                xmin, ymin = _mercator(west, south)
                xmax, ymax = _mercator(east, north)
                metadata = {"provider": "esri_wayback", "release_id": release_id,
                            "release_date": item["release_date"], "release_date_semantics": "basemap_publication",
                            "acquisition_time": point_metadata.get("acquisition_time") if point_metadata else None,
                            "native_resolution_m": point_metadata.get("native_resolution_m") if point_metadata else None,
                            "point_acquisition_metadata": point_metadata,
                            "bbox": bounds, "bbox_crs": "EPSG:4326", "raster_crs": "EPSG:3857",
                            "raster_bounds": [xmin, ymin, xmax, ymax],
                            "affine_gdal": [xmin, (xmax - xmin) / width, 0, ymax, 0, -(ymax - ymin) / height],
                            "georeferencing_basis": "WMTS_Web_Mercator_grid_and_requested_extent",
                            "image_contains_embedded_georeferencing": False,
                            "width": width, "height": height, "zoom": zoom, "tiles_fetched": count,
                            "completeness": "all_requested_tiles_decoded", "local_changes_verified": False,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "attribution": ATTRIBUTION, "terms_url": TERMS_URL, "warnings": warnings}
                return {"image_bytes": buffer.getvalue(), "media_type": "image/png", "metadata": metadata}
        except TimeoutError:
            raise WaybackError("The Wayback image capture timed out.", status_code=504) from None
