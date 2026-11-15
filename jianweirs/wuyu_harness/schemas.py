"""Validated task, evidence and model-output records for the remote sensing workflow."""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as CalendarDate
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TaskKind(StrEnum):
    DISCOVERY = "suspected_site_discovery"
    CLASSIFY = "waste_type_screening"
    EXTENT = "stockpile_extent"
    CHANGE = "stockpile_change"
    CLEANUP = "cleanup_progress"
    RESIDUAL = "residual_stockpile"
    RECURRENCE = "repeated_dumping"
    TRACKING = "site_tracking"
    ENVIRONMENT = "environmental_context"
    RELATIONSHIP = "surrounding_relationship"


TASK_LABELS = {
    TaskKind.DISCOVERY: "疑似点位发现", TaskKind.CLASSIFY: "固废类型筛查",
    TaskKind.EXTENT: "堆存范围判定", TaskKind.CHANGE: "堆存变化监测",
    TaskKind.CLEANUP: "清理进度核查", TaskKind.RESIDUAL: "残留堆体识别",
    TaskKind.RECURRENCE: "重复倾倒排查", TaskKind.TRACKING: "点位持续跟踪",
    TaskKind.ENVIRONMENT: "环境判读", TaskKind.RELATIONSHIP: "周边关系判读",
}


ResearchSource = Literal[
    "sentinel2", "landsat", "hls_s30", "hls_l30", "emit", "enmap",
    "sentinel1-rtc", "sentinel1-grd", "sentinel1-slc", "alos-palsar-rtc",
    "nisar-gcov", "jaxa-palsar-mosaic", "bing", "amap", "tencent", "google",
    "mapbox", "mapquest", "arcgis", "tianditu", "siwei", "siwei_daily",
    "gaofen2026", "century", "jilin2024", "siwei2025", "history246",
]


class OpticalPeriod(Record):
    """Requested edition/date is separate from provider-reported capture time."""
    source: ResearchSource
    year: int | None = Field(default=None, ge=1900, le=2100)
    date: CalendarDate | None = None

    @model_validator(mode="after")
    def validate_period(self):
        if self.source not in {"century", "jilin2024", "siwei2025", "history246"}:
            raise ValueError("指定时相仅支持已配置的年度或历史光学图源")
        if self.source == "century" and self.year not in range(2021, 2026):
            raise ValueError("世纪空间版次需为2021至2025")
        if self.source in {"jilin2024", "siwei2025"}:
            expected = 2024 if self.source == "jilin2024" else 2025
            if self.year is not None and self.year != expected:
                raise ValueError("固定年度图源的版次与来源不一致")
        if self.source == "history246" and (self.date is None or self.year is not None):
            raise ValueError("历史图源需提供date且不使用year版次")
        if self.source != "history246" and self.date is not None:
            raise ValueError("年度图源的year不是拍摄日期，不能同时指定date")
        return self


class ResearchRequest(Record):
    """Executable research intent, independent of waste-screening task labels."""
    mode: Literal["auto", "availability", "analysis", "timeseries"] = "auto"
    sources: list[ResearchSource] = Field(default_factory=list, max_length=28)
    expand_adjacent_months: int = Field(default=0, ge=0, le=12)
    full_catalog: bool = False
    include_optical_context: bool = False
    anchor_start: date | None = None
    anchor_end: date | None = None

    @model_validator(mode="after")
    def validate_anchor(self):
        if self.anchor_start and self.anchor_end and self.anchor_start > self.anchor_end:
            raise ValueError("研究基准开始日期不得晚于结束日期")
        return self


class ComplaintScan(Record):
    """A user-confirmed, bounded search region; a place name is not a footprint."""
    text: str = Field(default="", max_length=12000)
    region_bbox: tuple[float, float, float, float] | None = None
    region_polygon: dict | None = None
    region_confirmed: bool = False
    zoom: int = Field(default=18, ge=15, le=21)
    window_pixels: Literal[512, 1024, 2048] = 1024
    overlap: float = Field(default=0.15, ge=0, le=0.5)
    max_windows: int = Field(default=16, ge=1, le=256)
    max_area_km2: float = Field(default=25, ge=0.001, le=2500)
    window_id: str | None = Field(default=None, pattern=r"^r\d{4}c\d{4}$")
    window_bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_region(self):
        if self.region_bbox is not None and self.region_polygon is not None:
            raise ValueError("扫描范围只能提供bbox或Polygon之一")
        if self.region_bbox is not None:
            west, south, east, north = self.region_bbox
            if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
                raise ValueError("扫描bbox必须为WGS84有序的[西,南,东,北]")
        if self.region_confirmed and self.region_bbox is None and self.region_polygon is None:
            raise ValueError("确认扫描前需有明确范围，不能将地名当成边界")
        if (self.window_id is None) != (self.window_bbox is None):
            raise ValueError("扫描子窗口编号和范围必须同时提供")
        if self.window_bbox is not None:
            w, s, e, n = self.window_bbox
            if not self.region_confirmed or not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
                raise ValueError("扫描子窗口需有效WGS84范围并已确认父区域")
        return self


