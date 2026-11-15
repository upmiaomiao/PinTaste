"""Read-only, sanitized probes for installed Ovital temporal capabilities.

Reads the official 20860/20861 getters. Never returns source hosts, URLs,
query values, credentials, user IDs, or image data. No desktop map mutation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from omap_api.protocol import OmapClient, OmapError, _get_case


def layer_summary(layer: dict) -> dict:
    raw_url = _get_case(layer, "strUrl")
    url = raw_url if isinstance(raw_url, str) else ""
    result = {name: _get_case(layer, field) for name, field in {
        "map_type": "MapId", "name": "strName", "min_zoom": "MinLevel",
        "max_zoom": "MaxLevel", "projection_type": "CoordType",
        "tile_format": "TileFormat", "tile_size": "TileSize",
        "version": "Version", "use_layer": "UseLayer",
        "big_layer": "LayerBigId", "small_layer": "LayerSmallId",
        "is_satellite_config_flag": "IsSatellite",
    }.items()}
    # Print only variable NAMES, excluding their assignments or URL values.
    result["source_url_returned"] = isinstance(raw_url, str) and bool(raw_url)
    result["url_variable_names"] = sorted(set(re.findall(r"\{\$([A-Za-z][A-Za-z0-9_]*)", url))) if url else None
    result["contains_official_time_template"] = any(
        name in (result["url_variable_names"] or []) for name in ("dy", "dys", "dym", "dyms", "dyms1", "dinc", "dexc", "ovtm")
    ) if url else None
    result["contains_time_query_key"] = bool(re.search(r"(?:[?&])(?:date|time|year|month|timestamp)=", url, re.I)) if url else None
    result["url_fingerprint_sha256"] = hashlib.sha256(url.encode()).hexdigest() if url else None
    result["response_field_names"] = sorted(layer)
    return result


async def main() -> int:
    config = dotenv_values(ROOT / ".env")
    client = OmapClient(config.get("OMAP_WS_URL", "ws://127.0.0.1:7699"), config.get("OMAP_PSK", ""), timeout=5)
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
              "custom_maps": [], "default_maps_getter": None}
    try:
        custom = await client._exchange(20860, {})
        msg = _get_case(custom, "msg", {})
        layers = _get_case(msg, "CustomMaps", [])
        if isinstance(layers, list):
            report["custom_maps"] = [layer_summary(layer) for layer in layers
                                     if isinstance(layer, dict) and _get_case(layer, "MapId") in (215, 246, 291)]
    except OmapError as exc:
        report["custom_maps_error"] = {"type": type(exc).__name__, "code": exc.code}
    try:
        default = await client._exchange(20861, {})
        report["default_maps_getter"] = {
            "response_field_names": sorted(default),
            "msg_field_names": sorted(_get_case(default, "msg", {})),
            "status": _get_case(default, "ret"),
            "sanitized_maps": [],
        }
        msg = _get_case(default, "msg", {})
        for value in msg.values():
            if isinstance(value, list):
                report["default_maps_getter"]["sanitized_maps"].extend(
                    layer_summary(layer) for layer in value if isinstance(layer, dict))
    except OmapError as exc:
        report["default_maps_getter"] = {"error": type(exc).__name__, "code": exc.code}
    target = ROOT / "research" / "history-probe.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
