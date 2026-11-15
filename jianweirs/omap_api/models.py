"""Validated public requests. Coordinates are always longitude, latitude."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EARTH_RADIUS_M = 6_378_137.0
MAX_IMAGE_PIXELS = 4_000_000
MAX_BBOX_AXIS_M = 12_000.0
MAX_BBOX_AREA_M2 = 144_000_000.0


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    map_type: int | None = Field(default=None, ge=0)
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="[min_longitude, min_latitude, max_longitude, max_latitude]"
    )
    center: tuple[float, float] | None = Field(
        default=None, description="[longitude, latitude]"
    )
    radius_m: float | None = Field(default=None, gt=0, le=5_000)
    coord_type: Literal[0, 1] = Field(
        default=0, description="0: CGCS2000; 1: GCJ-02. No automatic conversion."
    )
    zoom: int = Field(default=18, ge=1, le=28)
    width: int = Field(default=1024, ge=64, le=2048)
    height: int = Field(default=1024, ge=64, le=2048)
    timeout_seconds: float = Field(default=45, ge=1, le=120)

    @model_validator(mode="after")
    def validate_region(self) -> SnapshotRequest:
        if (self.bbox is None) == (self.center is None):
            raise ValueError("Provide exactly one of bbox or center.")
        if self.bbox is not None and self.radius_m is not None:
            raise ValueError("radius_m is only valid with center.")
        if self.center is not None:
            lon, lat = self.center
            if not (-180 <= lon <= 180 and -85 <= lat <= 85):
                raise ValueError("center must be longitude [-180,180], latitude [-85,85].")
            if self.radius_m is None:
                raise ValueError("center requires radius_m.")
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds the 4,000,000 pixel limit.")
        _validate_bbox(self.normalized_bbox())
        return self

    def normalized_bbox(self) -> tuple[float, float, float, float]:
        if self.bbox is not None:
            return self.bbox
        assert self.center is not None and self.radius_m is not None
        lon, lat = self.center
        delta_lat = math.degrees(self.radius_m / EARTH_RADIUS_M)
        delta_lon = delta_lat / math.cos(math.radians(lat))
        return lon - delta_lon, lat - delta_lat, lon + delta_lon, lat + delta_lat

    def capture_arguments(self, resolved_map_type: int) -> dict:
        return {
            "map_type": resolved_map_type,
            "bbox": list(self.normalized_bbox()),
            "coord_type": self.coord_type,
            "zoom": self.zoom,
            "width": self.width,
            "height": self.height,
            "timeout_seconds": self.timeout_seconds,
        }


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(value) for value in bbox):
        raise ValueError("bbox coordinates must be finite.")
    if not (-180 <= min_lon < max_lon <= 180 and -85 <= min_lat < max_lat <= 85):
        raise ValueError("bbox must be ordered lon/lat bounds within [-180,180] and [-85,85].")
    middle_lat = (min_lat + max_lat) / 2
    width_m = EARTH_RADIUS_M * math.radians(max_lon - min_lon) * math.cos(math.radians(middle_lat))
    height_m = EARTH_RADIUS_M * math.radians(max_lat - min_lat)
    if max(width_m, height_m) > MAX_BBOX_AXIS_M or width_m * height_m > MAX_BBOX_AREA_M2:
        raise ValueError("bbox is too large: maximum 12 km per side and 144 square km.")