class TaskRequest(Record):
    objective: str = Field(min_length=1, max_length=12000)
    task_types: list[TaskKind] = Field(min_length=1, max_length=10)
    workflow_mode: Literal['auto','site_investigation','complaint_investigation','environmental_risk','temporal_change','contextual_qa',
                           'information_search'] = 'auto'
    waste_status: Literal['unverified','user_confirmed'] = 'unverified'
    context_package: dict = Field(default_factory=dict)
    bbox: tuple[float, float, float, float] | None = None
    center: tuple[float, float] | None = None
    coordinate_system: Literal["wgs84", "unknown"] = "wgs84"
    site_area_m2: float | None = Field(default=None, gt=0, le=100_000_000)
    view_mode: Literal["auto", "single"] = "auto"
    selector_mode: Literal["auto", "model"] = "auto"
    date_start: date | None = None
    date_end: date | None = None
    jurisdiction: str = Field(default="中国", max_length=200)
    asset_ids: list[str] = Field(default_factory=list, max_length=8)
    document_ids: list[str] = Field(default_factory=list, max_length=12)
    acquire_optical: bool = False
    optical_source: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,50}$")
    optical_zoom: int | None = Field(default=None, ge=1, le=22)
    refresh_imagery: bool = False
    location_context: dict = Field(default_factory=dict)
    nearby_radius_m: float | None = Field(default=None, ge=1, le=5000)
    nearby_max_windows: int = Field(default=16, ge=1, le=16)
    selected_sources: list[ResearchSource] = Field(default_factory=list, max_length=28)
    optical_periods: list[OpticalPeriod] = Field(default_factory=list, max_length=6)
    online_supplement: Literal["auto", "allowed", "uploaded_only", "ask"] = "auto"
    search_policy: bool = True
    search_only: bool = False
    consult_shensi: bool = False
    # Follow-up answer model chosen in the composer. "jianwei" keeps the
    # existing evidence answer; "shensi" lets 无隅慎思 write the final reply.
    answer_model: Literal["jianwei", "shensi"] = "jianwei"
    deep_thinking: bool = True
    adaptive_review: bool = True
    research: ResearchRequest | None = None
    complaint_scan: ComplaintScan | None = None
    prompt: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def validate_scope(self) -> TaskRequest:
        if self.complaint_scan and self.complaint_scan.window_bbox is not None:
            if self.bbox != self.complaint_scan.window_bbox:
                raise ValueError("扫描任务范围必须与计划子窗口一致")
        if self.bbox is not None and self.center is not None:
            raise ValueError("center 与 bbox 只能提供一项")
        if self.center is not None:
            lon, lat = self.center
            if not (-180 <= lon <= 180 and -85 <= lat <= 85):
                raise ValueError("center 必须为 [经度, 纬度]")
        if self.bbox:
            west, south, east, north = self.bbox
            if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
                raise ValueError("bbox 必须为有序的 [西经度, 南纬度, 东经度, 北纬度]")
        if self.date_start and self.date_end and self.date_start > self.date_end:
            raise ValueError("开始日期不得晚于结束日期")
        if self.acquire_optical and not (self.bbox or self.center):
            raise ValueError("自动取图需要 center 或 bbox")
        if self.acquire_optical and len(self.asset_ids) >= 8:
            raise ValueError("自动取图时最多同时引用七张上传影像")
        return self


class ImageMetadata(Record):
    role: Literal["current", "baseline", "target_crop", "local_context", "full_scene", "sar", "false_color"] = "current"
    modality: Literal["optical", "multispectral", "hyperspectral", "sar"] = "optical"
    acquisition_time: datetime | None = None
    resolution_m: float | None = Field(default=None, gt=0)
    crs: str | None = None
    georeferenced: bool = False
    registration_verified: bool = False
    radiometric_comparability_verified: bool = False
    source: str = Field(default="user_upload", max_length=200)
    metadata_scope: str = Field(default="user_declared_unverified", max_length=200)
    period_label: str | None = Field(default=None, max_length=300)


class UploadRequest(Record):
    filename: str = Field(min_length=1, max_length=180)
    content_base64: str = Field(max_length=28_000_000)
    metadata: ImageMetadata = Field(default_factory=ImageMetadata)


class Observation(Record):
    asset_id: str
    statement: str = Field(min_length=1, max_length=2000)
    pixel_window: tuple[int, int, int, int] | None = None


class Candidate(Record):
    label: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    evidence_indices: list[int] = Field(min_length=1)
    alternative_explanations: list[str] = Field(min_length=1, max_length=5)
    verification: list[str] = Field(min_length=1, max_length=10)


class Recognition(Record):
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    candidates: list[Candidate] = Field(default_factory=list, max_length=30)
    quality_issues: list[str] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=30)


class ReportNarrative(Record):
    summary: str
    consumer_conclusion: str = Field(default='', max_length=600,
        description='首页单段结论，120至220字、最多300字。三至四个短句回答发现、近似类型和具体位置、可支持的时间、首要核查事项；逐图依据写入summary。')
    recommendations: list[str]
    cited_policy_ids: list[str] = Field(default_factory=list)
