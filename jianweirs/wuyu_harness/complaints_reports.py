"""Task-specific coverage report, distinct from an individual point finding."""

from __future__ import annotations

from html import escape
from pathlib import Path
import re


_METRICS = (("planned_coverage", "计划范围覆盖"), ("downloaded_requested_coverage", "已取图的请求覆盖"),
            ("visual_reviewed_coverage", "已完成有效影像审阅"), ("time_qualified_coverage", "实际日期适用的覆盖"))

_STATUS_LABELS = {
    "queued": "等待处理", "pending": "等待处理", "running": "正在处理",
    "succeeded": "已完成", "completed": "已完成", "failed": "处理未完成",
    "cancelled": "已取消", "canceled": "已取消", "interrupted": "已中断",
    "planning": "正在安排核查", "ready": "已准备就绪", "paused": "已暂停",
    "waiting_for_input": "等待补充信息", "needs_input": "等待补充信息",
    "needs_location": "等待明确位置", "needs_region_confirmation": "等待确认核查范围",
    "over_budget": "超过本次核查额度，待调整范围", "unverified_ledger": "证据账本待核实",
    "findings_pending_review": "发现疑似线索，待复核", "needs_spatial_review": "疑似线索位置待复核",
    "inconclusive_review": "判读尚不确定，待补充证据", "no_reviewable_finding_in_scope": "本次有效范围内未见可复核疑点",
    "visual_review_complete_time_unqualified": "影像已审阅，成像日期尚不适用", "incomplete_coverage": "部分范围尚未完成核查",
}
_GAP_LABELS = {
    "task_not_completed": "任务尚未完成",
    "missing_evidence_reference": "原图或任务证据记录不完整",
    "image_not_received": "尚未取得影像",
    "footprint_not_verified": "影像覆盖位置尚未核实",
    "quality_not_passed": "影像质量尚未达到判读要求",
    "model_not_completed": "影像判读尚未完成",
    "footprint_outside_window": "有效影像未覆盖计划窗口",
    "actual_date_unknown_outside_period_or_partial_scope": "实际拍摄日期未核实、不在核查期间内或仅适用于部分影像",
}


def _status_label(value):
    return _STATUS_LABELS.get(value, "状态待核实，请查看逐窗任务记录")


def _gap_text(row):
    # Translate for readers only; keep the audit ledger's original codes intact.
    codes = list(row.get("exclusions") or [])
    if row.get("date_issue"):
        codes.append(row["date_issue"])
    labels = [_GAP_LABELS.get(code, "存在待核实事项，请查看逐窗任务记录") for code in codes]
    return "；".join(dict.fromkeys(labels)) or "无已记录缺口"


def _percent(body):
    value = (body or {}).get("fraction")
    return f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else "未知"


def _task_rows(tasks):
    result = []
    for item in tasks[:256]:
        task = item.get("task") or {}
        spec = task.get("complaint_scan") or {}
        run_id = item.get("id") or item.get("run_id") or "未知任务"
        result.append((spec.get("window_id") or item.get("window_id") or "窗口未标明", str(run_id),
                       _status_label(item.get("status")), item))
    return result


def _safe_link(value):
    # Reports receive server routes, not arbitrary attachment HTML/JS URLs.
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//") and not re.search(r"[\x00-\x20]", value):
        return value
    return None


