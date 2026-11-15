"""Bounded, read-only XLSX extraction with cell and embedded-picture provenance.

Never evaluates formulas, opens external links, executes macros or interprets
cell text as instructions. Coordinate and unit declarations stay unverified.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import re
from collections import Counter
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAW = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
ART = "http://schemas.openxmlformats.org/drawingml/2006/main"
MAX_WORKBOOK_BYTES = 64 * 1024 * 1024


def _xml(archive: ZipFile, part: str) -> ET.Element:
    info = archive.getinfo(part)
    if info.file_size > 16 * 1024 * 1024:
        raise ValueError("工作簿 XML 部件过大")
    content = archive.read(part)
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
        raise ValueError("工作簿不允许 DTD 或实体声明")
    return ET.fromstring(content)


def _target(part: str, target: str) -> str:
    if ":" in target or "\\" in target:
        raise ValueError("无效工作簿内部关系")
    result = posixpath.normpath(target.lstrip("/") if target.startswith("/") else
                              posixpath.join(posixpath.dirname(part), target))
    if result.startswith("../") or result == "..":
        raise ValueError("工作簿内部关系越界")
    return result


def _relationships(archive: ZipFile, part: str) -> dict:
    rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if rel_part not in archive.namelist():
        return {}
    return {node.attrib["Id"]: {**node.attrib,
            "part": None if node.attrib.get("TargetMode") == "External" else _target(part, node.attrib["Target"])}
            for node in _xml(archive, rel_part)}


def _column(index: int) -> str:
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def _label(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _unit(header: str) -> str:
    for pattern, unit in ((r"平方千米|km[²2]", "km2"), (r"平方米|㎡|m[²2]", "m2"),
                          (r"公顷|ha", "ha"), (r"亩", "亩")):
        if re.search(pattern, header, re.I):
            return unit
    return "unknown"


def _coordinate_system(values: list[dict]) -> dict:
    declarations = []
    for cell in values:
        text = str(cell["value"] or "")
        if re.search(r"(?:WGS\s*[-_]?\s*84|EPSG\s*:\s*4326|CGCS\s*2000|EPSG\s*:\s*4490|GCJ\s*[-_]?\s*02|BD\s*[-_]?\s*09|坐标系|坐标系统|大地基准)", text, re.I):
            declarations.append({"cell": cell["cell"], "value": text[:500]})
    systems = set()
    for declaration in declarations:
        text = declaration["value"]
        for pattern, system in ((r"WGS\s*[-_]?\s*84|4326", "wgs84"), (r"CGCS\s*2000|4490", "cgcs2000"),
                                (r"GCJ\s*[-_]?\s*02", "gcj02"), (r"BD\s*[-_]?\s*09", "bd09")):
            if re.search(pattern, text, re.I):
                systems.add(system)
    return {"value": next(iter(systems)) if len(systems) == 1 else "unknown",
            "status": "user_declared_unverified" if len(systems) == 1 else "unknown",
            "declarations": declarations[:20]}


def parse_workbook(content: bytes, filename: str) -> dict:
    if not content or len(content) > MAX_WORKBOOK_BYTES:
        raise ValueError("XLSX 为空或超过64MiB")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if (len(entries) > 4000 or len({entry.filename for entry in entries}) != len(entries)
                    or sum(entry.file_size for entry in entries) > 256 * 1024 * 1024
                    or any(entry.flag_bits & 1 for entry in entries)):
                raise ValueError("工作簿压缩包超过安全读取限制或已加密")
            if any(name.endswith("vbaProject.bin") for name in archive.namelist()):
                raise ValueError("不接受含宏的工作簿")
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(t.text or "" for t in node.iter(f"{{{NS}}}t"))
                          for node in _xml(archive, "xl/sharedStrings.xml")]
            workbook = _xml(archive, "xl/workbook.xml")
            workbook_rels = _relationships(archive, "xl/workbook.xml")
            sheets, records, hyperlinks, image_count, external_count = [], [], [], 0, 0
            for sheet_index, sheet in enumerate(workbook.findall(f"{{{NS}}}sheets/{{{NS}}}sheet"), 1):
                if sheet_index > 30:
                    raise ValueError("工作簿超过30张表")
                relation = workbook_rels.get(sheet.attrib.get(f"{{{REL}}}id"), {})
                if not relation.get("part"):
                    continue
                part, title = relation["part"], sheet.attrib["name"]
                root = _xml(archive, part)
                sheet_rels = _relationships(archive, part)
                values, by_row = [], {}
                for row in root.findall(f"{{{NS}}}sheetData/{{{NS}}}row"):
                    row_number = int(row.attrib["r"])
                    if row_number > 20000:
                        raise ValueError("工作簿超过20000行，请分卷")
                    row_cells = []
                    for cell in row.findall(f"{{{NS}}}c"):
                        address = cell.attrib["r"]
                        column = re.sub(r"\d", "", address)
                        value_node, formula = cell.find(f"{{{NS}}}v"), cell.find(f"{{{NS}}}f")
                        raw = value_node.text if value_node is not None else None
                        cell_type = cell.attrib.get("t", "n")
                        if cell_type == "s":
                            value = shared[int(raw)] if raw is not None else None
                        elif cell_type == "inlineStr":
                            value = "".join(t.text or "" for t in cell.iter(f"{{{NS}}}t"))
                        elif cell_type in ("str", "e", "b", "d"):
                            value = raw
                        else:
                            value = _number(raw)
                            if value is not None and value.is_integer():
                                value = int(value)
                        if value is None and formula is None:
                            continue
                        if isinstance(value, str) and len(value) > 10000:
                            raise ValueError("工作簿单元格文本过长")
                        entry = {"cell": address, "column": column, "value": value,
                                 "value_status": "formula_cached_unverified" if formula is not None else "source_value",
                                 "formula": formula.text if formula is not None else None}
                        values.append(entry)
                        row_cells.append(entry)
                        if len(values) > 100000:
                            raise ValueError("工作簿有效单元格过多")
                    if row_cells:
                        by_row[row_number] = row_cells
                header_row = max((n for n in by_row if n <= 20), default=0,
                    key=lambda n: sum(bool(re.search(r"经度|纬度|编码|地址|面积|名称|编号|longitude|latitude", str(c["value"]), re.I)) for c in by_row[n]))
                headers = {c["column"]: str(c["value"] or "") for c in by_row.get(header_row, [])}
                coordinate_system = _coordinate_system(values[:2000])
                anchors = []
                for link in root.findall(f"{{{NS}}}hyperlinks/{{{NS}}}hyperlink"):
                    rel = sheet_rels.get(link.attrib.get(f"{{{REL}}}id"), {})
                    hyperlinks.append({"sheet": title, "cell": link.attrib.get("ref"), "target": rel.get("Target") or link.attrib.get("location"), "content_trust": "untrusted_external_data"})
                for drawing in root.findall(f"{{{NS}}}drawing"):
                    drawing_rel = sheet_rels.get(drawing.attrib.get(f"{{{REL}}}id"), {})
                    drawing_part = drawing_rel.get("part")
                    if not drawing_part:
                        continue
                    drawing_rels = _relationships(archive, drawing_part)
                    external_count += sum(rel.get("TargetMode") == "External" for rel in drawing_rels.values())
                    for anchor in _xml(archive, drawing_part):
                        start = anchor.find(f"{{{DRAW}}}from")
                        if start is None:
                            continue
                        row_number = int(start.findtext(f"{{{DRAW}}}row", "0")) + 1
                        column = _column(int(start.findtext(f"{{{DRAW}}}col", "0")) + 1)
                        for blip in anchor.iter(f"{{{ART}}}blip"):
                            image = drawing_rels.get(blip.attrib.get(f"{{{REL}}}embed"), {})
                            member = image.get("part")
                            if not member or member not in archive.namelist():
                                continue
                            header = headers.get(column, "")
                            role = "baseline" if "变化前" in header or "整改前" in header else "current" if "变化后" in header or "整改后" in header else "local_context"
                            anchors.append({"archive_member": member, "sheet": title, "anchor_cell": f"{column}{row_number}",
                                            "row": row_number, "column": column, "column_label": header,
                                            "role": role, "period_label": header if re.search(r"\d{4}", header) else None,
                                            "bytes": archive.getinfo(member).file_size,
                                            "acquisition_time": None, "content_trust": "user_supplied_unverified"})
                image_count += len(anchors)
                field_names = {column: _label(header) for column, header in headers.items()}
                longitude_columns = [column for column, header in field_names.items() if re.search(r"经度|longitude|^lon$", header, re.I)]
                latitude_columns = [column for column, header in field_names.items() if re.search(r"纬度|latitude|^lat$", header, re.I)]
                for row_number, cells in by_row.items():
                    if row_number <= header_row:
                        continue
                    by_column = {cell["column"]: cell for cell in cells}
                    def find(pattern: str):
                        return next((cell for col, cell in by_column.items() if re.search(pattern, field_names.get(col, ""), re.I)), None)
                    identity = find(r"唯一编码|点位编码|点位编号|^编号$|^ID$")
                    longitude_cell = by_column.get(longitude_columns[0]) if longitude_columns else None
                    latitude_cell = by_column.get(latitude_columns[0]) if latitude_columns else None
                    axis_status = "declared_headers"
                    if latitude_cell is None and len(longitude_columns) == 2:
                        candidate = by_column.get(longitude_columns[1])
                        inferred_x = _number(longitude_cell["value"]) if longitude_cell else None
                        inferred_y = _number(candidate["value"]) if candidate else None
                        if inferred_x is not None and inferred_y is not None and abs(inferred_x) > 90 and abs(inferred_y) <= 90:
                            latitude_cell, axis_status = candidate, "inferred_duplicate_longitude_header"
                    x = _number(longitude_cell["value"]) if longitude_cell else None
                    y = _number(latitude_cell["value"]) if latitude_cell else None
                    numeric_valid = x is not None and y is not None and -180 <= x <= 180 and -90 <= y <= 90
                    area_cell = find(r"^(?:占地)?面积(?:[（(].*)?$")
                    def field(cell):
                        if cell is None:
                            return None
                        header = headers.get(cell["column"], "")
                        return {"raw": cell["value"], "value": _number(cell["value"]), "unit": _unit(header),
                                "header": header, "cell": cell["cell"], "value_status": cell["value_status"]}
                    record_id = f"S{sheet_index:02d}-R{row_number:05d}"
                    point = {"record_id": record_id, "site_id": str(identity["value"]) if identity else record_id,
                             "source": {"sheet": title, "row": row_number, "header_row": header_row},
                             "coordinates": {"longitude": x, "latitude": y, "numeric_valid": numeric_valid,
                                "coordinate_system": coordinate_system["value"], "coordinate_system_status": coordinate_system["status"],
                                "axis_mapping_status": axis_status, "longitude_cell": longitude_cell["cell"] if longitude_cell else None,
                                "latitude_cell": latitude_cell["cell"] if latitude_cell else None,
                                "verified": False},
                             "area": field(area_cell), "change_area": field(find(r"变化面积")),
                             "administrative_area": (find(r"行政区|区县") or {}).get("value"),
                             "address": (find(r"详细地址|^地址$") or {}).get("value"),
                             "period_label": (find(r"批次名称|监测时间|监测日期|日期") or {}).get("value"),
                             "change_type": (find(r"变化类型") or {}).get("value"),
                             "cells": [{**cell, "header": headers.get(cell["column"], "")} for cell in cells],
                             "embedded_images": [image for image in anchors if image["row"] == row_number],
                             "content_trust": "user_supplied_unverified"}
                    records.append(point)
                duplicate_headers = [header for header, count in Counter(headers.values()).items() if count > 1]
                sheets.append({"sheet": title, "header_row": header_row, "headers": headers,
                               "data_rows": sum(record["source"]["sheet"] == title for record in records),
                               "coordinate_system": coordinate_system, "duplicate_headers": duplicate_headers,
                               "embedded_image_count": len(anchors), "formula_count": sum(cell["formula"] is not None for cell in values)})
            media_files = [entry for entry in entries if entry.filename.startswith("xl/media/") and not entry.is_dir()]
            profile = {"filename": filename, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                       "sheets": sheets, "record_count": len(records), "valid_numeric_coordinate_count": sum(point["coordinates"]["numeric_valid"] for point in records),
                       "embedded_image_count": image_count, "media_file_count": len(media_files),
                       "media_uncompressed_bytes": sum(entry.file_size for entry in media_files),
                       "hyperlinks": hyperlinks, "external_drawing_relationships": external_count,
                       "warnings": ["坐标值、表内类别和说明属于用户资料，不是已核验结论", "坐标系统缺失时保持 unknown；重复经度表头推断需复核", "期间标签不等于逐图拍摄日期；未标注面积单位不换算为平方米", "图片保持原始字节，表内人工标签不能当作独立盲评标签"]}
            return {"schema_version": "wuyu_workbook_v1", "profile": profile, "records": records,
                    "content_trust": "user_supplied_unverified"}
    except (BadZipFile, KeyError, ET.ParseError, IndexError, TypeError, OverflowError) as error:
        raise ValueError("XLSX 结构无法读取或不受支持") from error


def summary_text(workbook: dict) -> str:
    profile = workbook["profile"]
    return json.dumps({"document_kind": "xlsx", "content_trust": "user_supplied_unverified",
        "record_count": profile["record_count"], "embedded_image_count": profile["embedded_image_count"],
        "sheets": [{"sheet": s["sheet"], "header_row": s["header_row"], "headers": s["headers"], "coordinate_system": s["coordinate_system"]} for s in profile["sheets"]],
        "warnings": profile["warnings"], "sample_record_ids": [record["record_id"] for record in workbook["records"][:5]],
        "next_step": "按记录ID查询逐点资料与嵌入图片，不要把全表加入模型上下文"}, ensure_ascii=False)
