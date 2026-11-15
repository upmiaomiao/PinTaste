"""Reusable source checks with explicit spatial/time scope and expiry.

A source catalogue, an intersecting scene, a preview and a downloaded science
raster are different capabilities. Sample results never assert global coverage.
No API keys, signed asset URLs or provider response bodies are stored here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any
import uuid


_STATES = {"passed", "failed", "credentials_required", "unsupported", "unknown", "no_coverage", "blocked"}
_CAPABILITIES = {"interface", "coverage", "preview", "download", "crop", "georeferencing", "historical_selection"}
_BLOCKING = {"credentials_required", "unsupported", "blocked"}
_TTL = {"passed": 21600, "failed": 900, "credentials_required": 86400,
        "unsupported": 604800, "unknown": 1800, "no_coverage": 86400, "blocked": 900}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class SourceRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("schema_version") == "1.0" and isinstance(value.get("records"), list):
                return value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {"schema_version": "1.0", "catalogs": {}, "records": []}

    def _save(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def catalog(self, fingerprint: str) -> dict | None:
        with self._lock:
            cached = self._read().get("catalogs", {}).get(fingerprint)
        if not cached or datetime.fromisoformat(cached["expires_at"]) <= utc_now():
            return None
        return cached["catalog"]

    def save_catalog(self, fingerprint: str, catalog: dict, ttl_seconds: int = 21600) -> None:
        if catalog.get("errors"):
            return  # A partial outage must not replace a known catalogue for six hours.
        body = {key: value for key, value in catalog.items() if key not in {"availability", "from_cache", "audit_artifacts"}}
        now = utc_now()
        with self._lock:
            store = self._read()
            store.setdefault("catalogs", {})[fingerprint] = {
                "checked_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)), "catalog": body,
            }
            self._save(store)

    def invalidate(self, fingerprint: str, *, group: str | None = None, source: str | None = None,
                   capability: str | None = None, reason: str = "manual_refresh") -> int:
        """Expire matching checks explicitly, retaining their audit history."""
        count = 0
        with self._lock:
            store = self._read()
            for item in store["records"]:
                if item.get("endpoint_fingerprint") != fingerprint or (group and item.get("group") != group) or (source and item.get("source") != source) or (capability and item.get("capability") != capability):
                    continue
                item.update(expires_at=_stamp(utc_now()-timedelta(seconds=1)), invalidated_reason=reason)
                count += 1
            self._save(store)
        return count

    def record(
        self, fingerprint: str, group: str, source: str, capability: str, state: str,
        *, scope: str = "endpoint", operation: str | None = None,
        bbox: list | tuple | None = None, time_range: str | None = None,
        sample_id: str | None = None, reason_code: str = "", status_code: int | None = None,
        evidence: dict | None = None, ttl_seconds: int | None = None,
    ) -> dict:
        if state not in _STATES or capability not in _CAPABILITIES or scope not in {"endpoint", "sample"}:
            raise ValueError("Invalid source capability record")
        if scope == "sample" and bbox is None:
            raise ValueError("Sample checks require their tested bounding box")
        now = utc_now()
        item = {"endpoint_fingerprint": fingerprint, "group": group, "source": source,
                "capability": capability, "state": state, "scope": scope, "operation": operation,
                "bbox": list(bbox) if bbox is not None else None, "time_range": time_range,
                "sample_id": sample_id, "reason_code": reason_code, "status_code": status_code,
                "checked_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=ttl_seconds if ttl_seconds is not None else _TTL[state])),
                "evidence": evidence or {}}
        # Callers supply bounded evidence, never credentials, response bodies, or asset hrefs.
        if any(word in json.dumps(item["evidence"]).lower() for word in ("authorization", "api_key", "access_token", "?sig=", "&sig=")):
            raise ValueError("Registry evidence may not contain credentials or signed URLs")
        with self._lock:
            store = self._read()
            key = (fingerprint, group, source, capability, operation, scope, item["bbox"], time_range)
            store["records"] = [old for old in store["records"] if (
                old.get("endpoint_fingerprint"), old.get("group"), old.get("source"), old.get("capability"),
                old.get("operation"), old.get("scope"), old.get("bbox"), old.get("time_range")) != key]
            store["records"].append(item)
            store["records"] = store["records"][-2000:]
            self._save(store)
        return item

    def snapshot(self, fingerprint: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock:
            store = self._read()
        records = []
        for item in store["records"]:
            if item.get("endpoint_fingerprint") != fingerprint:
                continue
            try:
                fresh = datetime.fromisoformat(item["expires_at"]) > now
            except (ValueError, KeyError):
                fresh = False
            records.append({**item, "fresh": fresh})
        grouped: dict[tuple[str, str], dict] = {}
        for item in records:
            row = grouped.setdefault((item["group"], item["source"]), {"group": item["group"], "source": item["source"], "checks": []})
            row["checks"].append(item)
        return {"schema_version": "1.0", "checked_at": _stamp(now), "sources": list(grouped.values()),
                "record_count": len(records), "fresh_record_count": sum(r["fresh"] for r in records),
                "policy": {"health_ttl_seconds": 21600, "transient_failure_ttl_seconds": 900,
                           "credential_ttl_seconds": 86400, "unsupported_ttl_seconds": 604800,
                           "coverage_scope": "exact tested bbox and time only; other AOIs remain unknown",
                           "configuration_changes": "endpoint/credential fingerprint mismatch invalidates reuse"}}

    def route(
        self, fingerprint: str, group: str, operation: str, *, bbox=None,
        time_range: str | None = None, catalog: dict | None = None,
    ) -> dict:
        catalog = catalog or self.catalog(fingerprint) or {"groups": {}}
        entries = catalog.get("groups", {}).get(group, {}).get("sources", [])
        names = [entry.get("source", entry.get("id")) for entry in entries if isinstance(entry, dict)]
        names = [name for name in names if isinstance(name, str)]
        snapshot = self.snapshot(fingerprint)
        by_source = {entry["source"]: entry["checks"] for entry in snapshot["sources"] if entry["group"] == group}
        eligible, unknown, blocked = [], [], []
        target = "coverage" if operation.endswith(".search") else "crop" if operation.endswith((".crop", ".subset")) else "preview" if operation.endswith(".preview") else "download"
        for source in names:
            checks = [check for check in by_source.get(source, []) if check["fresh"] and (check["operation"] in (None, operation))]
            endpoint_blocks = [check for check in checks if check["scope"] == "endpoint" and check["state"] in _BLOCKING and check["capability"] in {"interface", target}]
            exact = [check for check in checks if check["scope"] == "sample" and check["bbox"] == (list(bbox) if bbox is not None else None) and check["time_range"] == time_range]
            coverage_blocks = [check for check in exact if check["capability"] == "coverage" and check["state"] == "no_coverage"]
            sample_cooldown = [check for check in exact if check["capability"] == target and check["state"] == "failed"]
            endpoint_cooldown = [check for check in checks if check["scope"] == "endpoint" and check["capability"] == "interface" and check["state"] == "failed"]
            if endpoint_blocks or coverage_blocks or sample_cooldown or endpoint_cooldown:
                evidence = (endpoint_blocks or coverage_blocks or sample_cooldown or endpoint_cooldown)[-1]
                blocked.append({"source": source, "reason": evidence["reason_code"], "state": evidence["state"],
                                "cooldown": evidence["state"] == "failed", "expires_at": evidence["expires_at"], "evidence": evidence})
                continue
            passed = any(check["state"] == "passed" and check["capability"] == target for check in exact)
            if passed:
                eligible.append(source)
            else:
                unknown.append(source)
        # Prior successful downloads are preferred for new AOIs without asserting
        # that those other AOIs have coverage.
        unknown.sort(key=lambda source: not any(check["fresh"] and check["state"] == "passed" and check["capability"] == target for check in by_source.get(source, [])))
        return {"group": group, "operation": operation, "eligible": eligible, "unknown": unknown,
                "blocked": blocked, "candidate_order": eligible + unknown,
                "coverage_note": "Unknown coverage remains eligible for one bounded attempt; cached failures never prove no waste."}
