"""Client for the existing local imagery worker or authenticated job gateway.

Provider asset URLs are returned as metadata, never fetched with this client's
Bearer credential. Unknown acquisition dates and resolution values stay unknown.
"""
from __future__ import annotations

import asyncio
import ipaddress
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .source_registry import SourceRegistry


class DataClientError(RuntimeError):
    """A bounded, credential-safe failure suitable for an API or report."""

    def __init__(self, code: str, message: str, status_code: int | None = None, *, upstream_code: str | None = None,
                 trace_id: str | None = None):
        self.code = code
        self.status_code = status_code
        self.upstream_code = upstream_code
        self.trace_id = trace_id
        super().__init__(message)


_WORKER_ROUTES = {
    "optical.latest": ("POST", "/api/optical/latest"),
    "optical.register": ("POST", "/api/optical/register"),
    "multispectral.search": ("POST", "/api/spectral/search"),
    "multispectral.subset": ("POST", "/api/spectral/subset"),
    "multispectral.asset": ("GET", "/api/spectral/asset"),
    "hyperspectral.search": ("POST", "/api/spectral/search"),
    "hyperspectral.asset": ("GET", "/api/spectral/asset"),
    "sar.search": ("POST", "/api/sar/search"),
    "sar.assets": ("POST", "/api/sar/assets"),
    "sar.crop": ("POST", "/api/sar/crop"),
    "sar.preview": ("GET", "/api/sar/preview"),
}
_MULTI = {"sentinel2", "landsat", "hls_s30", "hls_l30"}
_HYPER = {"emit", "enmap"}
_FILE_PATH = re.compile(
    r"/api/(?:images/[0-9a-f]{32}\.png|(?:spectral|sar)/files/[0-9a-f]{32}\.tif|jobs/[0-9a-f]{32}/file)"
)
_JOB_PATH = re.compile(r"/api/jobs/[0-9a-f]{32}")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,239}")
_SAR_SOURCES = {
    "sentinel1-rtc", "sentinel1-grd", "sentinel1-slc",
    "alos-palsar-rtc", "nisar-gcov", "jaxa-palsar-mosaic",
}


