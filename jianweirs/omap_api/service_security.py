"""Optional worker Bearer authentication for tunneled Windows services.

Authentication applies before route handling, including static scientific files
and future API routes. Public health is deliberately a minimal liveness check.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any

from fastapi import FastAPI
from fastapi.security import HTTPBearer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = frozenset({"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
SCHEME_NAME = "WorkerBearer"


def public_request(path: str, method: str) -> bool:
    return method.upper() in {"GET", "HEAD"} and (path.rstrip("/") or "/") in PUBLIC_PATHS


def worker_api_key_from_environment() -> str | None:
    value = os.environ.get("OMAP_WORKER_API_KEY", "")
    if value == "":
        return None
    # Misconfigured nonempty keys must not silently turn authentication off.
    if not 32 <= len(value) <= 4096 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError("OMAP_WORKER_API_KEY must contain at least 32 visible ASCII characters without whitespace.")
    return value


class WorkerBearerMiddleware:
    """Pure ASGI middleware preserves streamed scientific downloads."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self._expected = api_key.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket":
            # This worker exposes HTTP only. Never create an unguarded websocket
            # surface when authentication is active.
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http" or public_request(scope.get("path", ""), scope.get("method", "GET")):
            await self.app(scope, receive, send)
            return
        values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
        accepted = False
        if len(values) == 1:
            parts = values[0].split()
            if len(parts) == 2 and parts[0].lower() == b"bearer":
                accepted = hmac.compare_digest(parts[1], self._expected)
        if not accepted:
            response = JSONResponse(
                {"detail": {"code": "worker_authentication_required", "message": "Valid Bearer authorization is required."}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def configure_worker_security(application: FastAPI) -> None:
    """Read fixed per-process configuration; key rotation requires a restart."""
    api_key = worker_api_key_from_environment()
    application.state.worker_authentication_enabled = api_key is not None
    if api_key is None:
        return
    application.add_middleware(WorkerBearerMiddleware, api_key=api_key)
    original_openapi = application.openapi
    bearer_schema = HTTPBearer(scheme_name=SCHEME_NAME).model.model_dump(mode="json", by_alias=True, exclude_none=True)

    def authenticated_openapi() -> dict[str, Any]:
        schema = original_openapi()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes[SCHEME_NAME] = bearer_schema
        for path, methods in schema.get("paths", {}).items():
            for method, operation in methods.items():
                if method.lower() in {"get", "post", "put", "delete", "patch", "options", "head", "trace"} and not public_request(path, method):
                    operation["security"] = [{SCHEME_NAME: []}]
        return schema

    application.openapi = authenticated_openapi


def source_map_types_from_environment(defaults: dict[str, int]) -> tuple[dict[str, int], set[str]]:
    """Strict local source->MapType override; never accepts arbitrary URLs."""
    value = os.environ.get("OMAP_SOURCE_MAP_TYPES_JSON", "").strip()
    if not value:
        return dict(defaults), set()

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate mapping key")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
        if not isinstance(parsed, dict):
            raise ValueError("mapping must be an object")
        for source, map_type in parsed.items():
            if source not in defaults or isinstance(map_type, bool) or not isinstance(map_type, int) or not 1 <= map_type <= 2**31 - 1:
                raise ValueError("unsupported source or invalid MapType")
    except (ValueError, TypeError) as exc:
        # Do not echo configuration values into logs or HTTP responses.
        raise ValueError("OMAP_SOURCE_MAP_TYPES_JSON must map known optical sources to positive 32-bit integer MapType IDs, without duplicate keys.") from None
    return {**defaults, **parsed}, set(parsed)
