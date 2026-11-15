"""Exercise the running HTTP service against the real, signed-in Omap client.

No provider URLs, passwords or complete vendor configurations are recorded.
Run with the project interpreter: python scripts/smoke_test.py
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time

import httpx
from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "base_url": args.base_url, "checks": []}
    failed = False

    def record(name: str, ok: bool, **details) -> None:
        nonlocal failed
        failed |= not ok
        report["checks"].append({"name": name, "passed": ok, **details})
        print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)

    with httpx.Client(base_url=args.base_url, timeout=135, trust_env=False) as client:
        for route in ("/health", "/api/current", "/api/maps"):
            response = client.get(route)
            body = response.json()
            if route == "/api/maps":
                body = {"maps": [m for m in body.get("maps", []) if m.get("id") in (337, 502, 503, 710, 906)]}
            record(route, response.status_code == 200, status=response.status_code, response=body)

        scenes = [
            ("bing-beijing", [116.395, 39.908], 400, 18, 1024, None),
            ("bing-shanghai", [121.499, 31.239], 300, 17, 512, None),
            ("bing-aerial-beijing", [116.395, 39.908], 250, 17, 512, 502),
            ("bing-shenzhen", [114.062, 22.548], 300, 17, 512, None),
        ]
        for name, center, radius, zoom, size, source in scenes:
            request = {"center": center, "radius_m": radius, "coord_type": 0, "zoom": zoom,
                       "width": size, "height": size, "timeout_seconds": 120}
            if source is not None:
                request["map_type"] = source
            began = time.monotonic()
            response = client.post("/api/snapshot", json=request)
            elapsed = round(time.monotonic() - began, 3)
            body = response.json()
            if response.status_code != 200:
                record(name, False, status=response.status_code, elapsed_seconds=elapsed, response=body)
                continue
            image = client.get(body["image_url"])
            try:
                with Image.open(io.BytesIO(image.content)) as im:
                    dimensions = list(im.size)
                    im.load()
                    # These three specific urban scenes have many photographic
                    # colors. A valid PNG containing only the gray loading grid
                    # must not pass this live acceptance check. This is a scene-
                    # specific independent check, not a general model QA metric.
                    photographic_colors = im.convert("RGB").getcolors(2000) is None
                valid = (image.status_code == 200 and image.headers.get("content-type", "").startswith("image/png")
                         and dimensions == [size, size] and photographic_colors
                         and body.get("metadata", {}).get("placeholder_check") == "passed")
            except (OSError, ValueError):
                valid, dimensions, photographic_colors = False, None, False
            if valid:
                (output / f"{name}.png").write_bytes(image.content)
            record(name, valid, status=response.status_code, elapsed_seconds=elapsed, dimensions=dimensions,
                   urban_scene_color_check=photographic_colors,
                   image_url=body.get("image_url"), sha256=hashlib.sha256(image.content).hexdigest(), metadata=body.get("metadata"))

        valid_request = {"center": [116.395, 39.908], "radius_m": 100, "width": 256, "height": 256}
        for name, patch, code in (
            ("unknown-source", {"map_type": 2147483647}, "unknown_map"),
            ("source-zoom-limit", {"map_type": 337, "zoom": 28}, "map_zoom_out_of_range"),
        ):
            response = client.post("/api/snapshot", json={**valid_request, **patch})
            detail = response.json().get("detail", {})
            record(name, response.status_code == 422 and isinstance(detail, dict) and detail.get("code") == code,
                   status=response.status_code, response=response.json())
        response = client.post("/api/snapshot", json={"bbox": [-180, -80, 180, 80]})
        record("oversized-area", response.status_code == 422, status=response.status_code)
        response = client.get("/api/images/not-a-valid-id.png")
        record("invalid-image-id", response.status_code == 404, status=response.status_code)

    report["passed"] = not failed
    report["pass_count"] = sum(c["passed"] for c in report["checks"])
    report["total_checks"] = len(report["checks"])
    path = output / "test-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {path}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
