"""HTTP API bound to localhost by the supplied startup script.

Only the protocol client's read/capture methods are exposed. Provider URLs,
credentials and raw protocol envelopes are never HTTP response fields.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError

from .models import MAX_IMAGE_PIXELS, SnapshotRequest
from .service_security import configure_worker_security

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAPS_CACHE_TTL_SECONDS = 30.0
load_dotenv(PROJECT_ROOT / ".env")


def _error(status: int, code: str, message: str, upstream_code: int | None = None) -> HTTPException:
    detail = {"code": code, "message": message}
    if upstream_code in {-4, -5, -7, -14, -15, -16}:
        detail["upstream_code"] = upstream_code
    return HTTPException(status_code=status, detail=detail)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _message(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    msg = raw.get("msg", raw)
    return msg if isinstance(msg, dict) else {}


def _pick(record: dict, *names: str) -> Any:
    keys = {str(key).casefold(): value for key, value in record.items()}
    return next((keys[name.casefold()] for name in names if name.casefold() in keys), None)


def _clean_current(raw: Any) -> dict:
    msg = _message(raw)
    result = {}
    for name, aliases in {
        "map_type": ("MapType", "map_type"),
        "zoom": ("MapLevel", "zoom", "Level"),
        "coord_type": ("CoordType", "coord_type"),
    }.items():
        value = _number(_pick(msg, *aliases))
        if value is not None:
            result[name] = value
    lon = _number(_pick(msg, "x_lng", "longitude", "lon", "lng"))
    lat = _number(_pick(msg, "y_lat", "latitude", "lat"))
    if lon is not None and lat is not None:
        result["center"] = [lon, lat]
    elif isinstance(msg.get("center"), (list, tuple)) and len(msg["center"]) == 2:
        values = [_number(value) for value in msg["center"]]
        if all(value is not None for value in values):
            result["center"] = values
    return result


def _clean_maps(raw: Any) -> list[dict]:
    msg = _message(raw)
    candidates = raw if isinstance(raw, list) else _pick(msg, "CustomMaps", "maps", "MapList")
    if not isinstance(candidates, list):
        return []
    result = []
    for record in candidates:
        if not isinstance(record, dict):
            continue
        map_id = _number(_pick(record, "MapId", "MapType", "id", "map_type"))
        if map_id is None:
            continue
        item = {"id": map_id}
        name = _pick(record, "strName", "name", "MapName")
        if isinstance(name, str) and "://" not in name and not re.search(r"(?:token|psk|key)\s*=", name, flags=re.IGNORECASE):
            item["name"] = name[:200]
        for public_name, aliases in {
            "min_zoom": ("MinLevel", "min_zoom"),
            "max_zoom": ("MaxLevel", "max_zoom"),
            "coord_type": ("CoordType", "coord_type"),
            "tile_format": ("TileFormat", "tile_format"),
            "is_satellite": ("IsSatellite", "is_satellite"),
        }.items():
            value = _pick(record, *aliases)
            if isinstance(value, (int, float, bool)):
                item[public_name] = value
        result.append(item)
    return result


def _png_bytes(image_bytes: bytes) -> tuple[bytes, int, int]:
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise _error(502, "invalid_image", "Omap returned no usable image.")
    if len(image_bytes) > 32 * 1024 * 1024:
        raise _error(502, "invalid_image", "Omap returned an image larger than the safety limit.")
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            width, height = original.size
            if not (1 <= width <= 2048 and 1 <= height <= 2048 and width * height <= MAX_IMAGE_PIXELS):
                raise _error(502, "invalid_image", "Omap returned dimensions beyond the configured image limits.")
            original.load()
            converted = original.convert("RGBA" if "A" in original.getbands() else "RGB")
            output = io.BytesIO()
            converted.save(output, format="PNG")
            return output.getvalue(), width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise _error(502, "invalid_image", "Omap returned an invalid or unsupported image.") from None


def _save_result(output_dir: Path, image_id: str, png: bytes, metadata: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # UUID filenames are generated by this service and never contain user paths.
    png_path = output_dir / f"{image_id}.png"
    json_path = output_dir / f"{image_id}.json"
    png_tmp = output_dir / f".{image_id}.png.tmp"
    json_tmp = output_dir / f".{image_id}.json.tmp"
    try:
        png_tmp.write_bytes(png)
        json_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        png_tmp.replace(png_path)
        json_tmp.replace(json_path)
    finally:
        png_tmp.unlink(missing_ok=True)
        json_tmp.unlink(missing_ok=True)


def create_app(client: Any = None, output_dir: str | Path | None = None) -> FastAPI:
    application = FastAPI(title="本机多源遥感影像 API", version="0.2.0")
    configure_worker_security(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    application.state.omap_client = client
    application.state.command_lock = asyncio.Lock()
    application.state.maps_cache_lock = asyncio.Lock()
    application.state.maps_cache = None
    application.state.maps_cached_at = 0.0
    application.state.output_dir = Path(output_dir or os.environ.get("OMAP_OUTPUT_DIR") or PROJECT_ROOT / "outputs" / "images").resolve()

    def get_client() -> Any:
        if application.state.omap_client is None:
            ws_url = os.environ.get("OMAP_WS_URL", "").strip()
            psk = os.environ.get("OMAP_PSK", "")
            if not ws_url or not psk:
                raise _error(503, "omap_not_configured", "Configure OMAP_WS_URL and OMAP_PSK in the local .env file.")
            from .protocol import OmapClient

            # Capture defaults to 45 seconds, but an explicit request may allow
            # up to 120. current/maps still use the HTTP call's 45-second budget.
            try:
                application.state.omap_client = OmapClient(ws_url, psk, timeout=120)
            except ValueError:
                raise _error(503, "omap_invalid_configuration", "The local Omap connection configuration is invalid.") from None
        return application.state.omap_client

    async def call(method: str, argument: dict | None = None, timeout: float = 45) -> Any:
        omap = get_client()
        try:
            async with asyncio.timeout(timeout):
                async with application.state.command_lock:
                    operation = getattr(omap, method)
                    return await operation(argument) if argument is not None else await operation()
        except HTTPException:
            raise
        except Exception as exc:
            kind = type(exc).__name__
            error_types = {
                "OmapConnectionError": (503, "omap_unavailable", "Cannot connect to the local Omap application."),
                "OmapTimeoutError": (504, "omap_timeout", "Omap did not finish within the request timeout."),
                "TimeoutError": (504, "omap_timeout", "Omap did not finish within the request timeout."),
                "OmapAuthError": (502, "omap_auth_failed", "Omap rejected the configured authentication."),
                "OmapPermissionError": (403, "omap_permission_denied", "The Omap account or map source does not permit this operation."),
                "OmapIncompleteImageError": (502, "omap_incomplete_image", "Omap returned incomplete map imagery. Try a smaller area or another source."),
                "OmapCaptureError": (502, "omap_capture_failed", "Omap could not capture the selected imagery."),
                "OmapProtocolError": (502, "omap_protocol_error", "Omap returned an unexpected protocol response."),
            }
            status, code, message = error_types.get(kind, (502, "omap_error", "The Omap operation failed."))
            raise _error(status, code, message, getattr(exc, 'code', None)) from None

    async def available_maps(timeout: float = 45) -> list[dict]:
        cached = application.state.maps_cache
        if cached is not None and time.monotonic() - application.state.maps_cached_at < MAPS_CACHE_TTL_SECONDS:
            return cached
        # Prevent concurrent cold requests from each opening a map-list socket.
        # The encompassing snapshot deadline also covers time waiting here.
        async with application.state.maps_cache_lock:
            cached = application.state.maps_cache
            if cached is not None and time.monotonic() - application.state.maps_cached_at < MAPS_CACHE_TTL_SECONDS:
                return cached
            maps = _clean_maps(await call("maps", timeout=timeout))
            application.state.maps_cache = maps
            application.state.maps_cached_at = time.monotonic()
            return maps

    def validate_map_request(request: SnapshotRequest, map_type: int, maps: list[dict]) -> dict:
        selected = next((item for item in maps if item.get("id") == map_type), None)
        if selected is None:
            from .optical import BUILTIN_MAPS
            selected = getattr(application.state, "optical_builtin_maps", BUILTIN_MAPS).get(map_type)
        if selected is None:
            raise _error(422, "unknown_map", "The selected map_type is not in Omap's available map list. Choose an ID from /api/maps.")
        minimum, maximum = selected.get("min_zoom"), selected.get("max_zoom")
        if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)) or not minimum <= request.zoom <= maximum:
            message = "The requested zoom is outside the selected source's supported levels."
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
                message += f" Supported range: {minimum} to {maximum}."
            raise _error(422, "map_zoom_out_of_range", message)
        return selected

    def resolve_map_type(request: SnapshotRequest) -> int:
        if request.map_type is not None:
            return request.map_type
        value = os.environ.get("OMAP_DEFAULT_MAP_TYPE", "").strip()
        try:
            map_type = int(value)
            if map_type < 0:
                raise ValueError
            return map_type
        except ValueError:
            raise _error(422, "map_type_required", "Provide map_type or configure OMAP_DEFAULT_MAP_TYPE.") from None

    async def snapshot(request: SnapshotRequest) -> tuple[str, bytes, dict]:
        map_type = resolve_map_type(request)
        arguments = request.capture_arguments(map_type)
        # Dedicated authenticated worker sessions must activate the requested
        # source/area before uncached tile acquisition on desktop 9.8.5.
        # Local legacy calls retain their original view-preserving behavior.
        arguments['prepare_view'] = bool(application.state.worker_authentication_enabled)
        deadline = time.monotonic() + request.timeout_seconds
        try:
            # One budget covers map discovery, queueing and image retrieval;
            # map discovery cannot silently add another 45-second wait.
            async with asyncio.timeout(request.timeout_seconds):
                maps = await available_maps(timeout=max(0.001, deadline - time.monotonic()))
                selected_map = validate_map_request(request, map_type, maps)
                remaining = max(0.001, deadline - time.monotonic())
                arguments["timeout_seconds"] = remaining
                result = await call("capture", arguments, remaining)
        except TimeoutError:
            raise _error(504, "omap_timeout", "Omap did not finish within the request timeout.") from None
        if not isinstance(result, dict):
            raise _error(502, "invalid_image", "Omap returned an unexpected capture response.")
        png, width, height = await asyncio.to_thread(_png_bytes, result.get("image_bytes"))
        image_id = uuid.uuid4().hex
        protocol_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata = {
            "image_id": image_id,
            "source": "omap_desktop",
            "map_type": map_type,
            "map_name": selected_map.get("name"),
            "request_bounds": arguments["bbox"],
            "request_center": list(request.center) if request.center else None,
            "request_radius_m": request.radius_m,
            "coord_type": request.coord_type,
            "coordinate_system": "CGCS2000" if request.coord_type == 0 else "GCJ-02",
            "coordinate_system_verified": False,
            "coordinate_system_basis": "Omap public Web API coordinate-code convention",
            "zoom": request.zoom,
            "requested_width": request.width,
            "requested_height": request.height,
            "width": width,
            "height": height,
            "media_type": "image/png",
            "acquisition_time": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "georeferenced": False,
            "desktop_view_prepared": protocol_metadata.get('desktop_view_prepared') is True,
            "bounds_are": "requested_area_only",
            "pixel_to_map_transform": None,
            "native_resolution_m": None,
            "completeness_caveat": "Completeness describes the desktop delivery status. It does not verify clouds, image age, useful content, or full geographic coverage.",
        }
        status = protocol_metadata.get("capture_status")
        if isinstance(status, int) and not isinstance(status, bool):
            metadata["capture_status"] = status
        elif isinstance(status, str) and status in {"complete", "partial", "unverified"}:
            metadata["capture_status"] = status
        completeness = protocol_metadata.get("completeness")
        if isinstance(completeness, str) and completeness in {"complete", "partial", "unverified"}:
            metadata["completeness"] = completeness
        basis = protocol_metadata.get("completeness_basis")
        if basis == "client_status_and_known_placeholder_check":
            metadata["completeness_basis"] = basis
        else:
            metadata["completeness_basis"] = "desktop_delivery_status"
        attempts = protocol_metadata.get("capture_attempts")
        metadata["capture_attempts"] = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) and 1 <= attempts <= 10 else None
        placeholder_check = protocol_metadata.get("placeholder_check")
        metadata["placeholder_check"] = placeholder_check if isinstance(placeholder_check, str) and placeholder_check in {"passed", "failed", "not_checked", "unverified"} else "not_checked"
        metadata["placeholder_check_basis"] = "Known Omap gray-grid placeholder pattern only; not a full image quality test."
        # Generate public notices locally, rather than exposing arbitrary
        # upstream warning text that could include URLs or credentials.
        metadata["warnings"] = []
        if metadata.get("completeness") == "unverified":
            metadata["warnings"].append("Image completeness is unverified for the returned desktop status code.")
        if width != request.width or height != request.height:
            metadata["warnings"].append("Returned image dimensions differ from the requested dimensions.")
        try:
            await asyncio.to_thread(_save_result, application.state.output_dir, image_id, png, metadata)
        except OSError:
            raise _error(500, "image_storage_failed", "Cannot save the captured image in the configured local output directory.") from None
        return image_id, png, metadata

    @application.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @application.get("/api/current")
    async def current() -> dict:
        return _clean_current(await call("current"))

    @application.get("/api/maps")
    async def maps() -> dict:
        return {"maps": await available_maps()}

    @application.post("/api/snapshot")
    async def snapshot_json(request: SnapshotRequest) -> dict:
        image_id, _, metadata = await snapshot(request)
        return {"image_id": image_id, "image_url": f"/api/images/{image_id}.png", "metadata": metadata}

    @application.post("/api/snapshot/image")
    async def snapshot_image(request: SnapshotRequest) -> Response:
        image_id, png, _ = await snapshot(request)
        return Response(png, media_type="image/png", headers={"X-Image-ID": image_id, "Cache-Control": "no-store"})

    @application.get("/api/images/{image_id}.png")
    async def image(image_id: str) -> FileResponse:
        if not IMAGE_ID_RE.fullmatch(image_id):
            raise _error(404, "image_not_found", "Image not found.")
        path = application.state.output_dir / f"{image_id}.png"
        if not path.is_file() or path.is_symlink():
            raise _error(404, "image_not_found", "Image not found.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def store_external_image(result: dict) -> dict:
        png, width, height = await asyncio.to_thread(_png_bytes, result.get('image_bytes'))
        image_id = uuid.uuid4().hex
        metadata = {**result.get('metadata', {}), 'image_id': image_id,
                    'width': width, 'height': height, 'media_type': 'image/png',
                    'retrieved_at': datetime.now(timezone.utc).isoformat()}
        await asyncio.to_thread(_save_result, application.state.output_dir, image_id, png, metadata)
        return {'image_id': image_id, 'image_url': f'/api/images/{image_id}.png', 'metadata': metadata}

    # Shared closures preserve one desktop queue for original and named APIs.
    application.state.snapshot_function = snapshot
    application.state.available_maps_function = available_maps
    application.state.store_external_image_function = store_external_image
    from .optical import configure_optical_sources, router as optical_router
    configure_optical_sources(application)
    application.include_router(optical_router)
    from .history import router as history_router
    application.include_router(history_router)
    from .spectral import router as spectral_router
    application.include_router(spectral_router)
    from .spectral_aliases import multispectral_router, hyperspectral_router
    application.include_router(multispectral_router)
    application.include_router(hyperspectral_router)
    from .sar import router as sar_router
    application.include_router(sar_router)
    return application


app = create_app()
