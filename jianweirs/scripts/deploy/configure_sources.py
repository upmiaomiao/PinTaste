"""Discover public source->local MapType overrides without editing private envs.

Names establish a conservative configuration suggestion, never download rights,
imagery availability, registration accuracy, or a successful capture.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlsplit
import uuid

from dotenv import dotenv_values
import httpx


SOURCES = ("bing", "amap", "tencent", "google", "mapbox", "mapquest",
           "tianditu", "siwei", "siwei_daily")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_PROVIDERS = {
    "bing": r"必应|(?<![a-z])bing(?:\s*aerial)?(?![a-z])",
    "amap": r"高德|(?<![a-z])(?:amap|gaode)(?![a-z])",
    "tencent": r"腾讯|(?<![a-z])tencent(?![a-z])",
    "google": r"谷歌|(?<![a-z])(?:google|gg)(?![a-z])",
    "mapbox": r"(?<![a-z])map\s*box(?![a-z])",
    "mapquest": r"(?<![a-z])map\s*quest(?![a-z])",
    "tianditu": r"天地图|(?<![a-z])tianditu(?![a-z])",
    "siwei": r"四维(?:地球)?|(?<![a-z])siwei(?![a-z])",
    "arcgis": r"(?<![a-z])(?:arcgis|esri)(?![a-z])",
}
_NO_OVERLAY = r"无字(?:版)?|无标注|无标签|无路网|不带标注|不含文字|纯卫星|纯影像|(?:no\s*labels?|unlabelled|unlabeled)"
_OVERLAY = r"混合|路网|道路|标注|地名|标签|有字|小字|大字|(?<![a-z])(?:hybrid|labels?|roads?|streets?)(?![a-z])"
_IMAGERY = r"卫星|影像|(?<![a-z])(?:satellite|aerial|imagery)(?![a-z])"


class DiscoveryError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", unicodedata.normalize("NFKC", name).casefold()).strip()


def _safe_record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    map_id, name = raw.get("id"), raw.get("name")
    if isinstance(map_id, bool) or not isinstance(map_id, int) or not 1 <= map_id <= 2**31 - 1:
        return None
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        return None
    # Never copy arbitrary provider configuration or credentials into reports.
    if any(ord(char) < 32 for char in name) or re.search(r"://|www\.|[?=]|(?:token|psk|api[_ -]?key)\b", name, re.I):
        return None
    record = {"map_type": map_id, "name": name.strip()}
    for key in ("min_zoom", "max_zoom", "coord_type"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 64:
            record[key] = value
    return record


def _match(source: str, record: dict) -> tuple[int, str] | None:
    text = _normalize(record["name"])
    provider = "siwei" if source == "siwei_daily" else source
    if not re.search(_PROVIDERS[provider], text):
        return None
    found = {key for key, pattern in _PROVIDERS.items() if re.search(pattern, text)}
    allowed = {provider, "tianditu"} if source in {"mapbox", "mapquest"} else {provider}
    if found - allowed:
        return 0, "multiple_providers"
    if re.search(r"电子图|电子地图|地形|矢量|等高线|(?<![a-z])(?:terrain|vector|dem|firefly)(?![a-z])", text):
        return 0, "different_map_product"
    daily = bool(re.search(r"日新|每日|(?<![a-z])daily(?![a-z])", text))
    if provider == "siwei" and (source == "siwei_daily") != daily:
        return 0, "different_siwei_product"
    if not re.search(_IMAGERY, text) and not (source == "siwei_daily" and daily):
        return 0, "imagery_not_explicit"
    if re.search(r"历史|时序|(?<![a-z])(?:history|historical|archive)(?![a-z])", text):
        return 0, "historical_product_not_current_mosaic"
    if source in {"mapbox", "mapquest"}:
        # Existing named APIs describe this specific imagery+Tianditu-roads
        # composite. A pure product or another overlay is a different source.
        if not re.search(r"天地图.*(?:路网|道路)|天地路网|tianditu.*(?:road|street)", text):
            return 0, "expected_tianditu_road_composite_not_explicit"
        if re.search(_NO_OVERLAY, text):
            return 0, "contradictory_composite_name"
        return (200, "explicit_tianditu_road_composite") if re.search(r"混合|hybrid", text) else (100, "tianditu_road_composite")
    # "hybrid (no labels)" remains contradictory; it is not silently treated
    # as pure imagery. Only explicitly negated overlay terms are stripped.
    stripped = re.sub(_NO_OVERLAY, " ", text)
    if re.search(_OVERLAY, stripped):
        return 0, "overlay_not_pure_imagery"
    if re.search(_NO_OVERLAY, text):
        return 200, "explicit_unlabelled_imagery"
    if source == "bing" and re.search(r"(?<![a-z])bing\s*aerial(?![a-z])", text):
        return 200, "named_bing_aerial_product"
    return 100, "provider_imagery_without_overlay_in_name"


def discover_sources(maps: list[object]) -> tuple[dict[str, int], dict]:
    """Return a bare override mapping plus a public, non-verifying report."""
    records, invalid = [], 0
    for raw in maps:
        record = _safe_record(raw)
        if record is None:
            invalid += 1
        elif record not in records:
            records.append(record)
    by_id: dict[int, list[dict]] = {}
    for record in records:
        by_id.setdefault(record["map_type"], []).append(record)
    conflicts = {map_id for map_id, items in by_id.items() if len(items) > 1}
    mapping, findings = {}, []
    for source in SOURCES:
        candidates = []
        for record in records:
            match = _match(source, record)
            if match is None:
                continue
            score, reason = match
            if record["map_type"] in conflicts:
                score, reason = 0, "conflicting_records_for_map_type"
            candidates.append({**record, "score": score, "reason": reason})
        candidates.sort(key=lambda item: (-item["score"], item["map_type"], item["name"]))
        positive = [item for item in candidates if item["score"] > 0]
        best = [item for item in positive if item["score"] == positive[0]["score"]] if positive else []
        finding = {"source": source, "status": "unmapped", "reason": "no_semantically_clear_candidate", "candidates": candidates}
        if len(best) == 1:
            chosen = best[0]
            mapping[source] = chosen["map_type"]
            finding.update(status="matched", reason="unique_highest_semantic_match",
                           map_type=chosen["map_type"], name=chosen["name"], match_score=chosen["score"])
        elif len(best) > 1:
            finding["reason"] = "ambiguous_highest_semantic_match"
        findings.append(finding)
    matched = sorted(mapping)
    report = {
        "schema_version": 1,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "basis": "local_map_names_only",
        "capture_verified": False,
        "availability": "not_tested",
        "maps_seen": len(maps),
        "invalid_or_unsafe_map_count": invalid,
        "source_map_types": mapping,
        "matched_sources": matched,
        "unmapped_sources": sorted(set(SOURCES) - set(mapping)),
        "gateway_optical_sources": sorted([*matched, "arcgis"]),
        "external_sources": [{"source": "arcgis", "transport": "esri_rest", "availability": "not_tested",
                              "basis": "independent_public_service_not_a_local_map_match"}],
        "sources": findings,
        "note": "Map names only; capture success, permissions, freshness and registration have not been verified. Unmapped sources must not be enabled through fallback IDs.",
    }
    return mapping, report


def _local_origin(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        is_loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
        port = parsed.port
        if (parsed.scheme != "http" or not is_loopback or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or "?" in base_url or "#" in base_url
                or port is not None and not 1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise DiscoveryError("invalid_local_base_url", "Use the local worker HTTP loopback origin without credentials or paths.") from None
    return "http://" + parsed.netloc


def fetch_maps(base_url: str, worker_config: Path, *, transport=None) -> list[object]:
    origin = _local_origin(base_url)
    try:
        values = dotenv_values(worker_config, encoding="utf-8-sig", interpolate=False)
    except (OSError, ValueError, UnicodeError):
        raise DiscoveryError("worker_config_unavailable", "Cannot read the local worker configuration.") from None
    key = values.get("OMAP_WORKER_API_KEY") or ""
    if not 32 <= len(key) <= 4096 or any(not 33 <= ord(char) <= 126 for char in key):
        raise DiscoveryError("worker_key_missing_or_invalid", "The local worker API key is missing or invalid.")
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", origin + "/api/maps", headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code in {401, 403}:
                    raise DiscoveryError("worker_authentication_failed", "The local worker rejected authentication.")
                if response.status_code != 200:
                    raise DiscoveryError("worker_maps_unavailable", "The local worker could not provide its map list.")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise DiscoveryError("worker_maps_too_large", "The worker map list exceeds the discovery size limit.")
        result = json.loads(content)
        maps = result.get("maps") if isinstance(result, dict) else None
        if not isinstance(maps, list) or len(maps) > 10000:
            raise ValueError
        return maps
    except (httpx.HTTPError, OSError):
        raise DiscoveryError("worker_connection_failed", "Cannot connect to the local worker.") from None
    except (ValueError, UnicodeError):
        raise DiscoveryError("invalid_worker_maps", "The local worker returned an invalid map list.") from None


def write_reports(output: Path, mapping: dict, report: dict) -> tuple[Path, Path]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    targets = (output / "source-map-types.json", output / "source-discovery.json")
    temporary = []
    try:
        for target, value in zip(targets, (mapping, report)):
            if target.is_symlink():
                raise OSError("Output must not be a symbolic link")
            tmp = output / (".source-discovery-" + uuid.uuid4().hex + ".tmp")
            temporary.append(tmp)
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for tmp, target in zip(temporary, targets):
            tmp.replace(target)
    finally:
        for tmp in temporary:
            tmp.unlink(missing_ok=True)
    return targets


def main(argv: list[str] | None = None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(description="Discover local imagery names without exporting credentials.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--worker-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="Directory for the two public JSON reports")
    args = parser.parse_args(argv)
    try:
        maps = fetch_maps(args.base_url, args.worker_config, transport=transport)
        mapping, report = discover_sources(maps)
        mapping_file, discovery_file = write_reports(args.output, mapping, report)
        result = {"status": "ok", "mapping_file": str(mapping_file), "discovery_file": str(discovery_file),
                  "matched_sources": report["matched_sources"], "gateway_optical_sources": report["gateway_optical_sources"],
                  "unmapped_sources": report["unmapped_sources"], "capture_verified": False}
    except DiscoveryError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "code": "report_write_failed", "message": "Cannot write the public discovery reports."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