def render_scan_report_html(summary: dict, tasks: list[dict]) -> str:
    title = "举报线索区域核查报告"
    metrics = "".join(f"<tr><th>{label}</th><td>{_percent(summary.get(key))}</td></tr>" for key, label in _METRICS)
    gaps = summary.get("missing_visual_window_ids") or []
    dates = summary.get("time_unqualified_window_ids") or []
    limitations = "".join(f"<li>{escape(str(value))}</li>" for value in summary.get("limitations", []))
    conditional = "".join(f"<li>窗口 {escape(str(row.get('window_id','')))} 来源 {escape(str(row.get('source','')))}："
                          f"{escape(str(row.get('limitation') or '对象尚未核实是否位于确认区域内'))}</li>"
                          for row in summary.get("conditional_findings", []))
    rows = []
    for window_id, run_id, status, item in _task_rows(tasks):
        links = []
        raw_artifacts = item.get("artifacts") or {}
        if isinstance(raw_artifacts, dict):
            values = raw_artifacts.items()
        elif isinstance(raw_artifacts, list):
            values = ((entry.get("name", "证据"), entry.get("url")) if isinstance(entry, dict) else (str(entry), None) for entry in raw_artifacts)
        else:
            values = []
        for name, url in values:
            if isinstance(url, dict):
                url = url.get("url") or url.get("href")
            if safe := _safe_link(url):
                links.append(f'<a href="{escape(safe, quote=True)}">{escape(str(name))}</a>')
        rows.append(f"<tr><td>{escape(window_id)}</td><td>{escape(run_id)}</td><td>{escape(status)}</td><td>{' · '.join(links) or '见逐窗任务记录'}</td></tr>")
    ledger = "".join(f"<tr><td>{escape(str(row.get('window_id','')))}</td><td>{escape(str(row.get('source','')))}</td>"
                     f"<td>{'已审阅' if row.get('visual_reviewed') else '尚未满足'}</td>"
                     f"<td>{escape('、'.join(row.get('actual_dates') or []) or '日期未核实')}</td>"
                     f"<td>{escape(_gap_text(row))}</td></tr>"
                     for row in summary.get("ledger", []))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:Arial,"Microsoft YaHei",sans-serif;color:#202326;max-width:1080px;margin:40px auto;line-height:1.7;padding:0 24px}}h1{{font-size:26px}}h2{{font-size:19px;margin-top:28px}}table{{border-collapse:collapse;width:100%;font-size:14px;table-layout:fixed}}td,th{{border:1px solid #ddd;padding:10px;text-align:left;overflow-wrap:anywhere}}th{{background:#f3f4f5}}p,li{{overflow-wrap:anywhere}}a{{color:#256b56}}@media print{{body{{margin:0;max-width:none}}}}</style>
