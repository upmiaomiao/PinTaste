"""Anonymous, expiring Planetary Computer container SAS signing.

Shared by spectral and SAR adapters so a multi-band request needs one token.
The public SAS signature is never a user account password or local credential.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

PC_TOKEN_ROOT = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
TOKEN_LOCK = asyncio.Lock()
TOKEN_CACHE: dict[tuple[str, str], dict] = {}


def _error(status, code, message):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def sign_href(href: str, client: httpx.AsyncClient | None = None, *, force_refresh: bool = False) -> dict:
    """Return {href, expires_at}; only vetted HTTPS Azure Blob asset URLs.

    SAS tokens are cached in process until 60 seconds before expiry. No user's
    saved credentials are accessed. Retry 429/503 at most twice, <=5 s per delay.
    """
    parsed = urlparse(href)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not host.endswith(".blob.core.windows.net"):
        raise _error(422, "invalid_planetary_asset", "仅支持公共 Planetary Computer Azure Blob HTTPS 资产。")
    account = host.split(".")[0]
    container = parsed.path.lstrip("/").split("/", 1)[0]
    if not re.fullmatch(r"[a-z0-9]{3,24}", account) or not re.fullmatch(r"[a-z0-9-]{3,63}", container):
        raise _error(502, "invalid_storage_path", "提供商未返回有效的存储账户与容器。")
    if client is None:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as own:
            return await sign_href(href, own, force_refresh=force_refresh)
    cache_key = (account, container)
    async with TOKEN_LOCK:
        token = TOKEN_CACHE.get(cache_key)
        if force_refresh or token is None or token["expires_timestamp"] <= datetime.now(timezone.utc).timestamp() + 60:
            try:
                for attempt in range(3):
                    try:
                        response = await client.get(f"{PC_TOKEN_ROOT}/{account}/{container}")
                    except httpx.TransportError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if response.status_code not in {429, 503} or attempt == 2:
                        break
                    try:
                        delay = float(response.headers.get("Retry-After", 1 + attempt))
                    except ValueError:
                        delay = 1 + attempt
                    await asyncio.sleep(min(5.0, max(0.5, delay)))
                response.raise_for_status()
                raw = response.json()
                expiry = datetime.fromisoformat(raw["msft:expiry"].replace("Z", "+00:00"))
                value = raw["token"]
                if not isinstance(value, str) or not value or "\n" in value or len(value) > 20000:
                    raise ValueError("invalid SAS")
                if expiry.tzinfo is None or expiry.timestamp() <= datetime.now(timezone.utc).timestamp():
                    raise ValueError("expired SAS")
                token = {"value": value.lstrip("?"), "expiry": raw["msft:expiry"], "expires_timestamp": expiry.timestamp()}
            except httpx.TimeoutException as exc:
                raise _error(504, "planetary_signing_timeout", "公共科学数据匿名签名请求超时。") from exc
            except httpx.HTTPStatusError as exc:
                raise _error(503 if exc.response.status_code == 429 else 502, "planetary_signing_error", f"公共匿名签名服务返回 HTTP {exc.response.status_code}。") from exc
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise _error(502, "invalid_asset_signature", "提供商未返回有效的匿名下载签名。") from exc
            TOKEN_CACHE[cache_key] = token
    separator = "&" if parsed.query else "?"
    return {"href": href + separator + token["value"], "expires_at": token["expiry"]}
