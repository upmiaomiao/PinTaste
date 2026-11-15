"""Verify an installed remote upgrade using a private local connection file.

Never prints credentials, signed provider URLs, or raw upstream exceptions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from scripts.deploy.expand_sources import BBOX
from scripts.deploy.verify_public_api import (
    GatewayClient, PublicVerifier, VerificationError, write_report,
)


class UpgradeVerifier(PublicVerifier):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.science_metadata = {}

    def run_job(self, path, payload):
        job, result = super().run_job(path, payload)
        if path in {"/api/multispectral/subset", "/api/sar/crop"}:
            metadata = result.get("metadata", result)
            fields = ("coordinate_contract", "spatial_registration", "raster_crs",
                      "raster_bounds", "actual_bbox_wgs84", "affine_gdal", "transform")
            self.science_metadata[path] = {"job_id": job, **{k: metadata[k] for k in fields if k in metadata}}
        return job, result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--connection-env", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    secrets = []
    try:
        cfg = dotenv_values(a.connection_env, interpolate=False)
        key = cfg["REMOTE_SENSING_API_KEY"]
        secrets = [key]
        client = GatewayClient(cfg["REMOTE_SENSING_API_BASE_URL"], key)
        probe = PublicVerifier(client, a.output)
        catalog = probe.json_request("GET", "/api/optical/sources")
        entries = catalog.get("sources", [])
        enabled = [s["source"] for s in entries if s.get("configured_nodes")]
        disabled = [s["source"] for s in entries if not s.get("configured_nodes")]
        v = UpgradeVerifier(client, a.output, optical_sources=enabled, poll_timeout=240)
        report = v.run()
        report.update(upgrade_marker=catalog.get("optical_alignment"),
                      enabled_sources=enabled, disabled_sources=disabled,
                      science_coordinate_metadata=v.science_metadata)
        write_report(a.output, report, secrets)
        alignment = []
        for source in enabled:
            if source == "arcgis":
                continue
            row = {"source": source, "verified": False, "bbox_wgs84": BBOX,
                   "scope": "this_AOI_asset_only"}
            try:
                job, result = v.run_job("/api/optical/latest", {
                    "source": source, "bbox": BBOX, "input_crs": "EPSG:4326",
                    "coord_type": 0, "zoom": 17, "width": 768, "height": 768,
                    "timeout_seconds": 100, "alignment_reference": "arcgis",
                    "registration_mode": "strict", "correct_alignment": True,
                })
                audit = result.get("metadata", {}).get("spatial_registration", {})
                if audit.get("status") != "verified" or not audit.get("strict_same_point_eligible"):
                    raise VerificationError("alignment_not_verified")
                data = v.job_file(job, result)
                row.update(verified=True, spatial_registration=audit,
                           coordinate_contract=result.get("metadata", {}).get("coordinate_contract"),
                           artifact=v.save_artifact("aligned-"+source+".png", data))
            except VerificationError as exc:
                row["error"] = {"code": exc.code, **exc.fields}
            alignment.append(row)
            print("ALIGNMENT "+source+": "+("VERIFIED" if row["verified"] else "UNVERIFIED"), flush=True)
            report["optical_alignment_checks"] = alignment
            write_report(a.output, report, secrets)
        report.update(completed_at=datetime.now(timezone.utc).isoformat(),
                      all_enabled_optical_samples_aligned=all(s["verified"] for s in alignment),
                      alignment_scope="Relative optical-to-Esri only; no survey or cross-sensor accuracy certified")
        write_report(a.output, report, secrets)
        print("REMOTE_UPGRADE_CHECKS_COMPLETED", flush=True)
        return 0 if report["passed"] and report["all_enabled_optical_samples_aligned"] else 1
    except Exception:
        print("REMOTE_UPGRADE_VERIFICATION_CONFIGURATION_OR_TRANSPORT_ERROR", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
