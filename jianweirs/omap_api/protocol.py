"""Async bridge to Ovital's official desktop WebSocket protocol.

The public methods use a fresh authenticated connection for each operation.  The
client never changes the visible map and never returns layer URLs or passwords.
The protocol and its version differences are documented in the official manuals:
https://www.gpsov.com/appdoc/index.php?id=32 (Web)
https://www.gpsov.com/appdoc/index.php?id=38 (native structures)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import itertools
import json
import math
from statistics import median
import time
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageChops, UnidentifiedImageError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


class OmapError(RuntimeError):
    """An error safe to expose to the local HTTP application."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        command: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.command = command
        self.metadata = metadata or {}


class OmapConnectionError(OmapError):
    pass


class OmapTimeoutError(OmapError):
    pass


class OmapAuthError(OmapError):
    pass


class OmapPermissionError(OmapError):
    pass


class OmapCaptureError(OmapError):
    pass


class OmapProtocolError(OmapError):
    pass


class OmapIncompleteImageError(OmapCaptureError):
    pass


# Native idMap is INT64, but the JSON parser in older desktop builds may handle
# IDs as signed 32-bit values. Keep all transport IDs positive and below 2**31.
_MAX_REQUEST_ID = (1 << 31) - 1
_request_ids = itertools.count((time.time_ns() // 1_000_000) % _MAX_REQUEST_ID)
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_PIXELS = 64 * 1024 * 1024
_PLACEHOLDER_RETRY_DELAY = 2.0
# Desktop exports sometimes quantize each color channel independently by one
# level. Keep the narrow observed warm-gray palette, including that rounding.
# Color alone is insufficient: both axes must also contain periodic grid lines.
_PLACEHOLDER_COLORS = {(v, v-rg, v-rg-gb) for v in range(177, 195)
                       for rg in (1, 2, 3) for gb in (3, 4, 5)}


def _next_request_id() -> int:
    return next(_request_ids) % _MAX_REQUEST_ID + 1

# Whitelist instead of returning arbitrary vendor configuration or message text.
_SAFE_KEYS = {
    "cmdid", "ret", "msg", "appid", "location", "custommaps", "cusommaps",
    "mapid", "maptype", "maplevel", "maxlevel", "minlevel", "maxpixel",
    "coordtype", "latlngtype", "lat", "lng", "x", "y", "x_lng", "y_lat",
    "x_lng1", "y_lat1", "x_lng2", "y_lat2", "width", "height", "value",
    "flag", "file", "postcnt", "sendreq", "imagetype", "totalpic", "haspic",
    "itotalpic", "ihaspic", "layerbigid", "layersmallid", "layerbigflag",
    "layersmallflag", "tileformat", "version", "uselayer", "issatellite",
    "strname", "name", "level", "bounds", "bbox", "center", "topleft",
    "bottomright", "lefttop", "rightbottom", "reqmapfiles", "reqmapdatas",
}


def sanitize_response(value: Any) -> Any:
    """Keep only known geographic/status fields, never image/URL/auth data."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() not in _SAFE_KEYS:
                continue
            clean = sanitize_response(item)
            if clean is not None:
                result[key] = clean
        return result
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    if isinstance(value, str):
        # Names can contain arbitrary server-controlled strings, including URLs.
        if "://" in value or "?" in value or "=" in value:
            return None
        return value[:300]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _get_case(value: dict[str, Any], key: str, default: Any = None) -> Any:
    return next((item for name, item in value.items() if name.lower() == key.lower()), default)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OmapProtocolError(f"{name} must be an integer >= {minimum}.")
    return value


def _decode_image(data: str) -> tuple[bytes, str, int, int, str]:
    """Decode standard or Ovital's *reversed* URL-safe Base64 mapping.

    Official omapinterface.js uses '-' -> '/', '_' -> '+', not the standard
    base64url mapping.  File validation decides between the two alphabets.
    """
    if not isinstance(data, str) or not data:
        raise OmapCaptureError("Ovital returned no image data.", command=20302)
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    data = "".join(data.split())
    alphabets = (
        ("standard", data),
        ("ovital", data.replace("-", "/").replace("_", "+")),
        ("base64url", data.replace("-", "+").replace("_", "/")),
    )
    seen: set[str] = set()
    for encoding, encoded in alphabets:
        if encoded in seen:
            continue
        seen.add(encoded)
        encoded += "=" * (-len(encoded) % 4)
        try:
            payload = base64.b64decode(encoded, validate=True)
            if payload.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif payload.startswith(b"\xff\xd8\xff"):
                media_type = "image/jpeg"
            else:
                continue
            with Image.open(io.BytesIO(payload)) as img:
                width, height = img.size
                if width * height > _MAX_IMAGE_PIXELS:
                    raise OmapCaptureError("Returned image exceeds the bridge pixel limit.", command=20302)
                img.verify()
            # PNG verify checks chunk integrity; JPEG also needs a complete decode.
            with Image.open(io.BytesIO(payload)) as img:
                img.load()
            return payload, media_type, width, height, encoding
        except OmapCaptureError:
            raise
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            continue
    raise OmapCaptureError("Image data is not a valid PNG or JPEG.", command=20302)


def _periodic_grid_axis(profile: list[float]) -> bool:
    """Require several regularly spaced dark lines, not merely low variance."""
    if len(profile) < 24:
        return False
    baseline = sorted(profile)[int(.75 * (len(profile) - 1))]
    # A rescaled/antialiased line can have multiple local minima separated by
    # only one or two pixels. Treat its contiguous dark band as ONE grid line.
    # Ignore clipped edge bands because their center is not the true line center.
    centers: list[float] = []
    start: int | None = None
    for index, value in enumerate([*profile, baseline]):
        if value < baseline - 2.0:
            if start is None:
                start = index
        elif start is not None:
            if start > 0 and index < len(profile):
                centers.append((start + index - 1) / 2)
            start = None
    if len(centers) < 3:
        return False
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    spacing = median(gaps)
    return 4 <= spacing <= 64 and all(abs(gap - spacing) <= max(1.5, spacing * .08) for gap in gaps)


def detect_ovital_placeholder(payload: bytes) -> dict[str, Any]:
    """Conservatively find complete or partial instances of the known texture.

    Overlapping patches from 32px to 256px catch missing tiles at different zoom
    scales inside otherwise valid imagery.
    A patch needs >=98% narrowly quantized palette pixels, substantial light background, and
    periodic darker lines along BOTH axes. Flat cloud, water, and ordinary gray
    roofs are not classified solely by color or contrast. This check does not
    establish cloud-free imagery or identify every possible provider error tile.
    """
    with Image.open(io.BytesIO(payload)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    red, green, blue = img.split()
    red_mask = red.point([255 if 177 <= v < 195 else 0 for v in range(256)])
    rg_mask = ImageChops.subtract(red, green).point([255 if 1 <= v <= 3 else 0 for v in range(256)])
    gb_mask = ImageChops.subtract(green, blue).point([255 if 3 <= v <= 5 else 0 for v in range(256)])
    palette_mask = ImageChops.multiply(ImageChops.multiply(red_mask, rg_mask), gb_mask)
    palette_pixels = palette_mask.histogram()[255]
    result = {"algorithm": "ovital_gray_grid_v3", "detected": False,
              "checked_patches": 0, "matching_patches": 0,
              "palette_pixels": palette_pixels}
    if width < 32 or height < 32 or palette_pixels < 32 * 32 * .98:
        return result
    pixels = img.load()
    # 64px handles both observed 7px and 15px grids quickly; 32px preserves
    # sensitivity to small missing regions, while larger windows cover zoomed
    # textures with wider line spacing.
    for size in (64, 32, 96, 128, 256):
        if size > min(width, height) or palette_pixels < size * size * .98:
            continue
        stride = max(16, size // 4)
        xs = list(range(0, width - size + 1, stride))
        ys = list(range(0, height - size + 1, stride))
        if xs[-1] != width - size:
            xs.append(width - size)
        if ys[-1] != height - size:
            ys.append(height - size)
        samples = tuple(round((i + .5) * size / 4) for i in range(4))
        for y in ys:
            for x in xs:
                # Eliminate patches with ordinary imagery colors before doing
                # the exact palette and two-axis grid checks.
                if any(pixels[x + dx, y + dy] not in _PLACEHOLDER_COLORS
                       for dy in samples for dx in samples):
                    continue
                patch_image = img.crop((x, y, x + size, y + size))
                patch = list(patch_image.get_flattened_data() if hasattr(patch_image, "get_flattened_data") else patch_image.getdata())
                result["checked_patches"] += 1
                count = sum(color in _PLACEHOLDER_COLORS for color in patch)
                light = sum(color[0] >= 191 and color in _PLACEHOLDER_COLORS for color in patch)
                if count < size * size * .98 or light < size * size * .30:
                    continue
                columns = [sum(patch[row * size + col][0] for row in range(size)) / size
                           for col in range(size)]
                rows = [sum(patch[row * size + col][0] for col in range(size)) / size
                        for row in range(size)]
                if _periodic_grid_axis(columns) and _periodic_grid_axis(rows):
                    result["detected"] = True
                    result["matching_patches"] = 1
                    result["first_matching_patch"] = [x, y, size, size]
                    return result
    return result


def _read_json(frame: str | bytes) -> dict[str, Any]:
    try:
        response = json.loads(frame)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OmapProtocolError("Ovital returned an invalid JSON frame.") from None
    if not isinstance(response, dict):
        raise OmapProtocolError("Ovital returned a non-object response.")
    return response


class OmapClient:
    def __init__(self, ws_url: str, psk: str, timeout: float = 45) -> None:
        parsed = urlsplit(ws_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("ws_url must be a ws:// or wss:// endpoint.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("ws_url must not contain credentials, query, or fragment.")
        if not isinstance(psk, str):
            raise ValueError("psk must be a string.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive number.")
        self.ws_url = ws_url
        self.timeout = float(timeout)
        self._md5psk = hashlib.md5(psk.encode("utf-8")).hexdigest().upper()[:16] if psk.strip() else ""
        self._subprotocol = parsed.scheme
        self._lock = asyncio.Lock()

    async def current(self) -> dict[str, Any]:
        """Return sanitized 20012 envelope; msg includes MapType/MapLevel."""
        response = await self._exchange(20012, {"CoordType": 0})
        return sanitize_response(response)

    async def maps(self) -> dict[str, Any]:
        """Return sanitized 20860 envelope, excluding source hosts/URLs/keys."""
        response = await self._exchange(20860, {})
        return sanitize_response(response)

    async def capture(self, request_dict: dict[str, Any]) -> dict[str, Any]:
        """Capture a normalized bbox request, optionally preparing its desktop view.

        Required: bbox=[min_x,min_y,max_x,max_y], map_type, zoom.
        Optional: coord_type=0, width=1024, height=1024, image_type=0 (PNG),
        send_req=True, allow_partial=False, timeout_seconds (can shorten timeout),
        prepare_view=False. Dedicated workers enable prepare_view; this sets
        the selected map, zoom and bbox center using official command 20011.
        """
        bbox = request_dict.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise OmapProtocolError("bbox must contain [min_x, min_y, max_x, max_y].")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bbox):
            raise OmapProtocolError("bbox coordinates must be finite numbers.")
        bbox = list(map(float, bbox))
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise OmapProtocolError("bbox must run from the lower left to the upper right.")
        coord_type = _integer(request_dict.get("coord_type", 0), "coord_type")
        if coord_type not in (0, 1, 2):
            raise OmapProtocolError("coord_type must be 0, 1, or 2.")
        if coord_type in (0, 1) and not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
            raise OmapProtocolError("Geographic bbox coordinates are outside longitude/latitude bounds.")
        map_type = _integer(request_dict.get("map_type"), "map_type")
        zoom = _integer(request_dict.get("zoom"), "zoom")
        width = _integer(request_dict.get("width", 1024), "width", minimum=1)
        height = _integer(request_dict.get("height", 1024), "height", minimum=1)
        if width * height > _MAX_IMAGE_PIXELS:
            raise OmapProtocolError("Requested image exceeds the bridge pixel limit.")
        image_type = _integer(request_dict.get("image_type", 0), "image_type")
        if image_type not in (0, 1):
            raise OmapProtocolError("image_type must be 0 (PNG) or 1 (JPEG).")
        timeout = request_dict.get("timeout_seconds", self.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise OmapProtocolError("timeout_seconds must be positive.")
        map_request = {
            "MapID": _next_request_id(),
            "Value": 1,
            "x_lng1": bbox[0], "y_lat1": bbox[1],
            "x_lng2": bbox[2], "y_lat2": bbox[3],
            "CoordType": coord_type,
            "MapLevel": zoom, "MapType": map_type,
            "Width": width, "Height": height,
            "PostCnt": 30,
            "SendReq": int(bool(request_dict.get("send_req", True))),
        }
        # The published Web builder omits ImageType and Flag. Use that minimal
        # payload for default PNG, including compatibility with desktop 9.x.
        if image_type:
            map_request["ImageType"] = image_type
        budget = min(float(timeout), self.timeout)
        deadline = asyncio.get_running_loop().time() + budget
        prepare_view = request_dict.get('prepare_view', False)
        if not isinstance(prepare_view, bool):
            raise OmapProtocolError('prepare_view must be a boolean.')
        if prepare_view:
            if coord_type not in (0, 1):
                raise OmapProtocolError('View preparation requires geographic coordinates.')
            await self._exchange(20011, {'Location': {
                'MapID': map_type, 'MapLevel': zoom, 'CoordType': coord_type,
                'lng': (bbox[0]+bbox[2])/2, 'lat': (bbox[1]+bbox[3])/2,
                'Active': 0,
            }}, timeout=max(.001, deadline-asyncio.get_running_loop().time()))
        attempts = 0
        placeholder_retries = 0
        placeholder_result: dict[str, Any] | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise OmapIncompleteImageError(
                    "Ovital still returned its missing-tile grid when the timeout expired.",
                    command=20302,
                    metadata={"capture_attempts": attempts, "placeholder_detection": placeholder_result},
                )
            attempts += 1
            map_request["MapID"] = _next_request_id()
            try:
                response = await self._exchange(
                    20302, {"ReqMapFiles": [map_request]},
                    timeout=remaining, image_request=map_request,
                )
            except OmapTimeoutError:
                if placeholder_retries:
                    raise OmapIncompleteImageError(
                        "Ovital did not finish downloading imagery after returning its missing-tile grid.",
                        command=20302,
                        metadata={"capture_attempts": attempts, "placeholder_detection": placeholder_result},
                    ) from None
                raise
            msg = response["msg"]
            payload, media, actual_width, actual_height, encoding = _decode_image(_get_case(msg, "Data"))
            placeholder_result = detect_ovital_placeholder(payload)
            if not placeholder_result["detected"]:
                break
            placeholder_retries += 1
            await asyncio.sleep(min(_PLACEHOLDER_RETRY_DELAY,
                                    max(0, deadline - asyncio.get_running_loop().time())))
        status = _get_case(msg, "Ret")
        warnings: list[str] = []
        if status == 2:
            completeness = "partial"
            if not request_dict.get("allow_partial", False):
                raise OmapIncompleteImageError(
                    "Ovital returned a partial image; it was not submitted for analysis.",
                    code=2, command=20302,
                    metadata={**sanitize_response(response), "capture_attempts": attempts,
                              "placeholder_detection": placeholder_result},
                )
            warnings.append("Ovital reported a partial image.")
        elif status == 1:
            completeness = "complete"
        else:
            # Web manual says 3 is success, native source says 3 is no data.
            # A successfully decoded image is usable, but status alone can't prove
            # complete coverage on every desktop version.
            completeness = "unverified"
            warnings.append("Image completeness is unverified for this desktop status code.")
        if (actual_width, actual_height) != (width, height):
            warnings.append("Returned image dimensions differ from the requested dimensions.")
        metadata = {
            "bbox": bbox, "coord_type": coord_type,
            "map_type": map_type, "zoom": zoom,
            "width": actual_width, "height": actual_height,
            "request_id": _get_case(msg, "MapID", map_request["MapID"]),
            "capture_status": status, "completeness": completeness,
            "completeness_basis": "client_status_and_known_placeholder_check",
            "image_encoding": encoding,
            "capture_attempts": attempts,
            "placeholder_check": "passed",
            "placeholder_detection": placeholder_result,
            "placeholder_retries": placeholder_retries,
            "desktop_view_prepared": prepare_view,
            "warnings": warnings,
        }
        return {
            "image_bytes": payload,
            "media_type": media,
            "metadata": metadata,
            "raw": sanitize_response(response),
        }

    async def _exchange(
        self,
        command: int,
        msg: dict[str, Any],
        *,
        timeout: float | None = None,
        image_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        budget = timeout or self.timeout
        deadline = asyncio.get_running_loop().time() + budget
        try:
            async with asyncio.timeout(budget):
                async with self._lock:
                    for attempt in range(2):
                        try:
                            remaining = deadline - asyncio.get_running_loop().time()
                            async with connect(
                                self.ws_url,
                                subprotocols=[self._subprotocol],
                                max_size=_MAX_FRAME_BYTES,
                                open_timeout=remaining,
                                close_timeout=1,
                                ping_interval=20,
                                compression=None,
                                proxy=None,
                            ) as ws:
                                await ws.send(json.dumps({
                                    "cmdid": 20000, "ret": 0,
                                    "msg": {"md5psk": self._md5psk},
                                }, separators=(",", ":")))
                                await self._registration(ws)
                                if attempt and image_request is not None:
                                    image_request["MapID"] = _next_request_id()
                                await ws.send(json.dumps({
                                    "cmdid": command, "ret": 0, "msg": msg,
                                }, separators=(",", ":")))
                                return await self._receive(ws, command, image_request)
                        except (OSError, ConnectionClosed, InvalidHandshake):
                            if attempt:
                                raise OmapConnectionError(
                                    "Cannot communicate with Ovital. Check that it is running and its WebSocket interface is enabled.",
                                    command=command,
                                ) from None
        except TimeoutError:
            raise OmapTimeoutError(
                "Ovital did not finish the request before the timeout.", command=command,
            ) from None
        raise OmapConnectionError("Ovital connection ended unexpectedly.", command=command)

    async def _registration(self, ws: Any) -> None:
        while True:
            response = _read_json(await ws.recv())
            if _get_case(response, "cmdid") != 20000:
                continue
            msg = _get_case(response, "msg", {})
            ret = _get_case(response, "ret", 0)
            if not isinstance(msg, dict) or _get_case(msg, "errmsg") or (isinstance(ret, (int, float)) and ret < 0):
                raise OmapAuthError("Ovital rejected registration. Check the pre-shared key and allowed IP settings.", command=20000)
            return

    async def _receive(self, ws: Any, command: int, image_request: dict[str, Any] | None) -> dict[str, Any]:
        while True:
            response = _read_json(await ws.recv())
            if _get_case(response, "cmdid") != command:
                continue
            ret = _get_case(response, "ret")
            if isinstance(ret, (int, float)) and ret < 0:
                error_type = OmapPermissionError if ret in (-5, -7, -14, -15) else OmapProtocolError
                raise error_type(
                    "Ovital denied the operation." if ret in (-5, -7, -14, -15) else "Ovital rejected the request parameters or could not process the request.",
                    code=ret, command=command, metadata=sanitize_response(response),
                )
            msg = _get_case(response, "msg", {})
            if not isinstance(msg, dict):
                raise OmapProtocolError("Ovital returned an invalid message object.", command=command)
            if image_request is None:
                return response
            candidates = [msg]
            for key in ("ReqMapFiles", "ReqMapDatas"):
                items = _get_case(msg, key)
                if isinstance(items, list):
                    candidates.extend(item for item in items if isinstance(item, dict))
            for item in candidates:
                request_id = _get_case(item, "MapID")
                if request_id is not None and str(request_id) != str(image_request["MapID"]):
                    continue
                status = _get_case(item, "Ret")
                if isinstance(status, (int, float)) and status < 0:
                    raise OmapCaptureError(
                        "Ovital could not produce the requested image.",
                        code=status, command=command, metadata=sanitize_response(response),
                    )
                if _get_case(item, "Data"):
                    # Normalize only the envelope; vendor field case is preserved.
                    return {"cmdid": command, "ret": ret, "msg": item}
