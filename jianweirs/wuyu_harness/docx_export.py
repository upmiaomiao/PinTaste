"""Evidence-preserving Word exports from a task's controlled artifact directory.

The caller must authorize the task before invoking these functions. This module
does not fetch URLs, execute model text, load asset-store paths, or judge waste.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path, PureWindowsPath
import re
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from PIL import Image
from .recommendations import normalize_recommendations
from .consumer_conclusion import package_consumer_conclusion
from .task_outputs import presentation_contract
from .answer_layout import public_review_text, readable_blocks


def _presentation_sections(doc, contract):
    for section in contract['sections']:
        doc.add_heading(_text(section['heading']), level=1)
        for value in section['paragraphs']:
            _paragraph(doc, value)
        if section['columns'] and section['records']:
            count = len(section['columns'])
            _table(doc, section['columns'], section['records'], [6.9 / count] * count)


SOURCE_NAMES = {
    "arcgis": "Esri 世界影像", "bing": "Bing 影像", "amap": "高德卫星底图",
    "tencent": "腾讯卫星底图", "google": "Google GG 卫星底图",
    "mapquest": "MapQuest 影像", "mapbox": "Mapbox 影像", "tianditu": "天地图影像",
    "siwei": "四维地球影像", "siwei_daily": "四维地球日新图",
    "gaofen2026": "2026高清图源", "century": "世纪空间年度影像", "jilin2024": "吉林一号2024版",
    "siwei2025": "四维地球2025版", "history246": "谷歌历史影像246",
    "sentinel2": "Sentinel 2 多光谱", "landsat": "Landsat 多光谱",
    "sentinel1-rtc": "Sentinel 1 SAR", "sentinel1-grd": "Sentinel 1 SAR",
    "sentinel1-slc": "Sentinel 1 SAR", "emit": "EMIT 高光谱", "enmap": "EnMAP 高光谱",
    "user_upload": "用户上传影像", "user_workbook_embedded": "点位表附图",
}
BOUNDARY = "本报告形成待核查的遥感线索，不替代现场调查、固废或危险废物鉴别、污染检测及执法认定。"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_POINTS = 100


@dataclass(frozen=True)
class DocxExportResult:
    path: Path
    sha256: str
    bytes: int
    warnings: tuple[str, ...]

    def artifact(self) -> dict[str, Any]:
        """A public manifest entry; intentionally excludes the local path."""
        return {"name": self.path.name, "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "sha256": self.sha256, "bytes": self.bytes, "warnings": list(self.warnings)}


def _text(value: Any, limit: int = 8000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "；".join(_text(item, limit) for item in value if item)
    value = re.sub(r"<think(?:ing)?\b[^>]*>[\s\S]*?(?:</think(?:ing)?\s*>|$)", "", str(value), flags=re.I)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = re.sub(r"原生模型最终判读[（(]待核验[）)][:：]?", "", value)
    value = re.sub(r"(?:像素(?:窗|窗口)|归一化(?:框|坐标)|候选框|框)?\s*\[\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*\]", "", value)
    value = re.sub(r"像素框由整图[^。]*[。]?", "", value)
    value = value.replace("normalized_1000", "").replace("risk_level", "筛查等级").replace("confidence", "模型自评值")
    return public_review_text(value.strip())[:limit]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any, limit: int = 100) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)][:limit] if isinstance(value, (list, tuple)) else []


def _folder(path: Path) -> Path:
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("报告目录不存在")
    return root


def _within(root: Path, relative: str, *, directory: bool = False) -> Path:
    # PureWindowsPath also catches drive/UNC paths in Linux deployments.
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("报告附件路径无效")
    supplied = Path(relative)
    if supplied.is_absolute() or PureWindowsPath(relative).is_absolute() or any(part in ("..", ".") for part in supplied.parts):
        raise ValueError("报告附件必须位于当前任务目录")
    candidate = (root / supplied).resolve(strict=True)
    if not candidate.is_relative_to(root) or not (candidate.is_dir() if directory else candidate.is_file()):
        raise ValueError("报告附件必须位于当前任务目录")
    return candidate


def _output(root: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not re.fullmatch(r"[\w\-\u3400-\u9fff][\w\-\u3400-\u9fff ]{0,100}\.docx", filename, flags=re.U):
        raise ValueError("报告文件名须为当前任务目录内的 DOCX 文件名")
    result = root / filename
    if result.is_symlink() or result.resolve().parent != root:
        raise ValueError("报告输出路径无效")
    return result


def _font(properties: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    fonts = properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        properties.insert(0, fonts)
    for key, value in (("ascii", "Times New Roman"), ("hAnsi", "Times New Roman"),
                       ("cs", "Times New Roman"), ("eastAsia", "宋体")):
        fonts.set(qn("w:" + key), value)
    for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme", "csTheme"):
        fonts.attrib.pop(qn("w:" + attr), None)


def _document(title: str):
    try:
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor
    except ImportError:
        raise RuntimeError("Word 报告导出需要 python-docx>=1.2,<2") from None
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.72)
    section.left_margin = section.right_margin = Inches(.8)
    for style in doc.styles:
        if style.type == 1 or style.type == 2:
            _font(style.element.get_or_add_rPr())
            style.font.color.rgb = RGBColor(0, 0, 0)
        # Word's default template carries blue title/subtitle paragraph rules.
        # Remove them from all styles; table borders are deliberately separate.
        for border in list(style.element.xpath("./w:pPr/w:pBdr")):
            border.getparent().remove(border)
    for name, size, before, after in (("Normal", 12, 0, 7), ("Title", 20, 0, 14),
                                       ("Heading 1", 14, 15, 8), ("Heading 2", 12, 11, 6),
                                       ("Caption", 10.5, 5, 7)):
        style = doc.styles[name]
        style.font.size = Pt(size)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.25
        if name != "Normal":
            style.paragraph_format.keep_with_next = True
    doc.styles["Title"].font.bold = True
    doc.styles["Normal"].paragraph_format.widow_control = True
    doc.styles["Caption"].paragraph_format.keep_with_next = False
    doc.core_properties.title = title
    doc.core_properties.author = "无隅遥感"
    doc.core_properties.last_modified_by = "无隅遥感"
    doc.core_properties.subject = "遥感疑点与核查证据"
    doc.add_paragraph(title, style="Title")
    return doc


def _paragraph(doc: Any, value: Any, *, style: str | None = None) -> None:
    text = _text(value, 32000)
    if text:
        for block in readable_blocks(text).split('\n\n'):
            doc.add_paragraph(block, style=style)


def _table(doc: Any, headers: Sequence[str], records: Sequence[Sequence[Any]], widths: Sequence[float]):
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt
    table = doc.add_table(rows=1, cols=len(headers))
    table.autofit = False
    props = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement("w:" + side)
        for name, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
            border.set(qn("w:" + name), value)
        borders.append(border)
    props.append(borders)
    padding = OxmlElement("w:tblCellMar")
    for side in ("top", "left", "bottom", "right"):
        item = OxmlElement("w:" + side)
        item.set(qn("w:w"), "100")
        item.set(qn("w:type"), "dxa")
        padding.append(item)
    props.append(padding)
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    all_records = [headers, *records]
    for index, values in enumerate(all_records):
        cells = table.rows[0].cells if index == 0 else table.add_row().cells
        for col, cell in enumerate(cells):
            cell.width = Inches(widths[col])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(4)
            para.paragraph_format.line_spacing = 1.15
            para.paragraph_format.keep_with_next = index == 0
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER if col == 0 else WD_ALIGN_PARAGRAPH.LEFT
            run = para.add_run(_text(values[col], 2400))
            run.font.size = Pt(10.5)
            run.bold = index == 0
            if index == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "DFEAF2")
                cell._tc.get_or_add_tcPr().append(shade)
        for col, column in enumerate(table.columns):
            column.width = Inches(widths[col])
    doc.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def _effective_decision(row: Mapping[str, Any]) -> str:
    """The shared evidence rule (evidence.candidate_presence_evidence), per image."""
    from .evidence import effective_image_decision
    return effective_image_decision(row)


def _summary(package: Mapping[str, Any]) -> str:
    """The Word lead is the same projection the page and the HTML report show.

    ``package_consumer_conclusion`` is the single source: it returns the saved
    projection, or derives it from the evidence for older packages. Only a
    research/information answer that has no screening conclusion falls back to
    its own narrative.
    """
    conclusion = package_consumer_conclusion(package)
    if conclusion.get('text'):
        return _text(conclusion['text'])
    task, fusion = _mapping(package.get('task')), _mapping(package.get('fusion'))
    research = (package.get('research_result') or task.get('search_only') or fusion.get('screening_applicable') is False
                or fusion.get('level') == 'not_applicable')
    if research:
        answer = _mapping(package.get('research_result')).get('summary') or _mapping(package.get('narrative')).get('summary')
        return _text(answer) or ('本次未形成可引用的资料回答。' if task.get('search_only') else '本次研究结果以实际取得的数据、日期和分析记录为准。')
    if package.get('recognition_status') not in (None, 'completed'):
        return '无隅见微判读尚未完成，当前不形成固废类型或风险结论。'
    return '现有可审查证据不足，暂不能判断指定区域是否存在疑似堆存。'


def _location(package: Mapping[str, Any]) -> str:
    task = _mapping(package.get("task"))
    if isinstance(task.get("center"), (list, tuple)) and len(task["center"]) == 2:
        return f"{task['center'][0]} {task['center'][1]}"
    coordinate = task.get("point") or task.get("coordinates")
    if isinstance(coordinate, Mapping):
        return _text(coordinate.get("longitude")) + " " + _text(coordinate.get("latitude"))
    if task.get("longitude") is not None and task.get("latitude") is not None:
        return f"{task['longitude']} {task['latitude']}"
    return _text(task.get("place_name") or task.get("address") or task.get("bbox") or "位置待确认", 180)


def _image(root: Path, name: str) -> tuple[BytesIO, int, int]:
    path = _within(root, name)
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("报告影像格式或大小不受支持")
    with Image.open(BytesIO(path.read_bytes())) as image:
        if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("报告影像超过像素限制")
        image.load()
        output = BytesIO()
        image.convert("RGB").save(output, format="PNG")
        output.seek(0)
        return output, image.width, image.height


def _figures(doc: Any, package: Mapping[str, Any], root: Path, warnings: list[str]):
    from docx.oxml import OxmlElement
    from docx.shared import Inches
    for index, asset in enumerate(_rows(package.get("assets"), 24), 1):
        identity = str(asset.get("id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            warnings.append(f"图 {index} 附件标识无效，未读取文件")
            continue
        metadata = _mapping(asset.get("metadata"))
        source = SOURCE_NAMES.get(metadata.get("source"), _text(metadata.get("source")) or "来源未注明")
        doc.add_heading(f"图 {index} {source}", level=2)
        acquired = _text(metadata.get("acquisition_time")) or "未知"
        _paragraph(doc, f"拍摄时间 {acquired}。获取时间与图源菜单中的年份不能代替实际拍摄日期。")
        doc.paragraphs[-1].paragraph_format.keep_with_next = True
        names = []
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            name = f"source-{identity}{suffix}"
            if (root / name).exists():
                names.append(("原始影像", name))
                break
        marked = f"annotated-{identity}.png"
        if (root / marked).exists():
            assessment = next((row for row in _rows(package.get("image_assessments")) if row.get("asset_id") == identity), {})
            reviewed = _mapping(assessment.get("optical_false_positive_review")).get("status") == "completed"
            names.append(("复核后有效标注" if reviewed else "见微候选标注（待复核）", marked))
        if not names:
            warnings.append(f"图 {index} 未找到当前任务目录内的原图或标注图")
            _paragraph(doc, "本次导出未取得可嵌入附件，判读记录保留如下。")
            continue
        # Two views compare the same evidence; neither creates an independent source.
        table = _table(doc, [name for name, _ in names], [], [6.9 / len(names)] * len(names))
        image_row = table.add_row()
        image_row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        cells = image_row.cells
        for col, (label, name) in enumerate(names):
            try:
                stream, width, height = _image(root, name)
                max_width = 6.5 / len(names)
                inches = min(max_width, 2.6 * width / height)
                cells[col].paragraphs[0].add_run().add_picture(stream, width=Inches(inches))
                inline = cells[col]._tc.xpath(".//wp:docPr")[-1]
                inline.set("descr", f"图 {index} {source} {label}")
            except (ValueError, FileNotFoundError, OSError, Image.DecompressionBombError):
                cells[col].paragraphs[0].add_run("附件未能安全读取")
                warnings.append(f"图 {index} {label}未能安全读取")
        if any(_mapping(row.get("optical_false_positive_review")).get("status") == "completed"
               for row in _rows(package.get("image_assessments")) if row.get("asset_id") == identity):
            _paragraph(doc, "标注展示同图复核后保留的有效候选；已排除框不作为固废标注。原始见微结果与原始框保存在完整证据包中。原图与标注图属于同一份证据。", style="Caption")
        else:
            _paragraph(doc, "标注为见微输出的待核查候选位置；本图未记录已完成的同图误报复核。原图与标注图属于同一份证据。", style="Caption")


def _section(doc: Any, package: Mapping[str, Any], root: Path, warnings: list[str], *, include_images: bool) -> None:
    task = _mapping(package.get("task"))
    presentation = presentation_contract(package)
    risk_mode = presentation['enabled'] and presentation['workflow_mode'] == 'environmental_risk'
    _paragraph(doc, _text(task.get("objective")))
    if presentation['enabled'] and presentation['workflow_mode'] == 'contextual_qa':
        doc.add_heading(presentation['heading'], level=1)
        if presentation.get('answer_model') == 'shensi':
            _paragraph(doc, '本轮由无隅慎思结合已有影像证据、所在区域与周边设施及检索资料作答；模型推理过程不作为证据收录。', style="Caption")
        _paragraph(doc, presentation['lead'])
        _presentation_sections(doc, presentation)
        sources = _rows(_mapping(package.get('policy')).get('sources'), 30)
        if sources: doc.add_heading('本轮引用来源', level=1)
        for source in sources:
            _paragraph(doc, _text(source.get('title') or '参考资料'))
            url = _text(source.get('url'), 2048)
            if urlsplit(url).scheme in ('http','https'): _paragraph(doc, url)
        return
    _paragraph(doc, "分析范围 " + _location(package))
    task_results = _mapping(package.get('task_results'))
    result_rows=_rows(task_results.get('results'),10)
    consumer=package_consumer_conclusion(package)
    primary_task=result_rows[0] if len(result_rows)==1 and result_rows[0].get('task_type')!='suspected_site_discovery' else None
    research = bool(package.get('research_result')) or _mapping(package.get('fusion')).get('screening_applicable') is False or _mapping(package.get('fusion')).get('level') == 'not_applicable'
    doc.add_heading(presentation['heading'] if presentation['enabled'] else '回答' if task.get('search_only') else '研究结果' if research else _text(primary_task.get('heading')) if primary_task and not consumer.get('text') else "本次核查结论", level=1)
    _paragraph(doc, _summary(package))
    for run in doc.paragraphs[-1].runs:
        run.bold = True
    if presentation['enabled']: _presentation_sections(doc, presentation)
    for result in result_rows if not presentation['enabled'] else []:
        if result is primary_task and not consumer.get('text'): continue
        doc.add_heading(_text(result.get('heading') or result.get('label')), level=1)
        _paragraph(doc, result.get('conclusion'))
    timeline = _rows(task_results.get('timeline'), 100)
    if not presentation['enabled'] and timeline and any(row.get('timeline') is not None for row in _rows(task_results.get('results'),10)):
        doc.add_heading('已取得时相与覆盖缺口', level=1)
        records=[]
        for row in timeline:
            declared='、'.join(row.get('reported_actual_dates') or [])
            if declared: declared += ' 整幅日期已登记' if row.get('actual_capture_date') else ' 提供方显示日期 范围待核实'
            elif row.get('product_year'): declared=str(row['product_year'])+'版 非拍摄日期'
            else: declared=row.get('period_label') or '未知'
            records.append([row.get('figure'),row.get('actual_capture_date') or '未知',declared,
                {'positive':'保留疑点','negative':'本图未检出','unknown':'暂不可判'}.get(row.get('decision'),'暂不可判')])
        _table(doc,['图号','实际拍摄日期','提供方日期或版次','逐图状态'],records,[.55,1.2,3.8,1.35])
    doc.add_heading("环境风险场景观测" if risk_mode else "有效判读与同图复核", level=1)
    recognition = _mapping(package.get("recognition"))
    observations = _rows(recognition.get("observations"))
    candidates = _rows(recognition.get("candidates"))
    assessments = {row.get("asset_id"): row for row in _rows(package.get("image_assessments"))}
    figure_by_id = {asset.get("id"): f"图 {i}" for i, asset in enumerate(_rows(package.get("assets")), 1)}
    if not observations and not candidates:
        _paragraph(doc, "本次没有可引用的见微观测或候选记录。")
    for i, row in enumerate(observations, 1):
        assessment = assessments.get(row.get("asset_id"), {})
        review = _mapping(assessment.get("optical_false_positive_review"))
        statuses = {item.get("effective_status") for item in _rows(review.get("candidates"))}
        if review.get("status") == "completed" and statuses == {"excluded_obvious_false_positive"}:
            continue
        label = "待核实观测" if _effective_decision(assessment) == "unknown" else "观测"
        _paragraph(doc, f"{label} {i} {figure_by_id.get(row.get('asset_id'), '关联影像待核实')} " + _text(row.get("statement")))
    for row in assessments.values():
        review = _mapping(row.get("optical_false_positive_review"))
        if review.get("status") == "completed":
            doc.add_heading(figure_by_id.get(row.get("asset_id"), "关联影像待核实") + " 独立复核", level=2)
            _paragraph(doc, review.get("summary"))
            reviewed_candidates = _rows(review.get("candidates"))
            excluded = [item for item in reviewed_candidates if item.get("effective_status") == "excluded_obvious_false_positive"]
            if excluded:
                _paragraph(doc, f"已排除 {len(excluded)} 处明显误报。")
            for item in reviewed_candidates:
                label = {"excluded_obvious_false_positive": "排除明显误报", "supported_candidate": "保留疑点", "uncertain": "待核实候选"}.get(item.get("effective_status"), "待核实候选")
                _paragraph(doc, label + " " + _text(item.get("visible_evidence")))
                if item.get("competing_interpretation"):
                    _paragraph(doc, "对照解释 " + _text(item.get("competing_interpretation")))
            continue
        details = row.get("analysis_summary")
        if details and not any(obs.get("asset_id") == row.get("asset_id") for obs in observations):
            _paragraph(doc, figure_by_id.get(row.get("asset_id"), "关联影像待核实") + " 判读记录 " + _text(details))
    if any(_mapping(row.get("optical_false_positive_review")).get("status") == "completed" for row in assessments.values()):
        _paragraph(doc, "原始见微结果与原始框保存在完整证据包中；本报告采用同图复核后的有效记录，未将已排除误报作为固废结论。")
    for i, candidate in enumerate(candidates if not risk_mode else [], 1):
        candidate_assets = {observations[index].get("asset_id") for index in candidate.get("evidence_indices", [])
                            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(observations)}
        relevant_assessments = [assessments[identity] for identity in candidate_assets if identity in assessments]
        if not candidate_assets:
            relevant_assessments = list(assessments.values())
        if relevant_assessments and all(_effective_decision(row) == "negative" for row in relevant_assessments) and any(
                _mapping(row.get("optical_false_positive_review")).get("status") == "completed" for row in relevant_assessments):
            continue
        supported = bool(candidate_assets) and all(_effective_decision(assessments.get(identity, {})) == "positive" for identity in candidate_assets)
        label = "保留疑点" if supported else "待核实候选"
        _paragraph(doc, f"{label} {i} " + _text(candidate.get("label") or candidate.get("waste_type") or "待核查线索"))
        if candidate.get("alternative_explanations"):
            _paragraph(doc, "需排除的解释 " + _text(candidate["alternative_explanations"]))
        if candidate.get("verification"):
            _paragraph(doc, "核查建议 " + _text(candidate["verification"]))
    doc.add_heading("逐图证据记录", level=1)
    ledger = _rows(package.get("evidence_summary"))
    if not ledger:
        ledger = [{"source": _mapping(asset.get("metadata")).get("source"),
                   "acquisition_time": _mapping(asset.get("metadata")).get("acquisition_time"),
                   "spatial_status": _mapping(asset.get("metadata")).get("registration_verified"),
                   "figure": f"图 {i}"} for i, asset in enumerate(_rows(package.get("assets")), 1)]
    if ledger:
        records = []
        for i, row in enumerate(ledger, 1):
            spatial = {"verified": "空间对应已核验", "corrected": "相对位置已校正", "unknown": "位置待核实",
                       "misaligned": "存在位置偏差", "user_supplied": "用户提供 待核实"}.get(row.get("spatial_status"), "位置待核实")
            if row.get("same_point_eligible") is False:
                spatial += " 仅供背景参考"
            records.append([row.get("figure") or f"图 {i}", SOURCE_NAMES.get(row.get("source"), row.get("source") or "来源未注明"),
                            row.get("acquisition_time") or "未知", spatial])
        _table(doc, ["图号", "图源", "实际拍摄时间", "空间核验"], records, [.55, 1.6, 1.7, 3.05])
    else:
        _paragraph(doc, "本次没有取得可用于逐图核查的影像。")
    _paragraph(doc, "拍摄时间未知的底图仍按未知处理。菜单中的 2026 或最新标签只表示所选图源，不能据此确认拍摄于 2026 年。")
    if include_images:
        _figures(doc, package, root, warnings)
    science = _rows(package.get("scientific_evidence"))
    if science:
        doc.add_heading("科学辅助证据", level=1)
    for item in science:
        source = SOURCE_NAMES.get(item.get("source"), _text(item.get("source")) or "科学数据")
        doc.add_heading(source, level=2)
        analysis = _mapping(item.get("analysis"))
        if analysis.get("status") == "completed":
            _paragraph(doc, analysis.get("summary") or "分析已完成，未记录可引用摘要。")
            _paragraph(doc, analysis.get("uncertainties"))
        else:
            _paragraph(doc, "本次尚无已完成的像素分析结论；目录检索或窗口下载记录不能作为固废判读。")
        _paragraph(doc, "科学辅助信息不替代见微对可见对象和候选类型的判读，也不能单独确认固废类型。")
    all_sources = _rows(_mapping(package.get("policy")).get("sources"), 100)
    cited_ids = set(_mapping(package.get("narrative")).get("cited_policy_ids") or [])
    relevant_sources = [source for source in all_sources if source.get("id") in cited_ids] if cited_ids else all_sources
    sources = relevant_sources[:12]
    if sources:
        doc.add_heading("联网来源与监管背景", level=1)
    for index, source in enumerate(sources, 1):
        url = _text(source.get("url"), 2048)
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            continue
        _paragraph(doc, f"{index} " + _text(source.get("title") or "公开资料"))
        _paragraph(doc, url)
        stamp = _text(source.get("published_at"))
        if stamp:
            _paragraph(doc, "发布日期 " + stamp)
    if sources:
        _paragraph(doc, "上述资料须核对现行性、地域和时间适用范围；来源中的陈述不等于本点位的现场事实。")
    if len(all_sources) > len(sources):
        _paragraph(doc, "其余检索来源与执行记录保存在本任务完整证据包中。")
    if not risk_mode: doc.add_heading("下一步核查", level=1)
    suggestions = normalize_recommendations(_mapping(package.get("narrative")).get("recommendations"), limit=12)
    for item in suggestions if not risk_mode else []:
        _paragraph(doc, item)
    unknowns = recognition.get("unknowns") or []
    quality = recognition.get("quality_issues") or []
    unknowns = unknowns if isinstance(unknowns, (list, tuple)) else [unknowns]
    quality = quality if isinstance(quality, (list, tuple)) else [quality]
    for item in list(dict.fromkeys(_text(value) for value in [*unknowns, *quality] if value))[:12]:
        _paragraph(doc, "待核查 " + item)
    _paragraph(doc, BOUNDARY)


def _save(doc: Any, root: Path, filename: str, warnings: list[str]) -> DocxExportResult:
    # Explicitly set every run as well as styles to avoid theme-font substitution.
    for element in doc.element.body.iter():
        if element.tag.endswith("}r"):
            _font(element.get_or_add_rPr())
    output = _output(root, filename)
    data = BytesIO()
    doc.save(data)
    content = data.getvalue()
    # Output is a server-owned filename, never a path from model/user content.
    # Replace atomically so concurrent downloads never see a partial ZIP, and
    # an existing hard link cannot redirect writes into another artifact.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".word-export-", suffix=".docx", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return DocxExportResult(output, hashlib.sha256(content).hexdigest(), len(content), tuple(dict.fromkeys(warnings)))


def export_report_docx(package: Mapping[str, Any], run_dir: Path, *, filename: str = "report.docx",
                       title: str | None = None, include_images: bool = True) -> DocxExportResult:
    """Export one already-authorized finding package using only this run's files.

    Call after ``report.render_report`` so ``source-<asset_id>.*`` and
    ``annotated-<asset_id>.png`` exist. No Store or global asset path is accepted.
    The package is data, never executable instructions. Main-model free-form
    conclusions are deliberately not promoted over primary model observations.
    """
    if not isinstance(package, Mapping):
        raise ValueError("报告需要结构化证据包")
    root = _folder(run_dir)
    _output(root, filename)
    from .task_results import task_report_title
    task, fusion = _mapping(package.get('task')), _mapping(package.get('fusion'))
    default_title = ('无隅见微 联网资料问答' if task.get('search_only') else '无隅见微 卫星影像研究报告'
                     if package.get('research_result') or fusion.get('screening_applicable') is False or fusion.get('level') == 'not_applicable'
                     else task_report_title(task.get('task_types', [])))
    presentation = presentation_contract(package)
    if presentation['enabled']: default_title = presentation['report_title']
    doc = _document(_text(title or default_title, 100))
    warnings: list[str] = []
    _section(doc, package, root, warnings, include_images=include_images)
    return _save(doc, root, filename, warnings)


def export_batch_docx(points: Sequence[Mapping[str, Any]], run_dir: Path, *, filename: str = "batch-report.docx",
                      title: str | None = None, include_images: bool = True,
                      workbook_name: str | None = None) -> DocxExportResult:
    """Export up to 100 points, each with ``site_id``, ``package``, ``artifact_dir``.

    ``artifact_dir`` is relative to this batch's authorized directory and must
    already exist. Stage child report artifacts there, e.g. ``points/site-01``;
    paths outside the batch directory and absolute paths are rejected.
    """
    if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError("单份多点报告需包含 1 至 100 个点位")
    root = _folder(run_dir)
    _output(root, filename)
    checked = []
    for point in points:
        if not isinstance(point, Mapping) or not isinstance(point.get("package"), Mapping):
            raise ValueError("点位需要结构化 package")
        checked.append((point, _within(root, point.get("artifact_dir", ""), directory=True)))
    from .task_results import task_report_title
    task_types=list(dict.fromkeys(kind for point,_ in checked for kind in _mapping(point['package'].get('task')).get('task_types', [])))
    doc = _document(_text(title or task_report_title(task_types,batch=True), 100))
    _paragraph(doc, f"本次汇总 {len(points)} 个点位，逐点保留见微观测、科学辅助信息和未解决的证据缺口。")
    if workbook_name:
        _paragraph(doc, "点位资料 " + _text(workbook_name, 180) + "。用户资料中的类别与位置声明需单独核验。")
    doc.add_heading("点位结论总览", level=1)
    _table(doc, ["点位", "位置或范围", "主要线索"],
           [[point.get("site_id") or str(i), _location(point["package"]), _summary(point["package"])]
            for i, (point, _) in enumerate(checked, 1)], [1.0, 1.8, 4.1])
    warnings: list[str] = []
    for index, (point, artifact_dir) in enumerate(checked, 1):
        if index > 1:
            doc.add_page_break()
        doc.add_heading(f"点位 {index} " + _text(point.get("site_id"), 100), level=1)
        _section(doc, point["package"], artifact_dir, warnings, include_images=include_images)
    return _save(doc, root, filename, warnings)
