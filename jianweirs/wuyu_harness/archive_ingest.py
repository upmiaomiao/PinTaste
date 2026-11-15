"""Bounded, read-only ZIP imagery manifests; archive paths are never extracted.

Original numeric rasters remain numeric files. A display preview is a separate
derived artifact, not calibrated spectral/SAR data or a sensor identification.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import re
import stat
import unicodedata
import warnings
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from PIL import Image


MIB = 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
METADATA_SUFFIXES = {".json", ".prj", ".tfw", ".jgw", ".csv"}
ALLOWED_SUFFIXES = IMAGE_SUFFIXES | METADATA_SUFFIXES


class ArchiveIngestError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ArchiveLimits:
    max_zip_bytes: int = 64 * MIB
    max_file_bytes: int = 64 * MIB
    max_expanded_bytes: int = 128 * MIB
    max_entries: int = 128
    max_images: int = 16
    max_compression_ratio: int = 200
    max_image_pixels: int = 40_000_000
    max_raster_bands: int = 256
    max_metadata_bytes: int = 2 * MIB
    max_csv_rows: int = 2000
    max_preview_dimension: int = 1536
    task_image_limit: int = 8

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ArchiveIngestError("invalid_archive_limits", "压缩包处理限制需为正整数。")
        if (self.max_zip_bytes > 64*MIB or self.max_file_bytes > 64*MIB or self.max_expanded_bytes > 128*MIB
                or self.max_entries > 128 or self.max_images > 16 or self.max_compression_ratio > 200
                or self.max_image_pixels > 40_000_000 or self.max_raster_bands > 256
                or self.max_metadata_bytes > 2*MIB or self.max_csv_rows > 2000
                or self.max_preview_dimension > 2048 or self.task_image_limit > 8):
            raise ArchiveIngestError("invalid_archive_limits", "不能扩大内置压缩包处理上限。")


def _safe_name(info) -> str:
    name = getattr(info, "orig_filename", info.filename)
    if (not isinstance(name, str) or not name or len(name) > 300 or "\\" in name or any(c in name for c in ':<>"|?*')
            or name.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ArchiveIngestError("unsafe_archive_path", "压缩包包含绝对路径、控制字符或不安全文件名。")
    parts = name[:-1].split("/") if info.is_dir() else name.split("/")
    if (not parts or len(parts) > 12 or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
            or any(re.fullmatch(r"(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])", part.split(".")[0].rstrip(" ."), re.I) for part in parts)):
        raise ArchiveIngestError("unsafe_archive_path", "压缩包包含越界路径或 Windows 保留名称。")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if (kind not in (0, stat.S_IFREG, stat.S_IFDIR) or stat.S_ISLNK(mode) or info.external_attr & 0x400
            or ((kind == stat.S_IFDIR or info.external_attr & 0x10) and not info.is_dir())):
        raise ArchiveIngestError("unsafe_archive_entry", "压缩包不接受符号链接、重解析点或特殊文件。")
    return name


def _short_text(value, maximum=300):
    return value[:maximum] if isinstance(value, str) else ""


def _read_limited(archive: ZipFile, info, maximum: int) -> bytes:
    chunks, size = [], 0
    try:
        with archive.open(info, "r") as stream:
            while True:
                chunk = stream.read(min(1024*1024, maximum-size+1))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum or size > info.file_size:
                    raise ArchiveIngestError("archive_expansion_exceeded", "压缩包成员实际展开大小超过限制或与目录不一致。")
                chunks.append(chunk)
    except (BadZipFile, RuntimeError, EOFError, NotImplementedError, OSError):
        raise ArchiveIngestError("invalid_archive", "压缩包成员损坏、加密或无法安全读取。") from None
    if size != info.file_size:
        raise ArchiveIngestError("invalid_archive", "压缩包成员大小与目录不一致。")
    return b"".join(chunks)


def _display_image(raw: bytes, suffix: str, limits: ArchiveLimits) -> dict:
    expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[suffix]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != expected or image.width*image.height > limits.max_image_pixels or getattr(image, "n_frames", 1) != 1:
                    raise ValueError
                dimensions, mode = [image.width, image.height], image.mode
                image.verify()
        return {"format": expected, "width": dimensions[0], "height": dimensions[1], "mode": mode,
                "import_kind": "display_image", "preview_status": "available", "sensor_modality": "unclassified",
                "native_numeric_measurements_preserved": False, "georeferenced": False,
                "spatial_registration_verified": False}
    except (OSError, ValueError, SyntaxError, OverflowError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ArchiveIngestError("invalid_display_image", "文件不是声明的单帧 PNG/JPEG/WebP，或像素数超过上限。") from None


def _tiff_profile(raw: bytes, limits: ArchiveLimits) -> dict:
    # Refuse VRT/other GDAL formats even if an attacker gives them a .tif suffix.
    if raw[:4] not in {b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"}:
        raise ArchiveIngestError("invalid_numeric_raster", "TIFF 文件头无效；不接受伪装的外部数据引用。")
    try:
        import rasterio
        from rasterio.io import MemoryFile
        with rasterio.Env(GDAL_PAM_ENABLED=False, GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_SKIP="VRT"):
            with MemoryFile(raw) as memory, memory.open(driver="GTiff") as raster:
                if raster.driver != "GTiff" or raster.width*raster.height > limits.max_image_pixels or not 1 <= raster.count <= limits.max_raster_bands:
                    raise ValueError
                color = [value.name for value in raster.colorinterp]
                rgb = [color.index(name)+1 for name in ("red", "green", "blue")] if all(name in color for name in ("red", "green", "blue")) else None
                nodata = raster.nodata
                affine, bounds = list(raster.transform.to_gdal()), list(raster.bounds)
                coordinate_metadata_finite = all(math.isfinite(value) for value in affine+bounds)
                complex_values = any(dtype.startswith("complex") for dtype in raster.dtypes)
                return {"format": "GTiff", "import_kind": "numeric_raster", "width": raster.width,
                    "height": raster.height, "band_count": raster.count, "dtypes": list(raster.dtypes),
                    "color_interpretations": color, "rgb_bands": rgb, "band_descriptions": [_short_text(value) or None for value in raster.descriptions],
                    "crs": str(raster.crs) if raster.crs else None, "affine_gdal": affine if coordinate_metadata_finite else None,
                    "raster_bounds": bounds if coordinate_metadata_finite else None, "coordinate_metadata_finite": coordinate_metadata_finite,
                    "nodata": nodata if nodata is not None and math.isfinite(nodata) else None,
                    "nodata_is_nan": nodata is not None and math.isnan(nodata),
                    "georeferenced": raster.crs is not None and coordinate_metadata_finite, "spatial_registration_verified": False,
                    "sensor_modality": "unclassified", "native_numeric_measurements_preserved": True,
                    "preview_status": "unsupported_complex" if complex_values else "available" if rgb or raster.count == 1 else "requires_explicit_rgb_bands",
                    "preview_is_numeric_analysis_data": False,
                    "coordinate_metadata_scope": "embedded_file_metadata_unverified"}
    except ImportError:
        raise ArchiveIngestError("rasterio_unavailable", "当前运行环境未安装 rasterio，不能读取 TIFF 元数据或生成数值影像预览。") from None
    except ArchiveIngestError:
        raise
    except Exception:
        raise ArchiveIngestError("invalid_numeric_raster", "TIFF 无法读取或尺寸、波段数量超过限制。") from None


def _decode_text(raw: bytes) -> tuple[str, str]:
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            raise ArchiveIngestError("unsupported_text_encoding", "资料需为 UTF-8 或 GB18030 编码。") from None


def _finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _csv_profile(text: str, limits: ArchiveLimits) -> tuple[dict, list[dict]]:
    try:
        reader = csv.reader(io.StringIO(text))
        headers = next(reader, [])
        if not headers or len(headers) > 64 or len(set(headers)) != len(headers) or any(len(h) > 200 for h in headers):
            raise ValueError
        compact = [re.sub(r"\s+", "", header).lower() for header in headers]
        def index(names):
            return next((i for i, header in enumerate(compact) if header in names), None)
        lon = index({"经度", "中心经度", "longitude", "lon", "lng"})
        lat = index({"纬度", "中心纬度", "latitude", "lat"})
        identity = index({"点位编号", "点位名称", "名称", "site_id", "siteid", "name", "id"})
        crs = index({"坐标系", "坐标系统", "crs", "coordinate_system"})
        points, row_count = [], 0
        for row in reader:
            if not row or not any(value.strip() for value in row):
                continue
            row_count += 1
            if row_count > limits.max_csv_rows or len(row) != len(headers) or any(len(value) > 2000 for value in row):
                raise ValueError
            if lon is None or lat is None:
                continue
            x, y = _finite_number(row[lon]), _finite_number(row[lat])
            if x is not None and y is not None and -180 <= x <= 180 and -85 <= y <= 85:
                points.append({"row": reader.line_num, "label": row[identity][:200] if identity is not None else f"第{reader.line_num}行",
                    "coordinates": [x, y], "declared_crs": row[crs][:100] if crs is not None else "unknown",
                    "coordinate_system_verified": False, "content_trust": "user_supplied_unverified"})
        return {"headers": headers, "row_count": row_count, "point_candidate_count": len(points),
                "point_preview": points[:20], "formulas_executed": False}, points
    except (csv.Error, ValueError):
        raise ArchiveIngestError("invalid_point_csv", "CSV 表头、字段或记录数不符合受限点位表格式。") from None


def _metadata_profile(raw: bytes, suffix: str, limits: ArchiveLimits) -> tuple[dict, list[dict]]:
    if len(raw) > limits.max_metadata_bytes:
        raise ArchiveIngestError("metadata_too_large", "文本元数据超过单文件2MiB读取上限。")
    text, encoding = _decode_text(raw)
    base = {"import_kind": "metadata_candidate", "encoding": encoding,
            "content_trust": "user_supplied_unverified", "auto_applied_to_images": False}
    if suffix == ".csv":
        profile, points = _csv_profile(text, limits)
        return {**base, "metadata_type": "point_csv", **profile}, points
    if suffix == ".json":
        try:
            value = json.loads(text)
            if not isinstance(value, (dict, list)):
                raise ValueError
            return {**base, "metadata_type": "json", "top_level_type": type(value).__name__,
                    "keys": [_short_text(key, 200) for key in list(value)[:30]] if isinstance(value, dict) else [],
                    "item_count": len(value), "external_links_followed": False}, []
        except (ValueError, RecursionError):
            raise ArchiveIngestError("invalid_json_metadata", "JSON 元数据无法解析为对象或列表。") from None
    if suffix in {".tfw", ".jgw"}:
        try:
            values = [float(line.strip()) for line in text.splitlines() if line.strip()]
            if len(values) != 6 or not all(math.isfinite(value) for value in values):
                raise ValueError
            a, d, b, e, c, f = values
            return {**base, "metadata_type": "world_file", "world_file_values": values,
                    "candidate_affine_gdal": [c-a/2-b/2, a, b, f-d/2-e/2, d, e], "crs": None}, []
        except ValueError:
            raise ArchiveIngestError("invalid_world_file", "TFW/JGW 需为六个有限数值；文件本身不声明坐标系。") from None
    return {**base, "metadata_type": "prj_wkt_candidate", "wkt_preview": text[:1000],
            "crs_verified": False}, []


class ArchivePackage:
    """Build a read-only import plan and release only bounded, validated members."""

    def __init__(self, content: bytes, filename: str = "imagery.zip", *, limits: ArchiveLimits | None = None):
        self.limits = limits or ArchiveLimits()
        if not isinstance(content, bytes) or not content or len(content) > self.limits.max_zip_bytes:
            raise ArchiveIngestError("archive_size_exceeded", "ZIP 为空或超过64MiB源文件上限。")
        if not isinstance(filename, str) or not filename.lower().endswith(".zip") or len(filename) > 300 or any(ord(c) < 32 for c in filename):
            raise ArchiveIngestError("invalid_archive_filename", "请提供 ZIP 压缩包。")
        self._content, self._members, self._points = content, {}, {}
        self._plan = {"archive_filename": filename, "archive_sha256": hashlib.sha256(content).hexdigest(),
            "archive_bytes": len(content), "files": [], "directories": [], "limits": asdict(self.limits),
            "content_trust": "user_supplied_unverified", "extracted_to_disk": False,
            "supported_image_formats": ["PNG", "JPEG", "WebP", "TIFF metadata and explicit display preview"],
            "unsupported_formats_policy": "listed_and_hashed_only_never_executed_or_recursively_opened",
            "task_image_limit": self.limits.task_image_limit,
            "sensor_classification_inferred": False}
        self._inspect()

    def _inspect(self):
        try:
            with ZipFile(io.BytesIO(self._content)) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > self.limits.max_entries:
                    raise ArchiveIngestError("archive_entry_limit", "ZIP 成员为空或超过128项上限。")
                seen, expanded, image_count = set(), 0, 0
                for info in infos:
                    name = _safe_name(info)
                    canonical = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
                    if canonical in seen:
                        raise ArchiveIngestError("duplicate_archive_path", "ZIP 包含在 Windows 上冲突的重复名称。")
                    seen.add(canonical)
                    if info.flag_bits & 0x41:
                        raise ArchiveIngestError("encrypted_archive", "不接受加密 ZIP 成员。")
                    if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                        raise ArchiveIngestError("unsupported_zip_compression", "ZIP 仅支持存储或 DEFLATE 压缩。")
                    if info.file_size > self.limits.max_file_bytes or info.file_size < 0:
                        raise ArchiveIngestError("archive_member_size_exceeded", "ZIP 单个文件超过64MiB限制。")
                    if info.file_size and (info.compress_size <= 0 or info.file_size/info.compress_size > self.limits.max_compression_ratio):
                        raise ArchiveIngestError("archive_compression_ratio_exceeded", "ZIP 成员压缩比例超过200倍安全限制。")
                    expanded += info.file_size
                    if expanded > self.limits.max_expanded_bytes:
                        raise ArchiveIngestError("archive_total_size_exceeded", "ZIP 总展开大小超过128MiB限制。")
                    if info.is_dir():
                        if info.file_size:
                            raise ArchiveIngestError("invalid_archive", "ZIP 目录含有不允许的数据。")
                        self._plan["directories"].append(name)
                        continue
                    suffix = PurePosixPath(name).suffix.lower()
                    if suffix in IMAGE_SUFFIXES:
                        image_count += 1
                        if image_count > self.limits.max_images:
                            raise ArchiveIngestError("archive_image_limit", "ZIP 影像候选超过16张，请分包。")
                    self._members[hashlib.sha256(name.encode()).hexdigest()[:32]] = {"info": info, "name": name, "suffix": suffix}
                # A file named 'folder' and a member 'folder/image.png' would
                # conflict if a later adapter extracted them; reject it now.
                file_names = {unicodedata.normalize("NFC", record["name"]).casefold() for record in self._members.values()}
                for record in self._members.values():
                    parts = PurePosixPath(record["name"]).parts
                    if any(unicodedata.normalize("NFC", "/".join(parts[:n])).casefold() in file_names for n in range(1, len(parts))):
                        raise ArchiveIngestError("conflicting_archive_path", "ZIP 文件与子目录路径冲突。")
                self._plan["declared_expanded_bytes"] = expanded
                for member_id, record in self._members.items():
                    raw = _read_limited(archive, record["info"], self.limits.max_file_bytes)
                    row = {"id": member_id, "original_filename": record["name"], "suffix": record["suffix"],
                           "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                           "compressed_bytes": record["info"].compress_size, "status": "unsupported",
                           "import_kind": "unsupported", "content_trust": "user_supplied_unverified"}
                    if record["suffix"] in ALLOWED_SUFFIXES:
                        try:
                            if record["suffix"] in {".tif", ".tiff"}:
                                profile = _tiff_profile(raw, self.limits)
                            elif record["suffix"] in IMAGE_SUFFIXES:
                                profile = _display_image(raw, record["suffix"], self.limits)
                            else:
                                profile, points = _metadata_profile(raw, record["suffix"], self.limits)
                                self._points[member_id] = points
                            row.update(profile, status="ready")
                        except ArchiveIngestError as error:
                            row.update(status="invalid_or_unreadable", error_code=error.code, message=str(error))
                    else:
                        row.update(reason="format_not_supported_by_archive_adapter")
                    record["row"] = row
                    self._plan["files"].append(row)
        except ArchiveIngestError:
            raise
        except (BadZipFile, OSError, EOFError, ValueError):
            raise ArchiveIngestError("invalid_archive", "ZIP 结构损坏或无法安全读取。") from None
        images = [row for row in self._plan["files"] if row["status"] == "ready" and row["suffix"] in IMAGE_SUFFIXES]
        self._plan.update(image_count=len(images), supported_file_count=sum(row["status"] == "ready" for row in self._plan["files"]),
            unsupported_file_count=sum(row["status"] == "unsupported" for row in self._plan["files"]),
            invalid_file_count=sum(row["status"] == "invalid_or_unreadable" for row in self._plan["files"]),
            batch_required=len(images) > self.limits.task_image_limit,
            image_batches=[[row["id"] for row in images[i:i+self.limits.task_image_limit]] for i in range(0, len(images), self.limits.task_image_limit)],
            budget_note="单任务最多8张影像；超过需分批处理，导入成功不代表已分析、已配准或已识别传感器。")
        by_stem = {}
        for row in self._plan["files"]:
            if row["status"] == "ready" and row["suffix"] in {".prj", ".tfw", ".jgw", ".json"}:
                by_stem.setdefault(str(PurePosixPath(row["original_filename"]).with_suffix("")).casefold(), []).append(row["id"])
        for row in images:
            row["metadata_candidate_ids"] = by_stem.get(str(PurePosixPath(row["original_filename"]).with_suffix("")).casefold(), [])

    def plan(self) -> dict:
        return deepcopy(self._plan)

    def read_member(self, member_id: str) -> bytes:
        record = self._members.get(member_id)
        if not record or record["row"]["status"] != "ready":
            raise ArchiveIngestError("archive_member_unavailable", "该成员未通过受支持格式验证。")
        with ZipFile(io.BytesIO(self._content)) as archive:
            return _read_limited(archive, record["info"], self.limits.max_file_bytes)

    def point_rows(self, member_id: str, *, offset: int = 0, limit: int = 100) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100 or member_id not in self._points:
            raise ArchiveIngestError("invalid_point_page", "点位表分页或成员编号无效。")
        points = self._points[member_id]
        return {"total": len(points), "offset": offset, "limit": limit, "points": deepcopy(points[offset:offset+limit])}

    def preview(self, member_id: str, *, rgb_bands: list[int] | None = None) -> tuple[bytes, dict]:
        record = self._members.get(member_id)
        if not record or record["suffix"] not in IMAGE_SUFFIXES or record["row"]["status"] != "ready":
            raise ArchiveIngestError("archive_preview_unavailable", "该成员没有可用影像预览。")
        row, raw = record["row"], self.read_member(member_id)
        metadata = {"original_member_id": member_id, "original_sha256": row["sha256"],
            "derived_display_only": True, "numeric_analysis_data": False, "sensor_modality": "unclassified",
            "spatial_registration_verified": False, "original_numeric_file_preserved": row["import_kind"] == "numeric_raster"}
        if record["suffix"] in {".tif", ".tiff"}:
            import numpy as np
            import rasterio
            from rasterio.enums import Resampling
            from rasterio.io import MemoryFile
            chosen = rgb_bands if rgb_bands is not None else row.get("rgb_bands")
            if chosen is None and row["band_count"] == 1:
                chosen = [1]
            if not isinstance(chosen, list) or len(chosen) not in {1, 3} or any(type(b) is not int or not 1 <= b <= row["band_count"] for b in chosen) or len(set(chosen)) != len(chosen):
                raise ArchiveIngestError("explicit_rgb_bands_required", "未声明 RGB 顺序的多波段 TIFF 需明确选择三个不同波段，不能自动猜测传感器或真彩色。")
            if rgb_bands is not None and len(chosen) != 3:
                raise ArchiveIngestError("explicit_rgb_bands_required", "rgb_bands 需明确指定三个不同波段。")
            ratio = min(1, self.limits.max_preview_dimension/max(row["width"], row["height"]))
            width, height = max(1, round(row["width"]*ratio)), max(1, round(row["height"]*ratio))
            try:
                with rasterio.Env(GDAL_PAM_ENABLED=False, GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_SKIP="VRT"):
                    with MemoryFile(raw) as memory, memory.open(driver="GTiff") as raster:
                        values = raster.read(chosen, out_shape=(len(chosen), height, width), masked=True, resampling=Resampling.nearest)
                if np.iscomplexobj(values):
                    raise ArchiveIngestError("complex_preview_unsupported", "复数 TIFF 需明确的幅度/相位处理工具，当前不自动生成显示图。")
                channels, stretches = [], []
                for band in values:
                    data = np.asarray(band, dtype=np.float64)
                    valid = ~np.ma.getmaskarray(band) & np.isfinite(data)
                    if not valid.any():
                        raise ArchiveIngestError("raster_has_no_valid_pixels", "所选波段没有可显示的有效像元。")
                    low, high = np.percentile(data[valid], [2, 98])
                    channel = np.zeros((height, width), dtype=np.uint8)
                    if high > low:
                        channel[valid] = np.clip((data[valid]-low)/(high-low)*255, 0, 255).astype(np.uint8)
                    else:
                        channel[valid] = 128
                    channels.append(channel)
                    stretches.append({"percentiles": [2, 98], "low": float(low), "high": float(high), "constant_band": bool(high == low), "valid_pixels": int(valid.sum())})
                if len(channels) == 1:
                    channels *= 3
                image = Image.fromarray(np.stack(channels, axis=-1))
                metadata.update(display="explicit_band_composite_percentile_stretch" if rgb_bands is not None else "embedded_rgb_percentile_stretch" if len(chosen) == 3 else "single_band_grayscale_percentile_stretch",
                    rgb_bands=chosen if len(chosen) == 3 else None, grayscale_band=chosen[0] if len(chosen) == 1 else None,
                    stretch_by_input_band=stretches, resampling="nearest_for_display_only", actual_width=width, actual_height=height)
            except ArchiveIngestError:
                raise
            except Exception:
                raise ArchiveIngestError("raster_preview_failed", "无法安全读取所选 TIFF 波段。") from None
        else:
            if rgb_bands is not None:
                raise ArchiveIngestError("invalid_display_bands", "PNG/JPEG/WebP 显示影像不接受科学波段编号。")
            try:
                with Image.open(io.BytesIO(raw)) as original:
                    original.thumbnail((self.limits.max_preview_dimension, self.limits.max_preview_dimension))
                    if "A" in original.getbands():
                        rgba = original.convert("RGBA")
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        image = original.convert("RGB")
            except (OSError, ValueError, SyntaxError, OverflowError, Image.DecompressionBombError):
                raise ArchiveIngestError("display_preview_failed", "无法完整解码该显示影像，请检查源文件。") from None
            metadata.update(display="encoded_image_rgb_display", actual_width=image.width, actual_height=image.height,
                            transparency_display="white_background")
        output = io.BytesIO()
        image.save(output, format="PNG")
        content = output.getvalue()
        metadata.update(media_type="image/png", sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        return content, metadata


def inspect_archive(content: bytes, filename: str = "imagery.zip", *, limits: ArchiveLimits | None = None) -> dict:
    return ArchivePackage(content, filename, limits=limits).plan()
