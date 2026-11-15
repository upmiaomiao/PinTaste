"""Latest Esri World Imagery export with the server-returned geographic extent.

This is the currently published RGB basemap, not an on-demand satellite capture.
The export service may expand the requested bounds to preserve image aspect ratio;
georeferencing therefore uses the ACTUAL extent returned by Esri.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import math
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .wayback import TERMS_URL, WaybackError, _validate_image_request

SERVICE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"
EXPORT_URL = SERVICE_URL + "/export"
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_JSON_BYTES = 4 * 1024 * 1024
_ATTRIBUTION = "Esri and the World Imagery data contributors"


class EsriImageryError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _output_url(value: Any) -> str:
    if not isinstance(value, str):
        raise EsriImageryError("The Esri export did not return an image URL.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise EsriImageryError("The Esri export returned an invalid image URL.") from None
    if (parts.scheme != "https" or parts.hostname != "server.arcgisonline.com"
            or port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment
            or not parts.path.lower().startswith("/arcgis/rest/directories/arcgisoutput/world_imagery_mapserver/")
            or not parts.path.lower().endswith(".png")):
        raise EsriImageryError("The Esri export returned an unapproved image resource.")
    return value


def _actual_extent(result: dict[str, Any]) -> list[float]:
    extent = result.get("extent")
    if not isinstance(extent, dict):
        raise EsriImageryError("The Esri export did not return its actual map extent.")
    values = [extent.get(key) for key in ("xmin", "ymin", "xmax", "ymax")]
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)
            or any(abs(v) > 20037508.5 for v in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise EsriImageryError("The Esri export returned an invalid map extent.")
    sr = extent.get("spatialReference")
    if not isinstance(sr, dict) or not any(isinstance(sr.get(key), int) and sr.get(key) in {3857, 102100, 102113}
                                          for key in ("wkid", "latestWkid")):
        raise EsriImageryError("The Esri export returned an unexpected coordinate system.")
    return list(map(float, values))


class EsriImageryClient:
    def __init__(self, timeout: float = 45, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout, self.transport = float(timeout), transport
        self._service_info: dict[str, Any] | None = None
        self._service_info_expires = 0.0

    async def _fetch(self, client: httpx.AsyncClient, url: str, *, maximum: int,
                     params: dict[str, Any] | None = None) -> bytes:
        try:
            for redirect in range(4):
                async with client.stream("GET", url, params=params) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        destination = urljoin(str(response.url), response.headers.get("location", ""))
                        parts = urlsplit(destination)
                        if (redirect == 3 or parts.scheme != "https" or parts.hostname != "server.arcgisonline.com"
                                or parts.username or parts.password or parts.port not in (None, 443)
                                or parts.fragment or not parts.path.lower().startswith("/arcgis/rest/")
                                or not response.headers.get("location")):
                            raise EsriImageryError("The Esri service returned an unsafe or excessive redirect.")
                        url, params = destination, None
                        continue
                    if response.status_code in (401, 403):
                        raise EsriImageryError("The Esri service denied access.", status_code=403)
                    if response.status_code != 200:
                        raise EsriImageryError("The Esri service returned an unsuccessful response.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > maximum:
                            raise EsriImageryError("The Esri response exceeds the local size limit.")
                    return bytes(body)
        except httpx.TimeoutException:
            raise EsriImageryError("The Esri service timed out.", status_code=504) from None
        except httpx.HTTPError:
            raise EsriImageryError("Cannot communicate with the Esri service.") from None

    async def _point_metadata(self, client: httpx.AsyncClient, center: list[float], scale: Any) -> tuple[dict | None, str]:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise EsriImageryError("The export scale is unavailable for acquisition metadata selection.")
        if self._service_info is None or time.monotonic() > self._service_info_expires:
            raw = await self._fetch(client, SERVICE_URL, maximum=_MAX_JSON_BYTES, params={"f": "json"})
            try:
                info = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise EsriImageryError("The Esri service description is invalid.") from None
            if not isinstance(info, dict) or info.get("error") or not isinstance(info.get("layers"), list):
                raise EsriImageryError("The Esri service description is unavailable.")
            self._service_info, self._service_info_expires = info, time.monotonic() + 3600
        info = self._service_info
        attribution = info.get("copyrightText")
        attribution = attribution[:500] if isinstance(attribution, str) else _ATTRIBUTION
        layers = []
        for layer in info["layers"]:
            if not isinstance(layer, dict) or "metadata" not in str(layer.get("name", "")).lower():
                continue
            identifier, minimum, maximum = layer.get("id"), layer.get("minScale", 0), layer.get("maxScale", 0)
            if (isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 <= identifier <= 100
                    or not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float))):
                continue
            if (minimum == 0 or scale <= minimum) and (maximum == 0 or scale > maximum):
                layers.append(identifier)
        if not layers:
            return None, attribution
        raw = await self._fetch(client, SERVICE_URL + f"/{layers[0]}/query", maximum=_MAX_JSON_BYTES, params={
            "f": "json", "where": "1=1", "outFields": "SRC_DATE2,NICE_DESC,SRC_DESC,SAMP_RES,SRC_ACC",
            "geometry": json.dumps({"spatialReference": {"wkid": 4326}, "x": center[0], "y": center[1]}),
            "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        })
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("error"):
                raise EsriImageryError("The Esri acquisition metadata query failed.")
            features = result.get("features", [])
            if not features:
                return None, attribution
            attributes = features[0]["attributes"]
            stamp = attributes.get("SRC_DATE2")
            acquired = (datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
                        if not isinstance(stamp, bool) and isinstance(stamp, (int, float))
                        and math.isfinite(stamp) and stamp >= 0 else None)
            def numeric(key):
                value = attributes.get(key)
                return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None
            def label(key):
                value = attributes.get(key)
                return value[:200] if isinstance(value, str) else None
            return {"acquisition_time": acquired, "provider": label("NICE_DESC"), "sensor_or_source": label("SRC_DESC"),
                    "native_resolution_m": numeric("SAMP_RES"), "reported_accuracy_m": numeric("SRC_ACC"),
                    "scope": "bbox_center_point_only", "query_point": center, "metadata_layer_id": layers[0]}, attribution
        except (ValueError, TypeError, KeyError, OverflowError, OSError, AttributeError):
            raise EsriImageryError("The Esri acquisition metadata has an unexpected structure.") from None

    async def capture(self, bbox: list[float], width: int = 1024, height: int = 1024,
                      timeout_seconds: float = 45) -> dict[str, Any]:
        try:
            bounds = _validate_image_request(bbox, width, height, 0)
        except WaybackError as exc:
            raise EsriImageryError(str(exc), status_code=exc.status_code) from None
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise EsriImageryError("timeout_seconds must be positive.", status_code=422)
        budget = min(self.timeout, float(timeout_seconds))
        try:
            async with asyncio.timeout(budget), httpx.AsyncClient(
                timeout=budget, transport=self.transport, follow_redirects=False, trust_env=False,
                headers={"User-Agent": "OmapLocalImageryAPI/1.0"},
            ) as client:
                export_attempts=[]
                for attempt in range(4):
                    export_width=max(64,width//(2**attempt))
                    export_height=max(64,height//(2**attempt))
                    if export_attempts and [export_width,export_height]==export_attempts[-1]['requested_size']:
                        break
                    raw = await self._fetch(client, EXPORT_URL, maximum=_MAX_JSON_BYTES, params={
                        "f": "json", "bbox": ",".join(map(str, bounds)), "bboxSR": "4326", "imageSR": "3857",
                        "size": f"{export_width},{export_height}", "format": "png32", "transparent": "false",
                    })
                    try:
                        result = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise EsriImageryError("The Esri export response is not valid JSON.") from None
                    if not isinstance(result, dict) or result.get("error"):
                        raise EsriImageryError("The Esri export service could not create the image.")
                    # The public service can return HTTP200 with no error but
                    # zero dimensions/scale and an empty href below its locally
                    # available display scale. This is not an unsafe URL. Try a
                    # coarser output grid over the SAME requested geography.
                    empty=(result.get('href') in ('',None) and
                           result.get('width')==0 and result.get('height')==0 and result.get('scale')==0)
                    export_attempts.append({'requested_size':[export_width,export_height],
                                            'empty_export':empty})
                    if not empty:
                        break
                if export_attempts[-1]['empty_export']:
                    raise EsriImageryError("The Esri service has no exportable imagery at the attempted display scales. Try a larger area.")
                actual = _actual_extent(result)
                image_url = _output_url(result.get("href"))
                blob = await self._fetch(client, image_url, maximum=_MAX_IMAGE_BYTES)
                try:
                    with Image.open(io.BytesIO(blob)) as source:
                        actual_width, actual_height = source.size
                        if (source.format != "PNG" or not 1 <= actual_width <= 2048 or not 1 <= actual_height <= 2048
                                or actual_width * actual_height > 4_000_000
                                or result.get("width") != actual_width or result.get("height") != actual_height):
                            raise EsriImageryError("The Esri image dimensions or format do not match the export response.")
                        source.load()
                except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                    raise EsriImageryError("The Esri export did not contain a decodable PNG image.") from None
                xmin, ymin, xmax, ymax = actual
                def unproject(x, y):
                    return [math.degrees(x / 6378137), math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)]
                lower, upper = unproject(xmin, ymin), unproject(xmax, ymax)
                point_metadata, attribution = None, _ATTRIBUTION
                warnings = ["Current published basemap does not mean imagery was acquired today.",
                            "The actual extent may be expanded by the service to preserve image aspect ratio.",
                            "RGB display imagery is not the original multispectral data.",
                            "Acquisition metadata describes only the requested bbox center, not every pixel."]
                if len(export_attempts)>1:
                    warnings.append("The service returned empty exports at finer display scales; fewer output pixels were used over the same requested area. The image was not upsampled.")
                try:
                    async with asyncio.timeout(min(8.0, budget / 3)):
                        point_metadata, attribution = await self._point_metadata(
                            client, [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2], result.get("scale"))
                except (EsriImageryError, TimeoutError):
                    warnings.append("Center point acquisition metadata is currently unavailable.")
                return {"image_bytes": blob, "media_type": "image/png", "metadata": {
                    "provider": "esri_world_imagery", "source_mode": "current_published_basemap",
                    "bbox": bounds, "bbox_crs": "EPSG:4326", "actual_bbox_wgs84": [*lower, *upper],
                    "raster_crs": "EPSG:3857", "raster_bounds": actual,
                    "affine_gdal": [xmin, (xmax - xmin) / actual_width, 0, ymax, 0, -(ymax - ymin) / actual_height],
                    "georeferencing_basis": "server_export_actual_extent_and_pixel_dimensions",
                    "image_contains_embedded_georeferencing": False,
                    "acquisition_time": point_metadata.get("acquisition_time") if point_metadata else None,
                    "native_resolution_m": point_metadata.get("native_resolution_m") if point_metadata else None,
                    "point_acquisition_metadata": point_metadata,
                    "width": actual_width, "height": actual_height,
                    "requested_width": width, "requested_height": height,
                    "export_attempts":export_attempts,
                    "display_scale_fallback_used":len(export_attempts)>1,
                    "display_resampling":"none; actual provider image dimensions retained",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "attribution": attribution, "terms_url": TERMS_URL,
                    "quality_check": "server_export_and_png_decode", "coverage_verified": False,
                    "warnings": warnings}}
        except TimeoutError:
            raise EsriImageryError("The Esri image capture timed out.", status_code=504) from None
