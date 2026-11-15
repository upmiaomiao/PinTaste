"""Open SAR catalogs and real analysis-ready VV/VH GeoTIFF crops.

No Omap credentials, arbitrary URL proxying, or unbounded scene downloads.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse, urljoin

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .range_reader import HttpRangeReader
from .planetary_signing import sign_href

Source = Literal["sentinel1-rtc", "sentinel1-grd", "sentinel1-slc", "alos-palsar-rtc", "nisar-gcov", "jaxa-palsar-mosaic"]
ASF_ENDPOINT = "https://api.daac.asf.alaska.edu/services/search/param"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
OUTPUT_DIR = Path(os.getenv("OMAP_SAR_OUTPUT_DIR", str(Path(__file__).resolve().parent.parent / "outputs" / "sar")))
ITEM_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,239}$"
FILE_PATTERN = re.compile(r"^[a-f0-9]{32}\.tif$")
_CROP_LOCK = asyncio.Lock()



SOURCES = {
    "sentinel1-rtc": {"name": "Sentinel-1 RTC (Planetary Computer)", "provider": "Microsoft Planetary Computer", "band": "C", "product": "Radiometrically terrain corrected gamma0", "catalog_access": "anonymous", "data_access": "anonymous_temporary_sas", "query_enabled": True, "crop_enabled": True, "pixel_spacing_m": 10, "source_iw_grd_resolution_m": [20, 22], "analysis_units": "linear gamma0 power (float32)", "coverage": "Global archive; actual availability depends on date/location", "docs": "https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc"},
    "sentinel1-grd": {"name": "Sentinel-1 IW GRD high resolution", "provider": "ASF DAAC", "band": "C", "product": "GRD_HD", "catalog_access": "anonymous", "data_access": "earthdata_login_required", "query_enabled": True, "crop_enabled": False, "pixel_spacing_m": 10, "source_iw_grd_resolution_m": [20, 22], "analysis_units": "SAFE measurement requires calibration/terrain correction for analysis", "docs": "https://docs.asf.alaska.edu/api/keywords/"},
    "sentinel1-slc": {"name": "Sentinel-1 IW SLC", "provider": "ASF DAAC", "band": "C", "product": "SLC", "catalog_access": "anonymous", "data_access": "earthdata_login_required", "query_enabled": True, "crop_enabled": False, "pixel_spacing_m": None, "source_iw_slc_nominal_resolution_m": [5, 20], "analysis_units": "complex amplitude/phase; not calibrated intensity PNG", "docs": "https://docs.asf.alaska.edu/api/keywords/"},
    "alos-palsar-rtc": {"name": "ALOS PALSAR high-resolution RTC", "provider": "ASF DAAC", "band": "L", "product": "RTC_HI_RES", "catalog_access": "anonymous", "data_access": "earthdata_login_required", "query_enabled": True, "crop_enabled": False, "pixel_spacing_m": 12.5, "effective_resolution_m": None, "resolution_caveat": "12.5 m grid does not establish effective resolution or the resolution of the bundled DEM", "analysis_units": "gamma0 backscatter GeoTIFF (not a 12.5 m native DEM)", "docs": "https://docs.asf.alaska.edu/datasets/using_ASF_data/"},
    "nisar-gcov": {"name": "NISAR L2 GCOV", "provider": "ASF DAAC", "band": "L (ASF NASA products); S-band separate ISRO archive", "product": "GCOV", "catalog_access": "anonymous", "data_access": "earthdata_login_required", "query_enabled": True, "crop_enabled": False, "pixel_spacing_m": None, "effective_resolution_m": None, "resolution_caveat": "Read grid and band configuration from each HDF5 product; provisional quality may apply", "analysis_units": "geocoded covariance HDF5; not Sentinel-1 VV/VH", "docs": "https://nisar-docs.asf.alaska.edu/asf-search/"},
    "jaxa-palsar-mosaic": {"name": "JAXA annual PALSAR/PALSAR-2 mosaics", "provider": "JAXA EORC", "band": "L", "product": "annual gamma0 mosaic", "catalog_access": "registration_required", "data_access": "jaxa_registered_download_required", "query_enabled": False, "crop_enabled": False, "pixel_spacing_arc_seconds": 0.8, "pixel_spacing_m_at_equator": 25, "status": "not_configured", "reason": "Official download requires email registration; no credential-free scene API configured", "docs": "https://www.eorc.jaxa.jp/ALOS/en/dataset/fnf_e.htm"},
}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _validate_bbox(value):
    west, south, east, north = value
    if not all(math.isfinite(x) for x in value):
        raise ValueError("bbox coordinates must be finite")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be [west,south,east,north] in WGS84 without crossing the antimeridian")
    if east - west > 20 or north - south > 20:
        raise ValueError("Search each region separately; bbox span cannot exceed 20 degrees")
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SarSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Source = "sentinel1-rtc"
    bbox: tuple[float, float, float, float]
    input_crs: Literal['EPSG:4326'] = 'EPSG:4326'
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=10, ge=1, le=50)
    polarization: Literal["VV", "VH", "VV+VH", "HH", "HV", "HH+HV"] | None = None
    flight_direction: Literal["ASCENDING", "DESCENDING"] | None = None
    relative_orbit: int | None = Field(default=None, ge=1, le=671)

    _bbox = field_validator("bbox")(_validate_bbox)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start and self.end and _utc(self.start) > _utc(self.end):
            raise ValueError("start must not be later than end")
        return self


class SarAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Source = "sentinel1-rtc"
    item_id: str = Field(pattern=ITEM_PATTERN)
    sign: bool = False


class SarCropRequest(SarAssetRequest):
    bbox: tuple[float, float, float, float]
    input_crs: Literal['EPSG:4326'] = 'EPSG:4326'
    polarizations: list[Literal["vv", "vh"]] = Field(default_factory=lambda: ["vv", "vh"], min_length=1, max_length=2)
    _bbox = field_validator("bbox")(_validate_bbox)

    @model_validator(mode="after")
    def small_roi(self):
        if self.source != "sentinel1-rtc":
            raise ValueError("Raw ROI crop currently supports sentinel1-rtc only")
        if len(set(self.polarizations)) != len(self.polarizations):
            raise ValueError("polarizations must be unique")
        if self.bbox[2] - self.bbox[0] > 0.1 or self.bbox[3] - self.bbox[1] > 0.1:
            raise ValueError("ROI span cannot exceed 0.1 degree in either direction; split larger regions")
        return self


async def _request_json(method: str, url: str, **kwargs) -> dict:
    try:
        # The public PC catalog and anonymous Blob path have been verified
        # directly. Windows system proxies intermittently reset PC item lookups;
        # retain the configured transport policy for the independent ASF host.
        planetary = urlparse(url).hostname == 'planetarycomputer.microsoft.com'
        async with asyncio.timeout(60), httpx.AsyncClient(timeout=45, follow_redirects=False,
                                                          trust_env=not planetary) as client:
            for attempt in range(3 if planetary else 1):
                try:
                    response = await client.request(method, url, **kwargs)
                except httpx.TransportError:
                    if not planetary or attempt == 2:
                        raise
                    await asyncio.sleep(.5 * (attempt + 1))
                    continue
                if not planetary or response.status_code not in {429, 502, 503, 504} or attempt == 2:
                    break
                try:
                    delay = float(response.headers.get('Retry-After', .5 * (attempt + 1)))
                except ValueError:
                    delay = .5 * (attempt + 1)
                await asyncio.sleep(min(5., max(.25, delay)))
        if response.status_code == 404:
            raise _error(404, "sar_item_not_found", "SAR scene is not present in the selected catalog")
        if response.status_code == 429:
            raise _error(503, "sar_provider_throttled", "Public SAR provider is throttling; retry later")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid catalog")
        return result
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise _error(502, "sar_provider_unavailable", "SAR provider returned an error or unusable catalog response") from None


def _safe_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    return host in {"planetarycomputer.microsoft.com", "datapool.asf.alaska.edu", "data.asf.alaska.edu", "nisar.asf.earthdatacloud.nasa.gov", "d1mv8zhcvry6x4.cloudfront.net"} or host.endswith(".blob.core.windows.net")


def _asset(href, media_type=None, **extra):
    if not isinstance(href, str) or not _safe_url(href):
        return None
    return {"href": href, "media_type": media_type, **extra}


def _normalize_pc(feature: dict) -> dict:
    p = feature.get("properties", {})
    assets = {}
    for name, raw in feature.get("assets", {}).items():
        entry = _asset(raw.get("href"), raw.get("type"), roles=raw.get("roles", []), size_bytes=raw.get("file:size"), requires_earthdata_login=False, signed=False)
        if entry:
            entry["raster_bands"] = raw.get("raster:bands", [])
            assets[name] = entry
    return {"id": feature["id"], "source": "sentinel1-rtc", "platform": p.get("platform"), "acquired_at": p.get("datetime"), "bbox": feature.get("bbox"), "geometry": feature.get("geometry"), "polarizations": p.get("sar:polarizations", []), "band": p.get("sar:frequency_band"), "product": "RTC", "source_product": p.get("sar:product_type"), "instrument_mode": p.get("sar:instrument_mode"), "orbit_direction": p.get("sat:orbit_state"), "relative_orbit": p.get("sat:relative_orbit"), "pixel_spacing_m": [p.get("sar:pixel_spacing_range"), p.get("sar:pixel_spacing_azimuth")], "source_resolution_m": [p.get("sar:resolution_range"), p.get("sar:resolution_azimuth")], "crs_epsg": p.get("proj:epsg"), "transform": p.get("proj:transform"), "assets": assets, "data_downloaded": False, "analysis_ready": True, "preview_is_analysis_data": False, "preview_api": "/api/sar/preview?" + urlencode({"source": "sentinel1-rtc", "item_id": feature["id"]})}


def _normalize_asf(feature: dict, source: str) -> dict:
    p = feature.get("properties", {})
    assets = {}
    byte_count = p.get("bytes")
    if isinstance(byte_count, dict):
        byte_count = byte_count.get(p.get("fileName"), {}).get("bytes")
    data = _asset(p.get("url"), "application/x-hdf5" if source == "nisar-gcov" else "application/zip", roles=["data"], size_bytes=byte_count, requires_earthdata_login=True)
    if data:
        assets["data"] = data
    browse = p.get("browse") or []
    if isinstance(browse, str):
        browse = [browse]
    for n, url in enumerate(browse):
        entry = _asset(url, "image/png" if url.endswith(".png") else "image/jpeg", roles=["overview"], requires_earthdata_login=False)
        if entry:
            assets[f"browse{n}"] = entry
    source_info = SOURCES[source]
    spacing = source_info.get("pixel_spacing_m")
    resolution = None
    if p.get("beamModeType") == "IW":
        resolution = source_info.get("source_iw_grd_resolution_m") or source_info.get("source_iw_slc_nominal_resolution_m")
    polarization = p.get("polarization") or p.get("mainBandPolarization") or ""
    polarizations = polarization.split("+") if isinstance(polarization, str) and polarization else polarization if isinstance(polarization, list) else []
    return {"id": p.get("fileID", p.get("sceneName")), "source": source, "platform": p.get("platform"), "acquired_at": p.get("startTime"), "acquisition_end": p.get("stopTime"), "geometry": feature.get("geometry"), "polarizations": polarizations, "band": source_info["band"], "product": p.get("processingLevel"), "instrument_mode": p.get("beamModeType"), "orbit_direction": p.get("flightDirection"), "relative_orbit": p.get("pathNumber"), "pixel_spacing_m": spacing, "source_resolution_m": resolution, "assets": assets, "data_downloaded": False, "analysis_ready": source in ("alos-palsar-rtc", "nisar-gcov"), "processing_collection": p.get("collectionName"), "quality_status": "provisional" if "PROVISIONAL" in str(p.get("collectionName", "")).upper() else "provider_product", "preview_is_analysis_data": False, "preview_api": "/api/sar/preview?" + urlencode({"source": source, "item_id": p.get("fileID", p.get("sceneName"))})}


def _require_query(source):
    if not SOURCES[source]["query_enabled"]:
        raise _error(409, "sar_source_not_configured", SOURCES[source]["reason"])


async def search_catalog(body: SarSearchRequest) -> dict:
    _require_query(body.source)
    if body.source == "sentinel1-rtc":
        query = {}
        if body.flight_direction:
            query["sat:orbit_state"] = {"eq": body.flight_direction.lower()}
        if body.relative_orbit:
            query["sat:relative_orbit"] = {"eq": body.relative_orbit}
        # RTC source has dual polarizations; do not silently invent a single-pol query.
        if body.polarization and body.polarization != "VV+VH":
            raise _error(422, "sar_filter_unsupported", "RTC catalog search supports VV+VH; select vv/vh separately in /crop")
        end = _utc(body.end) if body.end else datetime.now(timezone.utc)
        start = _utc(body.start) if body.start else end - timedelta(days=90)
        payload = {"collections": ["sentinel-1-rtc"], "bbox": body.bbox, "datetime": f"{start.isoformat()}/{end.isoformat()}", "limit": body.limit}
        if query:
            payload["query"] = query
        raw = await _request_json("POST", PC_STAC + "/search", json=payload)
        items = [_normalize_pc(item) for item in raw.get("features", [])]
        more = any(link.get("rel") == "next" for link in raw.get("links", []))
    else:
        dataset, level = {"sentinel1-grd": ("SENTINEL-1", "GRD_HD"), "sentinel1-slc": ("SENTINEL-1", "SLC"), "alos-palsar-rtc": ("ALOS PALSAR", "RTC_HI_RES"), "nisar-gcov": ("NISAR", "GCOV")}[body.source]
        w, s, e, n = body.bbox
        params = {"dataset": dataset, "processingLevel": level, "intersectsWith": f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))", "maxResults": body.limit, "output": "geojson"}
        if body.source.startswith("sentinel1"):
            params["beamMode"] = "IW"
        for name, value in (("start", body.start), ("end", body.end)):
            if value:
                params[name] = _utc(value).isoformat()
        if body.polarization:
            params["mainBandPolarization" if body.source == "nisar-gcov" else "polarization"] = body.polarization
        if body.flight_direction:
            params["flightDirection"] = body.flight_direction
        if body.relative_orbit:
            params["relativeOrbit"] = body.relative_orbit
        raw = await _request_json("GET", ASF_ENDPOINT, params=params)
        items = [_normalize_asf(item, body.source) for item in raw.get("features", [])]
        more = len(items) >= body.limit
    return {"source": body.source, "query": body.model_dump(mode="json"), "coordinate_system": "EPSG:4326", "returned": len(items), "results_limited": more, "items": items, "data_downloaded": False, "retrieved_at": datetime.now(timezone.utc).isoformat(), "note": "Scene footprints intersect the search region; catalog result is not a downloaded raster or proof of full usable ROI coverage"}


async def _lookup(source: Source, item_id: str) -> dict:
    _require_query(source)
    if not re.fullmatch(ITEM_PATTERN, item_id):
        raise _error(422, "sar_invalid_item_id", "Invalid scene ID")
    if source == "sentinel1-rtc":
        return _normalize_pc(await _request_json("GET", PC_STAC + "/collections/sentinel-1-rtc/items/" + item_id))
    result = await _request_json("GET", ASF_ENDPOINT, params={"product_list": item_id, "output": "geojson"})
    features = result.get("features", [])
    if not features:
        raise _error(404, "sar_item_not_found", "SAR scene is not present in the selected catalog")
    item = _normalize_asf(features[0], source)
    expected = {"sentinel1-grd": "GRD_HD", "sentinel1-slc": "SLC", "alos-palsar-rtc": "RTC_HI_RES", "nisar-gcov": "GCOV"}[source]
    if item["product"] != expected:
        raise _error(422, "sar_source_mismatch", "Scene does not belong to the requested product type")
    return item


async def _signed_href(href: str) -> str:
    if not _safe_url(href) or not (urlparse(href).hostname or "").endswith(".blob.core.windows.net"):
        raise _error(502, "sar_invalid_asset", "Catalog returned an unsupported asset location")
    result = await sign_href(href)
    signed = result.get("href")
    if not isinstance(signed, str) or not _safe_url(signed) or urlparse(signed).hostname != urlparse(href).hostname:
        raise _error(502, "sar_signing_failed", "Public data signing service returned an invalid asset")
    return signed


async def _read_preview(url: str) -> bytes:
    if not _safe_url(url):
        raise _error(502, "sar_invalid_asset", "Unsupported preview location")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for attempt in range(4):
                async with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        destination = urljoin(url, response.headers.get("location", ""))
                        if not _safe_url(destination):
                            raise _error(409, "sar_preview_auth_required", "Preview redirects outside configured public hosts; use its official asset URL or authenticated provider")
                        url = destination
                        continue
                    if response.status_code in (401, 403):
                        raise _error(409, "sar_preview_auth_required", "Provider requires authentication for this preview; use its official asset URL")
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 8 * 1024 * 1024:
                            raise _error(413, "sar_preview_too_large", "Preview exceeds the 8 MiB limit")
                    return bytes(content)
            raise _error(502, "sar_preview_redirect_limit", "Preview provider redirects exceeded the configured limit")
    except httpx.HTTPError:
        raise _error(502, "sar_preview_unavailable", "SAR preview provider is unavailable") from None


def _write_crop(body: SarCropRequest, item: dict, signed_assets: dict, output_dir: Path = OUTPUT_DIR) -> dict:
    try:
        import numpy as np
        import rasterio
        from rasterio.warp import transform_bounds
        from rasterio.windows import Window, from_bounds
    except ImportError:
        raise _error(503, "sar_rasterio_missing", "Install rasterio>=1.4,<2 to enable raw SAR ROI crops") from None
    bands, template = [], None
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="30", GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_MAX_RETRY="1", VSI_CACHE=False):
            for name in body.polarizations:
                href = signed_assets[name]
                virtual_name = "sar-" + uuid.uuid4().hex + ".tif"
                def opener(path, mode="rb"):
                    if path != virtual_name:
                        raise FileNotFoundError("Sidecar access disabled")
                    return HttpRangeReader(href)
                open_args = {"opener": opener} if href.startswith("https://") else {}
                with rasterio.open(virtual_name if open_args else href, **open_args) as ds:
                    requested = transform_bounds("EPSG:4326", ds.crs, *body.bbox, densify_pts=21)
                    window = from_bounds(*requested, transform=ds.transform).round_offsets().round_lengths()
                    try:
                        window = window.intersection(Window(0, 0, ds.width, ds.height))
                    except Exception:
                        raise _error(422, "sar_roi_outside_scene", "ROI does not intersect scene raster") from None
                    if window.width < 1 or window.height < 1 or window.width * window.height > 4_000_000:
                        raise _error(422, "sar_roi_size", "ROI exceeds four million native pixels or is empty")
                    band = ds.read(1, window=window, masked=True)
                    if band.count() == 0:
                        raise _error(422, "sar_roi_no_data", "ROI has no valid backscatter pixels in this scene")
                    profile = {"driver": "GTiff", "height": band.shape[0], "width": band.shape[1], "count": len(body.polarizations), "dtype": "float32", "crs": ds.crs, "transform": ds.window_transform(window), "nodata": float(ds.nodata) if ds.nodata is not None else -32768.0, "compress": "deflate"}
                    if template and (profile["transform"] != template["transform"] or band.shape != bands[0].shape or profile["crs"] != template["crs"]):
                        raise _error(502, "sar_band_grid_mismatch", "Polarization grids differ; explicit alignment is required")
                    template = profile
                    bands.append(band)
        output_dir.mkdir(parents=True, exist_ok=True)
        file_id = uuid.uuid4().hex
        final_path = output_dir / (file_id + ".tif")
        tmp_path = output_dir / (file_id + ".tmp")
        try:
            with rasterio.open(tmp_path, "w", **template) as target:
                for index, (name, band) in enumerate(zip(body.polarizations, bands), start=1):
                    target.write(band.filled(template["nodata"]).astype("float32"), index)
                    target.set_band_description(index, name.upper())
                    target.update_tags(index, units="linear gamma0 power", polarization=name.upper())
                target.update_tags(source="Microsoft Planetary Computer Sentinel-1 RTC", item_id=item["id"], acquisition_time=item["acquired_at"], radiometry="gamma0 linear power; no dB conversion applied")
            tmp_path.replace(final_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        valid_counts = [int(band.count()) for band in bands]
        from .spatial_metadata import scientific_spatial_metadata
        spatial = scientific_spatial_metadata(template["crs"], template["transform"], template["width"], template["height"], kind="sar", resampling="none_native_window")
        return {**spatial, "file_id": file_id, "file_url": f"/api/sar/files/{file_id}.tif", "media_type": "image/tiff; application=geotiff", "source": body.source, "item_id": item["id"], "acquired_at": item["acquired_at"], "requested_bbox_wgs84": body.bbox, "crs": str(template["crs"]), "transform": list(template["transform"])[:6], "width": template["width"], "height": template["height"], "bands": [name.upper() for name in body.polarizations], "dtype": "float32", "nodata": template["nodata"], "valid_pixels_by_band": valid_counts, "valid_fraction_by_band": [count / (template["width"] * template["height"]) for count in valid_counts], "pixel_spacing_m": item["pixel_spacing_m"], "source_resolution_m": item["source_resolution_m"], "units": "linear gamma0 power", "georeferenced": True, "data_downloaded": True, "full_scene_downloaded": False, "size_bytes": final_path.stat().st_size, "preview_is_analysis_data": False, "note": "Native raster window clipped to this scene, without resampling; crop is not an orthorectified optical image or waste-type classification"}
    except HTTPException:
        raise
    except Exception:
        # GDAL errors can embed temporary signed URLs. Do not return raw exceptions.
        raise _error(502, "sar_crop_failed", "Unable to read the requested native SAR raster window; provider/network may be unavailable") from None


router = APIRouter(prefix="/api/sar", tags=["SAR"])


@router.get("/sources")
async def sources():
    return {"sources": [{"id": key, **value} for key, value in SOURCES.items()], "note": "Catalog, preview and scientific raster access are reported separately; pixel spacing is not effective recognition resolution"}


@router.post("/search")
async def search(body: SarSearchRequest):
    return await search_catalog(body)


@router.post("/assets")
async def assets(body: SarAssetRequest):
    item = await _lookup(body.source, body.item_id)
    if body.sign:
        if body.source != "sentinel1-rtc":
            raise _error(422, "sar_signing_unsupported", "Anonymous signing only applies to Planetary Computer RTC assets")
        for key in ("vv", "vh"):
            if key in item["assets"]:
                item["assets"][key]["href"] = await _signed_href(item["assets"][key]["href"])
                item["assets"][key]["signed"] = True
        item["signed_urls_are_temporary"] = True
    return item


@router.get("/preview")
async def preview(source: Source, item_id: str = Query(pattern=ITEM_PATTERN)):
    item = await _lookup(source, item_id)
    key = "rendered_preview" if source == "sentinel1-rtc" else "browse0"
    asset = item["assets"].get(key)
    if not asset:
        raise _error(404, "sar_preview_missing", "No preview asset provided for this scene")
    data = await _read_preview(asset["href"])
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise _error(413, "sar_preview_too_large", "Preview dimensions exceed the image limit")
            image.verify()
            media = "image/png" if image.format == "PNG" else "image/jpeg" if image.format == "JPEG" else None
        if not media:
            raise ValueError("unsupported image")
    except (UnidentifiedImageError, OSError, ValueError):
        raise _error(502, "sar_preview_invalid", "Provider did not return a decodable preview image") from None
    return Response(content=data, media_type=media, headers={"Cache-Control": "no-store", "X-Analysis-Data": "false"})


@router.post("/crop")
async def crop(body: SarCropRequest):
    item = await _lookup(body.source, body.item_id)
    signed = {}
    for key in body.polarizations:
        if key not in item["assets"]:
            raise _error(422, "sar_polarization_missing", f"Scene has no {key.upper()} asset")
        signed[key] = await _signed_href(item["assets"][key]["href"])
    async with _CROP_LOCK:
        return await asyncio.to_thread(_write_crop, body, item, signed)


@router.get("/files/{filename}")
async def file(filename: str):
    if not FILE_PATTERN.fullmatch(filename):
        raise _error(404, "sar_file_not_found", "SAR crop does not exist")
    path = OUTPUT_DIR / filename
    if path.is_symlink() or not path.is_file():
        raise _error(404, "sar_file_not_found", "SAR crop does not exist")
    return FileResponse(path, media_type="image/tiff", filename=filename, headers={"Cache-Control": "no-store"})