<h1>{title}</h1><p><strong>{escape(summary.get('conclusion') or '尚未形成核查结论')}</strong></p>
<p>本报告区分举报原文、实际取图、有效影像审阅与期间适用性。它仅描述本次确认范围和有效影像可见地表，不作举报真实性或违法认定。</p>
<h2>核查范围和覆盖</h2><p>已确认WGS84范围：{escape(str(summary.get('region_bbox') or '见原扫描计划'))}。核查期间：{escape(str(summary.get('period_start') or '未限定'))} 至 {escape(str(summary.get('period_end') or '未限定'))}。</p><table>{metrics}</table><p>计划窗口 {summary.get('planned_windows',0)} 个，完成有效审阅 {summary.get('visual_reviewed_windows',0)} 个。面积按几何并集近似计算，重叠不重复计入。</p>
<h2>盲区与时间缺口</h2><p>未完成有效审阅的窗口：{escape('、'.join(gaps) or '无已记录缺口')}。</p><p>日期适用性未满足的窗口：{escape('、'.join(dates) or '无已记录缺口')}。</p>
{'<h2>需确认位置的窗口线索</h2><ul>' + conditional + '</ul>' if conditional else ''}
<h2>逐窗证据</h2><table><thead><tr><th>窗口</th><th>任务编号</th><th>状态</th><th>证据入口</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>来源与质量账本</h2><table><thead><tr><th>窗口</th><th>来源</th><th>有效审阅</th><th>实际日期</th><th>缺口</th></tr></thead><tbody>{ledger}</tbody></table>
<h2>核查边界和下一步</h2><ul>{limitations}</ul><p>对疑点保留原图、标注和逐窗报告；对未覆盖或不可观测事项补充定位、适用时相、现场照片或台账。未经有效配准和可比性核验，不将不同渠道影像解释为新增、清理或再次倾倒。</p></html>'''


def export_scan_report_docx(summary: dict, tasks: list[dict], path) -> Path:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.8)
    section.left_margin = section.right_margin = Inches(.85)
    for name in ("Normal", "Title", "Heading 1", "Heading 2"):
        style = document.styles[name]
        style.font.name = "Times New Roman"
        style.font.color.rgb = RGBColor(0, 0, 0)
        fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
        fonts.set(qn("w:eastAsia"), "宋体")
        fonts.set(qn("w:ascii"), "Times New Roman")
        fonts.set(qn("w:hAnsi"), "Times New Roman")
        if name == "Title":
            # The bundled Word template carries an accent-blue bottom border.
            # Font colour alone does not remove it from Word/LibreOffice.
            properties = style.element.find(qn("w:pPr"))
            if properties is not None:
                for border in list(properties.findall(qn("w:pBdr"))):
                    properties.remove(border)
    normal = document.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.35
    normal.paragraph_format.space_after = Pt(6)
    document.styles["Title"].font.size = Pt(21)
    document.styles["Heading 1"].font.size = Pt(15)
    document.add_paragraph("举报线索区域核查报告", style="Title")
    document.add_paragraph(summary.get("conclusion") or "尚未形成核查结论")
    document.add_paragraph("本报告面向举报线索的区域核查，区分实际取图、有效影像审阅和成像日期适用性。结论仅约束本次确认范围及其有效影像可见地表，不作举报真实性或违法认定。")
    document.add_heading("核查范围和覆盖", level=1)
    document.add_paragraph(f"已确认WGS84范围 {summary.get('region_bbox') or '见原扫描计划'}。核查期间 {summary.get('period_start') or '未限定'} 至 {summary.get('period_end') or '未限定'}。")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "覆盖口径", "范围占比"
    for key, label in _METRICS:
        a, b = table.add_row().cells
        a.text, b.text = label, _percent(summary.get(key))
    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            tcpr = cell._tc.get_or_add_tcPr()
            borders = OxmlElement("w:tcBorders")
            for edge in ("top", "left", "bottom", "right"):
                node = OxmlElement("w:" + edge)
                node.set(qn("w:val"), "single")
                node.set(qn("w:sz"), "4")
                node.set(qn("w:color"), "D9D9D9")
                borders.append(node)
            tcpr.append(borders)
            if row_index == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "E8ECEF")
                tcpr.append(shade)
    document.add_paragraph(f"计划窗口 {summary.get('planned_windows',0)} 个，完成有效审阅 {summary.get('visual_reviewed_windows',0)} 个。覆盖按几何并集近似计算，重叠不重复计入，不作为测绘面积。")
    document.add_heading("盲区与时间缺口", level=1)
    document.add_paragraph("未完成有效审阅的窗口：" + ("、".join(summary.get("missing_visual_window_ids") or []) or "无已记录缺口"))
    document.add_paragraph("日期适用性未满足的窗口：" + ("、".join(summary.get("time_unqualified_window_ids") or []) or "无已记录缺口"))
    if summary.get("conditional_findings"):
        document.add_heading("需确认位置的窗口线索", level=1)
        for row in summary["conditional_findings"]:
            document.add_paragraph(f"窗口 {row.get('window_id','未知')} 来源 {row.get('source','未知')}：{row.get('limitation') or '对象尚未核实是否位于确认区域内'}")
    document.add_heading("逐窗证据", level=1)
    for window_id, run_id, state, item in _task_rows(tasks):
        window_heading = document.add_paragraph(f"窗口 {window_id}  任务 {run_id}  状态 {state}")
        window_heading.paragraph_format.keep_with_next = True
        for row in summary.get("ledger", []):
            if row.get("run_id") == run_id:
                actual = "、".join(row.get("actual_dates") or []) or "日期未核实"
                exclusions = _gap_text(row)
                document.add_paragraph(f"来源 {row.get('source','未知')}；{'已完成有效审阅' if row.get('visual_reviewed') else '未满足有效审阅'}；实际日期 {actual}；缺口 {exclusions}。")
    document.add_heading("核查边界和下一步", level=1)
    for limitation in summary.get("limitations", []):
        document.add_paragraph(str(limitation))
    document.add_paragraph("疑点应结合逐窗原图、标注和报告复核。对未覆盖、日期不适用或影像不可观测事项，应补充定位、适用时相、现场照片或台账。")
    document.save(destination)
    return destination
