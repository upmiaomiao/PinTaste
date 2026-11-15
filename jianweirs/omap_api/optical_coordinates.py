"""Explicit optical input coordinates, distinct from custom tile projections.

Omap's request CoordType and map-list CoordType are different enumerations.
The official Web manual is https://www.gpsov.com/appdoc/index.php?id=32 .
No provider-specific or locally observed pixel offset is a datum conversion.
"""
from __future__ import annotations

import hashlib
import json

from rasterio.warp import transform


def coordinate_contract(source: str, map_type: int | None, map_metadata: dict | None,
                        bbox: list[float] | None = None, *, input_crs: str = "EPSG:4326",
                        request_coord_type: int = 0, runtime_profile: str = "documented") -> dict:
    if runtime_profile not in {"documented", "wgs84_code1"}:
        raise ValueError("Unknown local Omap coordinate profile")
    native_code = request_coord_type
    request_crs = "EPSG:4326" if source == "arcgis" else "GCJ-02" if request_coord_type == 1 else "EPSG:4490"
    conversion = "identity_wgs84" if source == "arcgis" else "proj_wgs84_to_cgcs2000_without_epoch" if input_crs == "EPSG:4326" else "identity_explicit_input_crs"
    if source != "arcgis" and runtime_profile == "wgs84_code1":
        # This explicit, locally selected runtime profile was established by
        # same-number code0/code1 captures plus control points. It is not the
        # official manual's default and is not a constant pixel correction.
        native_code = 0 if input_crs == "GCJ-02" else 1
        request_crs = "GCJ-02" if input_crs == "GCJ-02" else "EPSG:4326"
        conversion = "proj_cgcs2000_to_wgs84_without_epoch" if input_crs == "EPSG:4490" else "identity_explicit_runtime_coordinate_profile"
    public = {k: (map_metadata or {}).get(k) for k in
              ("id", "coord_type", "tile_format", "min_zoom", "max_zoom", "is_satellite")}
    tile_code = public.get("coord_type")
    contract = {
        "input_crs": input_crs,
        "axis_order": "longitude_latitude",
        "provider": source, "provider_map_type": map_type,
        "input_coord_type_legacy": request_coord_type,
        "request_coord_type": native_code,
        "request_crs": request_crs,
        "runtime_coordinate_profile": "esri_rest" if source == "arcgis" else runtime_profile,
        "runtime_profile_scope": "explicit_local_configuration; revalidate_after_desktop_or_source_changes",
        "provider_tile_coordinate_code": tile_code if source != "arcgis" else None,
        "provider_tile_projection": ({0: "global_mercator", 1: "china_mercator", 2: "geographic"}.get(tile_code, "unknown")
                                     if source != "arcgis" else "EPSG:3857"),
        "tile_projection_basis": "Omap map-list CoordType; different enumeration from request CoordType",
        "conversion": conversion,
        "datum_accuracy_verified": False,
        "provider_tile_conversion": "Esri REST imageSR" if source == "arcgis" else "delegated_to_omap_request_coordtype; do_not_double_apply_gcj_shift",
        "local_pixel_offset_applied": False,
        "coordinate_conversion_proves_image_registration": False,
        "documentation": "https://www.gpsov.com/appdoc/index.php?id=32",
        "configuration_fingerprint_basis": "source_map_id_and_public_coordinate_metadata; exact asset SHA additionally required for audits",
    }
    binding = {"source": source, "map_type": map_type, "map": public,
               "input_crs": input_crs, "request_coord_type": request_coord_type,
               "runtime_profile":runtime_profile,"algorithm_version": 2}
    contract["configuration_fingerprint"] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    if bbox is not None:
        contract["input_bbox"] = bbox
        if input_crs in {"EPSG:4326", "EPSG:4490"}:
            xs, ys = transform(input_crs, contract["request_crs"],
                               [bbox[0], bbox[0], bbox[2], bbox[2]],
                               [bbox[1], bbox[3], bbox[1], bbox[3]])
            contract["request_bbox"] = [min(xs), min(ys), max(xs), max(ys)]
            wx, wy = transform(input_crs, "EPSG:4326", [bbox[0], bbox[2]], [bbox[1], bbox[3]])
            contract["requested_bbox_wgs84"] = [wx[0], wy[0], wx[1], wy[1]]
        else:
            # Retain explicit GCJ input for old desktop callers. No unverified
            # numerical inverse or fabricated WGS84 extent is introduced.
            contract["request_bbox"] = bbox
            contract["requested_bbox_wgs84"] = None
    return contract


def unknown_registration(reason: str = "no_local_control_point_audit") -> dict:
    return {"status": "unknown", "strict_same_point_eligible": False,
            "scope": "this_asset_and_reference_aoi_only", "reason": reason,
            "absolute_ground_accuracy_verified": False,
            "acquisition_date_verified": False, "material_identity_verified": False}
