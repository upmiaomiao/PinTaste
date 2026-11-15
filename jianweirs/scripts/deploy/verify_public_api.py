"""Verify a real public gateway, without printing credentials or signed URLs.

Uses stdlib HTTP with strict TLS and no redirects; image/science verification
uses Pillow and rasterio from the deployment runtime. Exit: 0 passed, 1 failed
checks, 2 invalid invocation/private configuration. Science crops run by default.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import re
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OPTICAL_SOURCES = {"bing", "amap", "tencent", "google", "mapbox", "mapquest", "arcgis", "tianditu", "siwei", "siwei_daily"}
ROI = [104.046, 30.656, 104.054, 30.664]
SEARCH_BOX = [104.04, 30.65, 104.06, 30.67]
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
SCENE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,239}$")
SAFE_ERROR_CODES = {
    "worker_rejected", "worker_timeout_or_connection", "gateway_execution_failed", "invalid_worker_result",
    "gateway_restarted", "gateway_stopped", "queue_full", "source_not_configured", "submission_rate_limit",
    "omap_timeout", "omap_unavailable", "omap_auth_failed", "omap_permission_denied", "omap_capture_failed",
    "omap_incomplete_image", "spectral_provider_unavailable", "spectral_provider_timeout", "spectral_provider_error",
    "spectral_raster_read_failed", "no_valid_pixels", "sar_provider_unavailable", "sar_provider_throttled",
    "sar_roi_no_data", "sar_crop_failed", "planetary_signing_error", "planetary_signing_timeout",
    "provider_authentication_required", "invalid_parameters", "request_too_large", "storage_limit",
    "invalid_worker_file", "worker_response_too_large", "file_not_ready", "file_expired", "job_not_found",
    "node_unavailable", "optical_provider_error", "invalid_image", "no_node_available", "gateway_timeout",
    "same_point_registration_unverified", "coordinate_conversion_required",
}


class VerificationError(Exception):
    def __init__(self, code: str, fields: dict | None = None):
        super().__init__(code)
        self.code, self.fields = code, fields or {}


@dataclass
class Reply:
    status: int
    content: bytes
    content_type: str = "application/json"

    def json(self) -> dict:
        try:
            result = json.loads(self.content)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError, UnicodeError):
            raise VerificationError("invalid_json_response") from None


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise VerificationError("public_https_origin_required")
        self.base_url = base_url.rstrip("/")
        self._api_key, self.transport = api_key, transport
        self.opener = build_opener(NoRedirects(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method: str, path: str, payload: dict | None = None, *, authenticated=True,
                maximum=5 * 1024 * 1024, timeout=25) -> Reply:
        # A result-controlled absolute URL/redirect must never receive a key.
        if not path.startswith("/api/") or "?" in path or "#" in path or "\\" in path or "/../" in path or "//" in path:
            raise VerificationError("unsafe_gateway_path")
        headers = {"Accept": "application/json", "ngrok-skip-browser-warning": "1"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self._api_key
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.transport is not None:
            reply = self.transport(method, path, payload, headers)
            if len(reply.content) > maximum:
                raise VerificationError("response_too_large")
            return reply
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=timeout)
            except HTTPError as exc:
                response = exc
            with response:
                content = response.read(maximum + 1)
                if len(content) > maximum:
                    raise VerificationError("response_too_large")
                return Reply(response.status, content, response.headers.get("Content-Type", "").split(";")[0])
        except VerificationError:
            raise
        except (URLError, OSError, TimeoutError, ValueError):
            # URL/certificate/exception strings can contain credentials; report
            # a fixed code and leave private transport diagnostics out of JSON.
            raise VerificationError("public_connection_failed") from None


def safe_error(value: Any, depth: int = 0) -> dict:
    if not isinstance(value, dict):
        return {"code": "unspecified_upstream_error"}
    code = value.get("code")
    result = {"code": code if code in SAFE_ERROR_CODES else "unrecognized_upstream_error"}
    for field in ("upstream_status", "upstream_code"):
        number = value.get(field)
        if isinstance(number, int) and not isinstance(number, bool) and -999 <= number <= 599:
            result[field] = number
    if depth < 3 and isinstance(value.get("detail"), dict):
        result["detail"] = safe_error(value["detail"], depth + 1)
    return result


def load_private_configuration(path: Path) -> tuple[str, list[str]]:
    try:
        if path.is_symlink():
            raise ValueError
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        key = config["clients"][0]["api_key"]
        if not isinstance(key, str) or len(key) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError
        secrets = [item["api_key"] for group in ("clients", "nodes") for item in config.get(group, [])
                   if isinstance(item, dict) and isinstance(item.get("api_key"), str) and item["api_key"]]
        return key, secrets
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise VerificationError("invalid_private_cluster_configuration") from None


def _ring_inside(x: float, y: float, ring: list) -> bool:
    inside = False
    for a, b in zip(ring, ring[1:]):
        x1, y1 = a[:2]
        x2, y2 = b[:2]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _segment_inside_rectangle(a, b, bbox) -> bool:
    # Liang-Barsky segment clipping against a rectangle very slightly inside
    # the ROI. Any footprint boundary through its interior invalidates full
    # containment, including a hole entirely within otherwise covered corners.
    west, south, east, north = bbox
    epsilon = min(east - west, north - south) * 1e-8
    west, south, east, north = west + epsilon, south + epsilon, east - epsilon, north - epsilon
    low, high = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - west), (dx, east - a[0]), (-dy, a[1] - south), (dy, north - a[1])):
        if p == 0:
            if q < 0:
                return False
        elif p < 0:
            low = max(low, q / p)
        else:
            high = min(high, q / p)
        if low > high:
            return False
    return True


def geometry_contains_roi(geometry: Any, bbox: list[float]) -> bool:
    """Conservative Polygon/MultiPolygon full containment, including holes."""
    try:
        kind = geometry["type"]
        polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"] if kind == "MultiPolygon" else []
        west, south, east, north = bbox
        corners = [(west, south), (east, south), (east, north), (west, north)]
        for rings in polygons:
            if not rings:
                continue
            for ring in rings:
                if len(ring) < 4 or ring[0][:2] != ring[-1][:2]:
                    raise ValueError
                if any(len(p) < 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in p[:2]) for p in ring):
                    raise ValueError
            if not all(_ring_inside(x, y, rings[0]) and not any(_ring_inside(x, y, hole) for hole in rings[1:]) for x, y in corners):
                continue
            if any(_segment_inside_rectangle(a, b, bbox) for ring in rings for a, b in zip(ring, ring[1:])):
                continue
            return True
    except (KeyError, TypeError, IndexError, ValueError, OverflowError, ZeroDivisionError):
        return False
    return False


def scene_summary(scene: dict) -> dict:
    result = {}
    value = scene.get("id")
    if isinstance(value, str) and SCENE_ID.fullmatch(value):
        result["scene_id"] = value
    value = scene.get("acquired_at")
    if isinstance(value, str):
        try:
            result["acquired_at"] = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return result


def inspect_png(payload: bytes) -> dict:
    try:
        from PIL import Image
        from omap_api.protocol import detect_ovital_placeholder
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != (512, 512):
                raise VerificationError("optical_png_dimensions_invalid")
            image.load()
            colors = len(image.convert("RGB").getcolors(512 * 512) or [])
        placeholder = detect_ovital_placeholder(payload)["detected"]
        if placeholder:
            raise VerificationError("optical_gray_grid_detected")
        if colors <= 1000:
            raise VerificationError("optical_image_content_insufficient")
        return {"dimensions": [512, 512], "unique_colors": colors, "known_gray_grid_detected": False,
                "decoded": True, "cloud_free_verified": False, "acquisition_date_verified": False}
    except VerificationError:
        raise
    except ImportError:
        raise VerificationError("image_verification_dependency_missing") from None
    except Exception:
        raise VerificationError("optical_image_decode_failed") from None


def inspect_geotiff(payload: bytes, expected_bands: list[str]) -> dict:
    try:
        import numpy as np
        from rasterio.io import MemoryFile
        with MemoryFile(payload) as mem:
            with mem.open() as raster:
                if raster.count != len(expected_bands) or raster.crs is None or raster.width < 16 or raster.height < 16 or raster.width * raster.height > 4_000_000:
                    raise VerificationError("scientific_raster_structure_invalid")
                if any(dtype != "float32" for dtype in raster.dtypes):
                    raise VerificationError("scientific_raster_dtype_invalid")
                if raster.descriptions and any(raster.descriptions) and [str(x).lower() for x in raster.descriptions] != [x.lower() for x in expected_bands]:
                    raise VerificationError("scientific_band_order_invalid")
                data = raster.read(masked=True)
                valid = ~np.ma.getmaskarray(data)
                finite = np.isfinite(data.filled(np.nan))
                fractions = [float((valid[i] & finite[i]).mean()) for i in range(raster.count)]
                if min(fractions) < .95:
                    raise VerificationError("scientific_roi_incomplete", {"valid_fraction_per_band": fractions})
                values = data.compressed()
                if values.size == 0 or not np.isfinite(values).all() or float(values.max()) <= float(values.min()):
                    raise VerificationError("scientific_values_invalid")
                transform = raster.transform
                if not all(math.isfinite(v) for v in list(transform)) or transform.a == 0 or transform.e == 0 or transform.is_identity:
                    raise VerificationError("scientific_georeferencing_invalid")
                return {"decoded": True, "bands": expected_bands, "band_count": raster.count,
                        "dimensions": [raster.width, raster.height], "crs": str(raster.crs),
                        "valid_fraction_per_band": fractions, "value_min": float(values.min()),
                        "value_max": float(values.max()), "dtype": "float32", "georeferenced": True,
                        "cloud_free_verified": False}
    except VerificationError:
        raise
    except ImportError:
        raise VerificationError("scientific_verification_dependency_missing") from None
    except Exception:
        raise VerificationError("scientific_raster_decode_failed") from None


class PublicVerifier:
    def __init__(self, client: GatewayClient, output: Path, *, optical_source="bing", optical_sources=None, run_science_crops=True,
                 poll_timeout=240.0, poll_interval=2.0, sleep=time.sleep, monotonic=time.monotonic):
        selected_sources = list(dict.fromkeys(optical_sources or [optical_source]))
        if not selected_sources or any(source not in OPTICAL_SOURCES for source in selected_sources) or not 1 <= poll_timeout <= 600 or not 0 <= poll_interval <= 30:
            raise VerificationError("invalid_verification_options")
        self.client, self.output, self.optical_source = client, Path(output), optical_source
        self.optical_sources = selected_sources
        self.run_science_crops, self.poll_timeout, self.poll_interval = run_science_crops, poll_timeout, poll_interval
        self.sleep, self.monotonic = sleep, monotonic
        self.checks = []
        self.artifact_dir = self.output.parent / ("public-verification-artifacts-" + uuid.uuid4().hex[:12])

    def record(self, category, name, passed, *, required=True, **fields):
        entry = {"category": category, "name": name, "passed": bool(passed), "required": required, **fields}
        self.checks.append(entry)
        print(f"CHECK {category}/{name}: {'PASS' if passed else 'SKIP' if fields.get('skipped') else 'FAIL'}", flush=True)
        return entry

    def attempt(self, category, name, operation, *, required=True):
        try:
            result = operation()
            self.record(category, name, True, required=required, **(result if isinstance(result, dict) else {}))
            return result
        except VerificationError as exc:
            self.record(category, name, False, required=required, error={"code": exc.code, **exc.fields})
        except Exception:
            self.record(category, name, False, required=required, error={"code": "verification_internal_error"})
        return None

    def json_request(self, method, path, payload=None, expected=200):
        reply = self.client.request(method, path, payload)
        if reply.status != expected:
            try:
                detail = safe_error(reply.json().get("detail"))
            except VerificationError:
                detail = {"code": "non_json_error_response"}
            raise VerificationError("gateway_http_error", {"http_status": reply.status, "upstream": detail})
        return reply.json()

    def run_job(self, path: str, payload: dict) -> tuple[str, dict]:
        accepted = self.json_request("POST", path, payload, 202)
        job = accepted.get("job_id")
        if not isinstance(job, str) or not JOB_ID.fullmatch(job):
            raise VerificationError("invalid_gateway_job_id")
        end = self.monotonic() + self.poll_timeout
        while self.monotonic() < end:
            status = self.json_request("GET", "/api/jobs/" + job)
            if status.get("job_id", job) != job:
                raise VerificationError("gateway_job_id_mismatch")
            state = status.get("state")
            if state == "succeeded":
                result = status.get("result")
                if not isinstance(result, dict):
                    raise VerificationError("invalid_gateway_job_result")
                return job, result
            if state == "failed":
                raise VerificationError("gateway_job_failed", {"upstream": safe_error(status.get("error"))})
            if state not in {"queued", "running"}:
                raise VerificationError("invalid_gateway_job_state")
            self.sleep(self.poll_interval)
        raise VerificationError("gateway_poll_timeout")

    def job_file(self, job: str, result: dict) -> bytes:
        file_url = result.get("image_url") or result.get("file_url")
        expected = "/api/jobs/" + job + "/file"
        if file_url != expected:
            raise VerificationError("unsafe_gateway_file_url")
        reply = self.client.request("GET", expected, maximum=64 * 1024 * 1024, timeout=60)
        if reply.status != 200:
            raise VerificationError("gateway_file_download_failed", {"http_status": reply.status})
        return reply.content

    def save_artifact(self, name: str, payload: bytes):
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / name).write_bytes(payload)
        return self.artifact_dir.name + "/" + name

    def optical(self, source=None):
        source = source or self.optical_source
        job, result = self.run_job("/api/optical/latest", {"source": source,
            "bbox": [116.446, 39.849, 116.456, 39.859], "coord_type": 0,
            "zoom": 16, "width": 512, "height": 512, "timeout_seconds": 120})
        data = self.job_file(job, result)
        evidence = inspect_png(data)
        return {"source": source, "bytes": len(data), **evidence,
                "artifact": self.save_artifact("optical-" + source + ".png", data)}

    def catalog(self, group: str):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=366)).date().isoformat()
        finish = now.date().isoformat()
        body = {"source": "sentinel2", "bbox": SEARCH_BOX, "datetime": start + "/" + finish, "limit": 20} if group == "multispectral" else {
            "source": "sentinel1-rtc", "bbox": SEARCH_BOX, "start": start + "T00:00:00Z", "end": finish + "T23:59:59Z", "limit": 20, "polarization": "VV+VH"}
        _, result = self.run_job("/api/" + group + "/search", body)
        raw = result.get("scenes" if group == "multispectral" else "items")
        if not isinstance(raw, list) or not raw:
            raise VerificationError("no_scene_in_recent_year")
        scenes = [s for s in raw if isinstance(s, dict) and scene_summary(s).get("scene_id")
                  and geometry_contains_roi(s.get("geometry"), ROI)]
        if group == "sar":
            scenes = [s for s in scenes if {"vv", "vh"} <= set(s.get("assets", {}))
                      and {"VV", "VH"} <= {str(p).upper() for p in s.get("polarizations", [])}]
        else:
            scenes = [s for s in scenes if {"B02", "B03", "B04", "B08"} <= set(s.get("assets", {}))]
        if not scenes:
            raise VerificationError("no_complete_roi_scene_in_returned_page", {"returned_scene_count": len(raw)})
        # Prefer low cloud in actual optical results; preserve returned date
        # order for SAR. A scene footprint hit never certifies cloud-free pixels.
        if group == "multispectral":
            scenes.sort(key=lambda s: s.get("cloud_cover") if isinstance(s.get("cloud_cover"), (int, float)) else 101)
        return {"returned_scene_count": len(raw), "complete_roi_candidate_count": len(scenes),
                "recent_year_start": start, "recent_year_end": finish,
                "selected_scene": scene_summary(scenes[0]), "science_data_downloaded": False}, scenes

    def science_crop(self, group: str, scenes: list):
        candidates = scenes[:3]
        failures = []
        for scene in candidates:
            try:
                if group == "multispectral":
                    bands = ["B02", "B03", "B04", "B08"]
                    path = "/api/multispectral/subset"
                    body = {"source": "sentinel2", "scene_id": scene["id"], "bbox": ROI, "assets": bands, "width": 128, "height": 128}
                else:
                    bands = ["VV", "VH"]
                    path = "/api/sar/crop"
                    body = {"source": "sentinel1-rtc", "item_id": scene["id"], "bbox": ROI, "polarizations": ["vv", "vh"]}
                job, result = self.run_job(path, body)
                data = self.job_file(job, result)
                evidence = inspect_geotiff(data, bands)
                return {"source": "sentinel2" if group == "multispectral" else "sentinel1-rtc",
                        **scene_summary(scene), "bytes": len(data), "science_data_downloaded": True,
                        "previous_candidate_failures": failures, **evidence,
                        "artifact": self.save_artifact(group + ".tif", data)}
            except VerificationError as exc:
                failures.append({**scene_summary(scene), "error": {"code": exc.code, **exc.fields}})
        raise VerificationError("science_candidates_failed", {"candidate_failures": failures})

    def run(self) -> dict:
        def anonymous():
            status = self.client.request("GET", "/api/nodes", authenticated=False).status
            if status != 401:
                raise VerificationError("anonymous_nodes_not_rejected", {"http_status": status})
            return {"http_status": status}
        self.attempt("public_ingress", "anonymous_nodes_rejected", anonymous)
        def nodes():
            result = self.json_request("GET", "/api/nodes")
            entries = result.get("nodes")
            if not isinstance(entries, list) or not entries:
                raise VerificationError("no_gateway_nodes")
            online = sum(isinstance(n, dict) and n.get("http_online") is True for n in entries)
            ready = sum(isinstance(n, dict) and n.get("http_online") is True and n.get("desktop_ready") is True for n in entries)
            desktop_required = any(source != "arcgis" for source in self.optical_sources)
            if not online or (desktop_required and not ready):
                raise VerificationError("no_ready_desktop_node", {"enabled_nodes": len(entries), "online_nodes": online, "desktop_ready_nodes": ready})
            return {"enabled_nodes": len(entries), "online_nodes": online, "desktop_ready_nodes": ready,
                    "desktop_required_for_requested_sources": desktop_required,
                    "all_configured_nodes_online": online == len(entries), "three_physical_nodes_verified": False}
        self.attempt("public_ingress", "authorized_node_status", nodes)
        for group in ("optical", "multispectral", "hyperspectral", "sar"):
            def sources(name=group):
                result = self.json_request("GET", "/api/" + name + "/sources")
                entries = result.get("sources")
                if not isinstance(entries, list) or not entries:
                    raise VerificationError("source_catalogue_empty")
                return {"source_count": len(entries)}
            self.attempt("public_ingress", group + "_sources", sources)
        for source in self.optical_sources:
            self.attempt("optical", source + "_decoded_real_png", lambda s=source: self.optical(s))
        for group in ("multispectral", "sar"):
            selected = []
            def catalogue(name=group):
                fields, scenes = self.catalog(name)
                selected.extend(scenes)
                return fields
            result = self.attempt("catalog", group + "_recent_scene_search", catalogue)
            if self.run_science_crops:
                if result is None:
                    self.record("science", group + "_numerical_geotiff", False, error={"code": "scene_selection_failed"})
                else:
                    self.attempt("science", group + "_numerical_geotiff", lambda g=group, s=selected: self.science_crop(g, s))
            else:
                self.record("science", group + "_numerical_geotiff", False, required=False,
                            skipped=True, error={"code": "explicit_catalogue_only_mode"})
        categories = {}
        for group in ("public_ingress", "optical", "catalog", "science"):
            checks = [c for c in self.checks if c["category"] == group and c["required"]]
            categories[group] = {"passed": bool(checks) and all(c["passed"] for c in checks), "required_checks": len(checks),
                                 "passed_checks": sum(c["passed"] for c in checks), "skipped": not checks}
        required = [c for c in self.checks if c["required"]]
        return {"schema_version": 1, "tested_at": datetime.now(timezone.utc).isoformat(),
                "public_origin": self.client.base_url, "scope": "Requests issued by this machine to the specified HTTPS public gateway; external independent network not verified",
                "optical_sources_tested": self.optical_sources,
                "science_crops_requested": self.run_science_crops, "all_three_requested_product_groups_verified": all(c["passed"] for c in required) and self.run_science_crops,
                "passed": all(c["passed"] for c in required), "pass_count": sum(c["passed"] for c in required),
                "total_checks": len(required), "categories": categories, "checks": self.checks,
                "limitations": ["目录命中不等于无云或科学数据下载。", "检测已知奥维灰网格不等于全面影像质量保证。", "仅测试当前请求地点；未验证所有来源、账户权限和其他物理节点。"]}


def write_report(path: Path, report: dict, secrets: list[str]):
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            encoded = encoded.replace(secret, "[REDACTED]")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    temp.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="通过真实 HTTPS 公网地址验证高分、多光谱、SAR 及科学文件；不输出密钥。")
    parser.add_argument("--base-url", required=True, help="实际 HTTPS 公网网关 origin")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "public-verification.json")
    parser.add_argument("--cluster-config", type=Path, default=ROOT / "cluster.json")
    parser.add_argument("--optical-source", choices=sorted(OPTICAL_SOURCES), action="append", help="可重复指定，每个来源单独验收；默认 bing")
    parser.add_argument("--optical-sources", help="逗号分隔来源列表，例如 bing,amap,tencent,arcgis")
    parser.add_argument("--run-science-crops", action=argparse.BooleanOptionalAction, default=True,
                        help="默认实际验证 Sentinel-2 四波段和 SAR VV/VH GeoTIFF；--no-run-science-crops 仅检索目录")
    args = parser.parse_args(argv)
    secrets = []
    try:
        if args.output.resolve() == args.cluster_config.resolve():
            print("Report destination cannot replace private cluster configuration.", file=sys.stderr)
            return 2
        selected = list(args.optical_source or [])
        if args.optical_sources:
            selected.extend(source.strip() for source in args.optical_sources.split(","))
        selected = selected or ["bing"]
        key, secrets = load_private_configuration(args.cluster_config)
        client = GatewayClient(args.base_url, key)
        report = PublicVerifier(client, args.output, optical_sources=selected,
                                run_science_crops=args.run_science_crops).run()
        exit_code = 0 if report["passed"] else 1
    except VerificationError as exc:
        report = {"schema_version": 1, "tested_at": datetime.now(timezone.utc).isoformat(), "passed": False,
                  "configuration_error": {"code": exc.code}, "checks": [], "pass_count": 0, "total_checks": 0}
        exit_code = 2
    try:
        write_report(args.output, report, secrets)
    except OSError:
        print("Verification report could not be written.", file=sys.stderr)
        return 2
    print(f"PUBLIC_VERIFICATION {'PASS' if report['passed'] else 'FAIL'} {report['pass_count']}/{report['total_checks']}; exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