class DataClient:
    """Read API capabilities, run a bounded operation, and fetch local artifacts.

    ``mode`` accepts ``auto`` (health discovery), ``worker``, ``local`` (worker
    alias), or ``gateway``. An injected AsyncClient is borrowed, never closed.
    Authentication comes from configuration, not model-generated parameters.
    Owned clients bypass environment/system proxies for loopback destinations.
    An injected client retains its caller's transport and proxy policy.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        mode: str | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        poll_interval: float = 2,
        max_wait_seconds: float = 600,
        max_download_bytes: int = 64 * 1024 * 1024,
        source_registry: SourceRegistry | None = None,
        source_policy: dict | None = None,
        unified_config: dict | None = None,
    ):
        address = (base_url or os.getenv("WUYU_DATA_BASE_URL", "http://127.0.0.1:8765")).rstrip("/")
        try:
            parsed = urlsplit(address)
            port = parsed.port
        except ValueError:
            raise DataClientError("invalid_configuration", "Data API URL has an invalid host or port.") from None
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or port == 0
        ):
            raise DataClientError("invalid_configuration", "Data API URL must be an HTTP(S) origin without credentials.")
        if parsed.scheme == "http":
            try:
                private = ipaddress.ip_address(parsed.hostname).is_private
            except ValueError:
                private = parsed.hostname == "localhost"
            if not private:
                raise DataClientError("invalid_configuration", "Public data API origins require HTTPS.")
        self.base_url = address
        selected_mode = mode or os.getenv("WUYU_DATA_MODE", "auto")
        self.mode = "worker" if selected_mode == "local" else selected_mode
        if self.mode not in {"auto", "worker", "gateway"}:
            raise DataClientError("invalid_configuration", "Data mode must be auto, worker, local, or gateway.")
        if not math.isfinite(max_wait_seconds) or not 0 < max_wait_seconds <= 600:
            raise DataClientError("invalid_configuration", "Data operation deadline must be within 600 seconds.")
        if not math.isfinite(poll_interval) or poll_interval < 0 or max_download_bytes < 1:
            raise DataClientError("invalid_configuration", "Data polling and download limits are invalid.")
        key = api_key if api_key is not None else os.getenv("WUYU_DATA_API_KEY", "")
        self._headers = {"Authorization": "Bearer " + key} if key else {}
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=not loopback)
        self._owns_client = client is None
        self._mode_lock = asyncio.Lock()
        self.poll_interval = poll_interval
        self.max_wait_seconds = max_wait_seconds
        self.max_download_bytes = max_download_bytes
        self.configuration_fingerprint = hashlib.sha256((address + "\0" + key).encode()).hexdigest()
        from .unified_imagery import UnifiedImageryClient, unified_configuration
        unified = unified_configuration(unified_config)
        self.unified = UnifiedImageryClient(unified, max_download_bytes=max_download_bytes) if unified.get("enabled") else None
        if self.unified:
            self.configuration_fingerprint = hashlib.sha256((self.configuration_fingerprint + "\0" + self.unified.fingerprint).encode()).hexdigest()
        self.source_registry = source_registry
        self.source_policy = source_policy
        if self.source_registry is None and client is None:
            folder = Path(os.getenv("WUYU_OUTPUT_DIR", "outputs/wuyu"))
            self.source_registry = SourceRegistry(os.getenv("WUYU_SOURCE_REGISTRY", str(folder / "source-registry.json")))

    async def __aenter__(self) -> DataClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self.unified:
            await self.unified.aclose()
        if self._owns_client:
            await self._client.aclose()

    def is_unified_source(self, source: str | None) -> bool:
        """Select credentials by a configured source contract, never request URLs."""
        from .unified_imagery import SOURCE_PROFILES, EXCLUSIVE_SOURCES
        return isinstance(source, str) and source in SOURCE_PROFILES and (source in EXCLUSIVE_SOURCES or bool(self.unified and self.unified.ready))

    async def _read(
        self, method: str, path: str, *, payload: dict | None = None,
        params: dict | None = None, headers: dict | None = None,
        maximum: int = 5 * 1024 * 1024,
    ) -> tuple[bytes, str, int]:
        try:
            async with self._client.stream(
                method, self.base_url + path, json=payload, params=params,
                headers={**self._headers, **(headers or {})},
                timeout=min(self.max_wait_seconds, 180), follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise DataClientError("redirect_rejected", "Data API redirects are not followed.", response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise DataClientError("response_too_large", "Data API response exceeds its configured size limit.")
                if not response.is_success:
                    code = "data_api_rejected"
                    try:
                        detail = json.loads(content).get("detail", {})
                        candidate = detail.get("code") if isinstance(detail, dict) else None
                        if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate):
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                    raise DataClientError(code, "Data API request failed; inspect the upstream service status.", response.status_code)
                return bytes(content), response.headers.get("content-type", "application/octet-stream"), response.status_code
        except httpx.TimeoutException:
            raise DataClientError("data_timeout", "Data API did not finish within the configured deadline.") from None
        except httpx.HTTPError:
            raise DataClientError("data_unavailable", "Data API could not be reached.") from None

    @staticmethod
    def _object(content: bytes) -> dict[str, Any]:
        try:
            data = httpx.Response(200, content=content).json()
        except ValueError:
            raise DataClientError("invalid_response", "Data API did not return a JSON object.") from None
        if not isinstance(data, dict):
            raise DataClientError("invalid_response", "Data API did not return a JSON object.")
        return data

    async def _resolve_mode(self) -> str:
        if self.mode != "auto":
            return self.mode
        async with self._mode_lock:
            if self.mode == "auto":
                content, _, _ = await self._read("GET", "/health")
                health = self._object(content)
                if health.get("status") != "ok":
                    raise DataClientError("data_not_ready", "Data API health discovery did not report a live service.")
                self.mode = "gateway" if health.get("service") == "imagery_gateway" else "worker"
        return self.mode

    def _with_availability(self, body: dict, *, cached: bool = False) -> dict:
        # Store raw provider capabilities; policy changes cannot fabricate rights.
        if self.source_registry:
            if not cached:
                self.source_registry.save_catalog(self.configuration_fingerprint, body)
            if self.source_policy:
                from .source_choices import apply_source_policy
                body = apply_source_policy(body, self.source_policy)
            return {**body, "from_cache": cached, "audit_artifacts": self._audit_artifacts(),
                    "availability": self.source_registry.snapshot(self.configuration_fingerprint)}
        if self.source_policy:
            from .source_choices import apply_source_policy
            body = apply_source_policy(body, self.source_policy)
        return body

    def _audit_artifacts(self) -> dict:
        """Publish compact audit links only when bound to this API configuration."""
        root = Path(os.getenv("WUYU_AUDIT_ROOT", str(Path(__file__).resolve().parents[1] / "outputs")))
        result: dict[str, Any] = {"source_report": None, "coverage_report": None}
        for kind, relative, report_name in (
            ("source", "source-audit/source-results.json", "source-report.html"),
            ("coverage", "chengdu-coverage/coverage.json", "coverage.html"),
        ):
            path = root / relative
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("endpoint_fingerprint") != self.configuration_fingerprint:
                    continue
                report = path.with_name(report_name)
                if not report.is_file():
                    continue
                result[kind + "_report"] = str(report.resolve())
                result[kind + "_results"] = str(path.resolve())
                if kind == "source":
                    rows = data.get("sources", [])
                    result["source_summary"] = {
                        "source_count": len(rows), "checked_at": data.get("checked_at"),
                        "downloaded_source_count": sum(any(c.get("capability") == "download" and c.get("state") == "passed" for c in row.get("checks", [])) for row in rows),
                        "representative_site_count": len(data.get("sample_profile", {}).get("samples", [])),
                        "all_36_sites_tested": False,
                    }
                else:
                    result["coverage_summary"] = {
                        "point_count": data.get("point_count"), "source_count": len(data.get("sources", [])),
                        "period_count": len(data.get("periods", {})),
                        "matrix_cell_count": data.get("point_count", 0) * len(data.get("sources", [])) * len(data.get("periods", {})),
                        "status_counts": data.get("status_counts", {}), "checked_at": data.get("checked_at"),
                        "meaning": "catalogue footprint intersections; not downloaded or cloud-free imagery; not automatically used as global routing exclusions",
                    }
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return result

    def route(self, group: str, operation: str, *, bbox=None, time_range: str | None = None, catalog: dict | None = None) -> dict:
        """Use saved capability evidence without network calls or model selection."""
        if self.source_registry:
            return self.source_registry.route(self.configuration_fingerprint, group, operation, bbox=bbox, time_range=time_range, catalog=catalog)
        entries = (catalog or {}).get("groups", {}).get(group, {}).get("sources", [])
        names = [entry.get("source", entry.get("id")) for entry in entries if isinstance(entry, dict)]
        names = [name for name in names if isinstance(name, str)]
        return {"group": group, "operation": operation, "eligible": [], "unknown": names, "blocked": [], "candidate_order": names}

    async def catalog(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Discover live catalogues; partial failures remain explicit in errors.

        ``groups`` retains upstream metadata. The worker's shared spectral
        catalogue is separated by its published ``spectral_type`` field.
        """
        groups: dict[str, dict] = {}
        errors: list[dict] = []
        if self.source_registry and not force_refresh:
            cached = self.source_registry.catalog(self.configuration_fingerprint)
            if cached and self.unified:
                from .unified_imagery import CATALOG_SCHEMA
                optical=cached.get('groups',{}).get('optical',{})
                if (optical.get('unified_api') or {}).get('catalog_schema')!=CATALOG_SCHEMA:
                    # A pre-schema cached default max_zoom=22 must not acquire
                    # provider-declared status just because code was upgraded.
                    cached=None
                else:self.unified.adopt_catalog_sources(optical.get('sources',[]))
            if cached:
                if self.mode == "auto":
                    self.mode = cached.get("mode", "auto")
                return self._with_availability(cached, cached=True)
        try:
            mode = await self._resolve_mode()
        except DataClientError as exc:
            mode = self.mode
            errors.append({"group": "discovery", "code": exc.code, "status_code": exc.status_code})
        names = () if errors else ("optical", "multispectral", "hyperspectral", "sar") if mode == "gateway" else ("optical", "spectral", "sar")

        async def discover_group(name: str) -> None:
            try:
                content, _, _ = await self._read("GET", f"/api/{name}/sources")
                body = self._object(content)
                if not isinstance(body.get("sources"), list):
                    raise DataClientError("invalid_catalog", "Data API catalogue has no source list.")
                if name == "optical" and body.get("desktop_status") == "unavailable":
                    body = {**body, "sources": [{**row, "configured": False, "unavailable_reason": "desktop_unavailable"}
                        if isinstance(row, dict) and row.get("requires_desktop") else row for row in body["sources"]]}
                if name == "spectral":
                    for kind in ("multispectral", "hyperspectral"):
                        groups[kind] = {**body, "sources": [entry for entry in body["sources"] if isinstance(entry, dict) and entry.get("spectral_type") == kind]}
                else:
                    groups[name] = body
            except DataClientError as exc:
                for group in (("multispectral", "hyperspectral") if name == "spectral" else (name,)):
                    errors.append({"group": group, "code": exc.code, "status_code": exc.status_code})

        await asyncio.gather(*(discover_group(name) for name in names))
        if self.unified:
            try:
                unified = await self.unified.catalog()
                current = groups.get("optical", {})
                identifiers = {row["source"] for row in unified["sources"]}
                previous = [row for row in current.get("sources", []) if row.get("source", row.get("id")) not in identifiers]
                groups["optical"] = {**current, "sources": unified["sources"] + previous,
                    "unified_api": {key: value for key, value in unified.items() if key != "sources"}}
            except DataClientError as exc:
                errors.append({"group": "optical_unified", "code": exc.code, "status_code": exc.status_code})
        return self._with_availability({"mode": mode, "groups": groups, "errors": errors})

    async def discover(self) -> dict[str, Any]:
        return await self.catalog()

    async def execute(
        self, operation: str, parameters: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Account once at the acquisition boundary, including gateway jobs."""
        if operation in {"optical.latest", "multispectral.subset", "sar.crop"}:
            from .usage_context import imagery_reservation
            async with imagery_reservation():
                return await self._execute(operation, parameters, idempotency_key)
        return await self._execute(operation, parameters, idempotency_key)

    async def _execute(
        self, operation: str, parameters: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Run once; gateway jobs are polled, never replayed after failure."""
        if operation not in {*_WORKER_ROUTES, "optical.times"}:
            raise DataClientError("unsupported_operation", "Unsupported data API operation.")
        if not isinstance(parameters, dict):
            raise DataClientError("invalid_parameters", "Data operation parameters must be an object.")
        payload = dict(parameters)
        unified_operation = operation == "optical.times" or (operation == "optical.latest" and self.is_unified_source(payload.get("source")))
        if unified_operation and 'refresh' in payload and type(payload['refresh']) is not bool:
            raise DataClientError('invalid_parameters','实时刷新参数必须是布尔值。')
        if operation == 'optical.register':
            if set(payload) - {'source_image_id','reference_image_id','correct'} or any(not re.fullmatch(r'[0-9a-f]{32}', str(payload.get(k,''))) for k in ('source_image_id','reference_image_id')) or type(payload.get('correct',False)) is not bool:
                raise DataClientError('invalid_parameters','Registration requires two worker-owned image IDs and a boolean correction flag.')
        if operation.startswith(("multispectral.", "hyperspectral.")):
            multi = operation.startswith("multispectral.")
            payload.setdefault("source", "sentinel2" if multi else "emit")
            if not isinstance(payload["source"], str) or payload["source"] not in (_MULTI if multi else _HYPER):
                raise DataClientError("source_group_mismatch", "Data source does not belong to the selected API group.")
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", idempotency_key):
            raise DataClientError("invalid_idempotency_key", "Idempotency key must contain at most 96 safe ASCII characters.")
        group = operation.split(".", 1)[0]
        source = None if operation == 'optical.register' else payload.get("source", "bing" if group == "optical" else "sentinel1-rtc" if group == "sar" else None)
        if source is not None and not isinstance(source, str):
            raise DataClientError("invalid_parameters", "Data source must be a string.")
        if source and source in (self.source_policy or {}).get("disabled_sources", []):
            raise DataClientError("source_administrator_disabled", "此影像来源已由系统管理员停用。", 403)
        if self.source_registry and source:
            period = payload.get("datetime")
            if unified_operation:
                period = payload.get("date") or (str(payload["year"]) if payload.get("year") else None)
            if group == "sar" and payload.get("start") and payload.get("end"):
                period = str(payload["start"]) + "/" + str(payload["end"])
            route = self.route(group, operation, bbox=payload.get("bbox"), time_range=period,
                               catalog={"groups": {group: {"sources": [{"source": source}]}}})
            if route["blocked"]:
                blocks=route['blocked']
                if unified_operation and operation=='optical.latest' and payload.get('refresh') is True:
                    # Refresh only a known transient cooldown. It never grants
                    # access to an admin-disabled, unsupported or auth-blocked source.
                    transient={'unified_api_rejected','unified_provider_render_failed','unified_timeout',
                               'unified_unavailable','data_deadline_exceeded','-1','history_source_error'}
                    blocks=[item for item in blocks if not (item.get('state')=='failed' and item.get('reason') in transient)]
                blocked = blocks[0] if blocks else None
            else:blocked=None
            if blocked is not None:
                evidence = blocked["evidence"]
                reason = blocked["reason"] if _SAFE_CODE.fullmatch(blocked["reason"] or "") else "saved_capability_failure"
                expiry = evidence.get("expires_at", "unknown")
                raise DataClientError(
                    "source_cached_blocked",
                    f"{source} / {operation} 暂停重复请求：{reason}；缓存有效至 {expiry}。"
                    "这不代表其他区域或时段永久不可用。账户或服务恢复后等待缓存到期，或由管理员显式重新核验并失效旧记录；"
                    "catalog(force_refresh=True) 只更新目录，不绕过提供商权限。", 409,
                )
        if unified_operation:
            if not self.unified:
                raise DataClientError("unified_credentials_required", "新影像接口尚未配置服务端 API key。", 401)
            try:
                async with asyncio.timeout(self.max_wait_seconds):
                    return await (self.unified.times(payload) if operation == "optical.times" else self.unified.image(payload))
            except TimeoutError:
                raise DataClientError("data_deadline_exceeded", "新影像请求等待或取图超过总预算。") from None
        try:
            async with asyncio.timeout(self.max_wait_seconds):
                mode = await self._resolve_mode()
                if mode == "gateway":
                    if operation == 'optical.register':
                        raise DataClientError('registration_requires_worker','Local image registration requires the original worker; cross-node image IDs are not forwarded.')
                    return await self._execute_gateway(operation, payload, idempotency_key)
                method, path = _WORKER_ROUTES[operation]
                if operation == "sar.preview":
                    payload.pop("sign", None)
                content, media, status = await self._read(
                    method, path, payload=payload if method == "POST" else None,
                    params=payload if method == "GET" else None,
                    maximum=32 * 1024 * 1024 if operation == "sar.preview" else 5 * 1024 * 1024,
                )
                if status == 202:
                    # A gateway's compatibility endpoints may also return 202.
                    return await self._poll(self._object(content))
                if operation == "sar.preview":
                    if media.split(";")[0] not in {"image/png", "image/jpeg"}:
                        raise DataClientError("invalid_preview", "SAR preview endpoint did not return an image.")
                    relative_url = path + "?" + urlencode(payload)
                    self._asset_path(relative_url)
                    return {"file_url": relative_url, "media_type": media, "preview_is_analysis_data": False}
                return self._object(content)
        except TimeoutError:
            raise DataClientError("data_deadline_exceeded", "Data operation exceeded its deadline; an accepted gateway job may still finish. Do not automatically resubmit it.") from None

    async def _execute_gateway(self, operation: str, payload: dict, key: str | None) -> dict:
        content, _, status = await self._read(
            "POST", "/api/jobs", payload={"operation": operation, "parameters": payload, "deadline_seconds": max(5, min(600, math.ceil(self.max_wait_seconds)))},
            headers={"Idempotency-Key": key} if key else None,
        )
        if status != 202:
            raise DataClientError("invalid_job_response", "Gateway did not acknowledge an asynchronous job.")
        return await self._poll(self._object(content))

    async def _poll(self, accepted: dict) -> dict:
        job_id = accepted.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise DataClientError("invalid_job_response", "Gateway returned an invalid job identifier.")
        status_path = accepted.get("status_url")
        expected_path = "/api/jobs/" + job_id
        if status_path != expected_path or not _JOB_PATH.fullmatch(status_path):
            raise DataClientError("unsafe_job_url", "Gateway returned an unsupported job status path.")
        while True:
            content, _, _ = await self._read("GET", status_path)
            status = self._object(content)
            state = status.get("state")
            if status.get("job_id") != job_id:
                raise DataClientError("invalid_job_response", "Gateway status belongs to a different job.")
            if state == "succeeded":
                result = status.get("result")
                if not isinstance(result, dict):
                    raise DataClientError("invalid_job_result", "Completed data job has no JSON result.")
                return {**result, "_gateway": {"job_id": job_id, "state": state, "node": status.get("node")}}
            if state == "failed":
                failure = status.get("error")
                candidate = failure.get("code") if isinstance(failure, dict) else None
                code = candidate if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate) else "data_job_failed"
                raise DataClientError(code, "Gateway data job failed; the request was not replayed.")
            if state not in {"queued", "running"}:
                raise DataClientError("invalid_job_state", "Gateway returned an unsupported job state.")
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _asset_path(relative_url: str) -> str:
        if not isinstance(relative_url, str):
            raise DataClientError("unsafe_asset_url", "Artifact URL must be a local API path.")
        try:
            parsed = urlsplit(relative_url)
        except ValueError:
            raise DataClientError("unsafe_asset_url", "Artifact URL must be a valid local API path.") from None
        if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in relative_url or any(ord(c) < 32 for c in relative_url):
            raise DataClientError("unsafe_asset_url", "Artifacts must use local API paths on the configured data origin.")
        if _FILE_PATH.fullmatch(parsed.path) and not parsed.query:
            return relative_url
        if parsed.path == "/api/sar/preview":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (set(query) == {"source", "item_id"} and all(len(v) == 1 for v in query.values())
                    and query["source"][0] in _SAR_SOURCES and _ITEM_ID.fullmatch(query["item_id"][0])):
                return relative_url
        raise DataClientError("unsafe_asset_url", "Artifact path is outside the supported local file endpoints.")

    async def download(self, relative_url: str) -> tuple[bytes, str]:
        """Fetch an allowlisted local artifact without redirects or URL secrets."""
        if isinstance(relative_url, str) and relative_url.startswith("/api/unified/"):
            if not self.unified:
                raise DataClientError("unified_artifact_missing", "新影像接口会话未配置。", 404)
            return self.unified.download(relative_url)
        path = self._asset_path(relative_url)
        try:
            async with asyncio.timeout(self.max_wait_seconds):
                content, media, _ = await self._read("GET", path, maximum=self.max_download_bytes)
                return content, media
        except TimeoutError:
            raise DataClientError("data_deadline_exceeded", "Artifact download exceeded the configured deadline.") from None
