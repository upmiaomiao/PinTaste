"""Bounded local image-to-image registration; never a global source offset.

Only worker-owned PNG IDs and Esri's actual export extent may anchor an audit.
Matching verifies relative spatial support, not absolute survey accuracy, date,
independence of satellite acquisitions, or waste/material interpretation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from .image_quality import validate_optical_preview
from .optical_coordinates import unknown_registration

IMAGE_ID = re.compile(r"[0-9a-f]{32}")
ALGORITHM = "local_sift_similarity_v1"


class RegistrationError(ValueError):
    pass


def read_worker_image(folder: Path, identifier: str) -> tuple[bytes, dict]:
    if not IMAGE_ID.fullmatch(identifier):
        raise RegistrationError("invalid_image_id")
    png, metadata = folder / f"{identifier}.png", folder / f"{identifier}.json"
    if any(not p.is_file() or p.is_symlink() for p in (png, metadata)):
        raise RegistrationError("image_not_found")
    if png.stat().st_size > 32*1024*1024 or metadata.stat().st_size > 2*1024*1024:
        raise RegistrationError("artifact_exceeds_registration_limit")
    try:
        raw, meta = png.read_bytes(), json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RegistrationError("invalid_local_image_metadata") from None
    if not isinstance(meta, dict):
        raise RegistrationError("invalid_local_image_metadata")
    return raw, meta


def anchor_extent(metadata: dict) -> dict:
    if (metadata.get("provider") != "esri_world_imagery"
            or metadata.get("georeferencing_basis") != "server_export_actual_extent_and_pixel_dimensions"
            or metadata.get("raster_crs") != "EPSG:3857"):
        raise RegistrationError("reference_requires_esri_actual_extent")
    affine, bounds = metadata.get("affine_gdal"), metadata.get("raster_bounds")
    if (not isinstance(affine, list) or len(affine) != 6
            or not isinstance(bounds, list) or len(bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
                   for v in affine+bounds) or affine[1] <= 0 or affine[5] >= 0 or affine[2:3] != [0]
            or affine[4] != 0):
        raise RegistrationError("invalid_reference_extent")
    width, height = metadata.get("width"), metadata.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or not 64 <= width <= 2048 or not 64 <= height <= 2048:
        raise RegistrationError("invalid_reference_dimensions")
    expected = [affine[0], affine[3]+height*affine[5], affine[0]+width*affine[1], affine[3]]
    if max(abs(a-b) for a,b in zip(expected,bounds)) > .05:
        raise RegistrationError("reference_extent_affine_mismatch")
    return {k: metadata.get(k) for k in ("raster_crs", "raster_bounds", "affine_gdal", "actual_bbox_wgs84", "width", "height")}


def _decode(blob: bytes) -> tuple[np.ndarray, np.ndarray]:
    try:
        with Image.open(io.BytesIO(blob)) as im:
            if not 64 <= im.width <= 2048 or not 64 <= im.height <= 2048 or im.width*im.height > 4_000_000:
                raise RegistrationError("image_dimensions_exceed_registration_limit")
            im.load()
            return np.asarray(im.convert("RGB")), np.asarray(im.convert("RGBA"))[:,:,3]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise RegistrationError("image_decode_failed") from None


def audit_images(source_png: bytes, reference_png: bytes, reference_metadata: dict,
                 *, correct: bool = False) -> tuple[dict, bytes | None]:
    extent = anchor_extent(reference_metadata)
    source_hash, ref_hash = hashlib.sha256(source_png).hexdigest(), hashlib.sha256(reference_png).hexdigest()
    audit = {**unknown_registration(), "algorithm": ALGORITHM, "method": "classical_sift_similarity",
             "source_sha256": source_hash, "reference_asset_sha256": ref_hash,
             "reference_extent": extent, "audited_at": datetime.now(timezone.utc).isoformat(),
             "thresholds": {"minimum_unique_inliers": 8, "minimum_hull_fraction": .025,
                            "minimum_xy_span_fraction": .18, "maximum_inlier_rms_px": 3,
                            "scale_range": [.8,1.25], "maximum_rotation_degrees": 5},
             "warning": "Local image-relative verification only; no surveyed accuracy or acquisition-date proof."}
    for role, blob in (("source",source_png),("reference",reference_png)):
        if not validate_optical_preview(blob)["valid"]:
            audit["reason"] = f"{role}_preview_quality_rejected"
            return audit, None
    (source,source_alpha), (reference,reference_alpha) = _decode(source_png), _decode(reference_png)
    if not np.all(reference_alpha):
        raise RegistrationError("reference_has_transparent_uncovered_pixels")
    if list(reference.shape[:2][::-1]) != [extent["width"],extent["height"]]:
        raise RegistrationError("reference_image_metadata_dimensions_mismatch")
    try:
        import cv2
    except ImportError:
        audit["reason"] = "registration_engine_not_installed"
        return audit, None
    # Deterministic bounded CPU work and fixed thresholding. No learned model.
    cv2.setNumThreads(1)
    sift = cv2.SIFT_create(nfeatures=6000)
    a, da = sift.detectAndCompute(cv2.cvtColor(source, cv2.COLOR_RGB2GRAY),None)
    b, db = sift.detectAndCompute(cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY),None)
    if da is None or db is None or min(len(a),len(b)) < 8:
        audit["reason"] = "insufficient_image_features"
        return audit, None
    pairs = cv2.BFMatcher().knnMatch(da,db,k=2)
    matches = [m for pair in pairs if len(pair)==2 for m,n in [pair] if m.distance < .72*n.distance]
    # Enforce unique source/reference locations; SIFT orientations can duplicate.
    unique = {};used_source=set();used_reference=set()
    for m in sorted(matches,key=lambda m:m.distance):
        p,q=a[m.queryIdx].pt,b[m.trainIdx].pt
        pkey=(round(p[0],1),round(p[1],1));qkey=(round(q[0],1),round(q[1],1))
        if pkey in used_source or qkey in used_reference:
            continue
        used_source.add(pkey);used_reference.add(qkey)
        unique[pkey+qkey]=m
    matches=list(unique.values())
    audit["candidate_match_count"] = len(matches)
    if len(matches)<8:
        audit["reason"]="insufficient_unique_matches"
        return audit,None
    pa=np.float32([a[m.queryIdx].pt for m in matches]);pb=np.float32([b[m.trainIdx].pt for m in matches])
    cv2.setRNGSeed(20261017)
    matrix,mask=cv2.estimateAffinePartial2D(pa,pb,method=cv2.RANSAC,ransacReprojThreshold=3,maxIters=5000,confidence=.999)
    if matrix is None or mask is None:
        audit["reason"]="similarity_estimation_failed"
        return audit,None
    keep=mask.ravel().astype(bool);ps,pr=pa[keep],pb[keep]
    predicted=ps@matrix[:,:2].T+matrix[:,2]
    rms=float(np.sqrt(np.mean(np.sum((predicted-pr)**2,axis=1))))
    scale=float(np.hypot(matrix[0,0],matrix[1,0]));rotation=float(np.degrees(np.arctan2(matrix[1,0],matrix[0,0])))
    expected_scale=reference.shape[1]/source.shape[1]
    relative_scale=scale/expected_scale
    spreads=[]
    for pts,im in ((ps,source),(pr,reference)):
        hull=float(cv2.contourArea(cv2.convexHull(pts)))/(im.shape[0]*im.shape[1])
        span=(np.ptp(pts,axis=0)/[im.shape[1],im.shape[0]]).tolist()
        spreads.append({"hull_fraction":hull,"xy_span_fraction":span})
    audit.update(transform_source_to_reference=matrix.tolist(),unique_inlier_count=len(ps),
                 inlier_ratio=float(keep.mean()),rms_pixels=rms,scale_factor=scale,
                 expected_scale_from_image_dimensions=expected_scale,
                 dimension_normalized_scale_factor=relative_scale,rotation_degrees=rotation,
                 inlier_spread={"source":spreads[0],"reference":spreads[1]},
                 control_points=[{"source_pixel":p.tolist(),"reference_pixel":q.tolist()} for p,q in zip(ps,pr)])
    acceptable=(len(ps)>=8 and keep.mean()>=.25 and rms<=3 and .8<=relative_scale<=1.25 and abs(rotation)<=5
                and all(s["hull_fraction"]>=.025 and min(s["xy_span_fraction"])>=.18 for s in spreads))
    if not acceptable:
        audit["reason"]="control_points_insufficient_or_poorly_distributed"
        return audit,None
    width,height=extent["width"],extent["height"]
    center=np.array([source.shape[1]/2,source.shape[0]/2]);aligned=matrix[:,:2]@center+matrix[:,2]
    displacement=float(np.linalg.norm(aligned-np.array([width/2,height/2])))
    audit["raster_center_displacement_reference_pixels"]=displacement
    # The reference footprint is known; a precise native GSD for either image is
    # not established. Use reference display sampling only to express fit scale.
    bbox=extent.get("actual_bbox_wgs84")
    lat=(bbox[1]+bbox[3])/2 if isinstance(bbox,list) and len(bbox)==4 else 0
    ground_sampling=extent["affine_gdal"][1]*math.cos(math.radians(lat))
    audit["relative_fit_uncertainty_m"]=max(5.,3*rms*ground_sampling)
    audit["uncertainty_basis"]="conservative display-fit floor only; anchor absolute accuracy unverified"
    audit["reference_display_sampling_m"]=ground_sampling
    misaligned=displacement>max(8,10/max(ground_sampling,.01)) or abs(relative_scale-1)>.015 or abs(rotation)>.5
    if not correct:
        audit.update(status="misaligned" if misaligned else "verified",
                     reason="local_transform_required" if misaligned else "local_control_points_consistent",
                     strict_same_point_eligible=not misaligned)
        return audit,None
    coverage=cv2.warpAffine(source_alpha,matrix,(width,height),flags=cv2.INTER_NEAREST,borderValue=0)
    center_mask=coverage[max(0,height//2-4):height//2+5,max(0,width//2-4):width//2+5]
    center_valid=bool(center_mask.size and np.all(center_mask))
    fraction=float(np.count_nonzero(coverage)/coverage.size)
    valid_y,valid_x=np.nonzero(coverage)
    audit.update(status="verified" if center_valid else "misaligned",corrected=True,
                 reason="local_corrected_target_covered" if center_valid else "corrected_image_does_not_cover_target",
                 strict_same_point_eligible=center_valid,valid_coverage_fraction=fraction,
                 full_requested_bbox_covered=bool(np.all(coverage)),target_center_covered=center_valid,
                 valid_reference_pixel_bounds=([int(valid_x.min()),int(valid_y.min()),int(valid_x.max()+1),int(valid_y.max()+1)] if valid_x.size else None),
                 reference_extent_is_display_canvas_not_guaranteed_full_source_coverage=True,
                 valid_coverage_rule="Only nontransparent pixels are evidence; nominal extent includes possible uncovered area.")
    rgb=cv2.warpAffine(source,matrix,(width,height),flags=cv2.INTER_NEAREST,borderValue=(0,0,0))
    rgba=np.dstack((rgb,coverage));buffer=io.BytesIO();Image.fromarray(rgba).save(buffer,format="PNG")
    audit["corrected_sha256"]=hashlib.sha256(buffer.getvalue()).hexdigest()
    return audit,buffer.getvalue()


def persist_audit(folder: Path, image_id: str, audit: dict[str,Any]) -> None:
    if not IMAGE_ID.fullmatch(image_id):
        raise RegistrationError("invalid_image_id")
    directory=folder/"registration";directory.mkdir(parents=True,exist_ok=True)
    path=directory/f"{image_id}.json";temporary=directory/f".{image_id}.tmp"
    temporary.write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(path)


def read_audit(folder: Path, image_id: str, png: bytes, fingerprint: str | None = None) -> dict:
    path=folder/"registration"/f"{image_id}.json"
    if not IMAGE_ID.fullmatch(image_id) or not path.is_file() or path.is_symlink() or path.stat().st_size>1024*1024:
        return unknown_registration()
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (ValueError,OSError):
        return unknown_registration("audit_unreadable")
    if not isinstance(value,dict) or value.get("source_sha256")!=hashlib.sha256(png).hexdigest():
        return unknown_registration("audit_asset_hash_mismatch")
    if fingerprint and value.get("configuration_fingerprint")!=fingerprint:
        return unknown_registration("audit_configuration_mismatch")
    return value
