r"""Live provider acceptance; does not use or read local credentials.

Run from the repository with .venv\Scripts\python.exe scripts\smoke_test_spectral.py
"""

import asyncio
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from omap_api import spectral


async def main():
    out = ROOT / "outputs" / "spectral"
    out.mkdir(parents=True, exist_ok=True)
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "bbox": [103.8, 30.5, 104.3, 30.9],
              "sources": [], "scientific_subsets": [], "credentials_used": False}
    scenes = {}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        async def check_source(source_id):
            try:
                req = spectral.SpectralSearchRequest(source=source_id, bbox=report["bbox"],
                    datetime="2022-01-01/" + datetime.now(timezone.utc).date().isoformat(), limit=3, max_cloud_cover=20)
                result = await spectral.search_spectral(req, client)
                out.joinpath(source_id + "-search.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                assert result["count"] > 0, "No scene matched Chengdu bbox/date/cloud filter"
                scene = result["scenes"][0]
                scenes[source_id] = scene
                science_keys = [k for k, a in scene["assets"].items() if "data" in a.get("roles", [])]
                preferred = {"sentinel2": "B04", "landsat": "red", "enmap": "image"}.get(source_id)
                if preferred is None:
                    preferred = next((k for k in science_keys if k.endswith("B02.tif") or "_RFL_" in k), science_keys[0])
                asset = await spectral.resolve_spectral_asset(source_id, scene["id"], preferred, client)
                async with client.stream("GET", asset["href"], headers={"Range": "bytes=0-1023"}) as response:
                    initial = bytearray()
                    async for part in response.aiter_bytes():
                        initial.extend(part)
                        if len(initial) >= 1024:
                            break
                    access = {"http_status": response.status_code, "content_type": response.headers.get("content-type"),
                              "first_bytes_hex": bytes(initial[:8]).hex(), "content_range": response.headers.get("content-range"),
                              "authentication_required": asset["authentication_required"]}
                    if asset["authentication_required"]:
                        access["scientific_download_verified"] = False
                        access["authentication_block_observed"] = response.status_code in {301, 302, 303, 307, 308, 401, 403}
                    else:
                        access["scientific_download_verified"] = response.status_code == 206 and bytes(initial[:4]) in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}
                        assert access["scientific_download_verified"], "Asset did not return TIFF bytes"
                detail = {"source": source_id, "passed": True, "search_count": result["count"], "first_scene_id": scene["id"],
                          "acquired_at": scene.get("acquired_at"), "cloud_cover": scene.get("cloud_cover"),
                          "asset_count": len(scene["assets"]), "science_asset": preferred, "access_probe": access}
                report["sources"].append(detail)
                print(json.dumps({"source": source_id, "passed": True, "scenes": result["count"], "asset_status": access["http_status"], "auth_required": access["authentication_required"]}))
            except Exception as exc:
                # Never write provider/GDAL exceptions containing short-lived SAS credentials.
                report["sources"].append({"source": source_id, "passed": False, "error_type": type(exc).__name__,
                                         "message": getattr(exc, "detail", None) if isinstance(getattr(exc, "detail", None), dict) else "Provider live check failed"})
                print(json.dumps({"source": source_id, "passed": False, "error_type": type(exc).__name__}))
        await asyncio.gather(*(check_source(source) for source in spectral.SOURCES))
        for source, assets in [("sentinel2", ["B02", "B03", "B04", "B08"]), ("landsat", ["blue", "green", "red", "nir08"])]:
            scene = scenes.get(source)
            if not scene:
                continue
            try:
                # Scene search intersects a broad city AOI. Pick an ROI inside the
                # scene envelope and city AOI; no assumption that every scene covers
                # every point of that city AOI.
                sb = scene["bbox"]
                lo = max(sb[0], report["bbox"][0]); hi = min(sb[2], report["bbox"][2])
                so = max(sb[1], report["bbox"][1]); no = min(sb[3], report["bbox"][3])
                x, y = (lo + hi) / 2, (so + no) / 2
                bbox = [x - 0.004, y - 0.004, x + 0.004, y + 0.004]
                req = spectral.SpectralSubsetRequest(source=source, scene_id=scene["id"], bbox=bbox, assets=assets, width=128, height=128)
                result = await spectral.subset_spectral(req, client)
                import numpy as np
                import rasterio
                path = spectral.SUBSET_DIR / (result["file_id"] + ".tif")
                with rasterio.open(path) as ds:
                    data = ds.read(masked=True)
                    assert ds.count == 4 and ds.crs is not None and ds.width == 128 and ds.height == 128
                    assert data.count() > 0
                    assert np.isfinite(data.compressed()).all()
                    verification = {"bands": ds.count, "width": ds.width, "height": ds.height, "crs": str(ds.crs),
                                    "valid_pixel_count": int(data.count()), "raw_DN_min": float(data.min()), "raw_DN_max": float(data.max()),
                                    "band_names": list(ds.descriptions)}
                report["scientific_subsets"].append({"source": source, "passed": True, "file_id": result["file_id"],
                                                       "path": str(path), "verification": verification, "metadata": result["metadata"]})
                print(json.dumps({"source": source, "subset_passed": True, "bytes": result["bytes"]}))
            except Exception as exc:
                report["scientific_subsets"].append({"source": source, "passed": False, "error_type": type(exc).__name__,
                                                       "message": getattr(exc, "detail", None) if isinstance(getattr(exc, "detail", None), dict) else "Subset live check failed"})
                print(json.dumps({"source": source, "subset_passed": False, "error_type": type(exc).__name__}))
    report["passed"] = len(report["sources"]) == 6 and len(report["scientific_subsets"]) == 2 and all(c["passed"] for c in report["sources"] + report["scientific_subsets"])
    out.joinpath("verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
