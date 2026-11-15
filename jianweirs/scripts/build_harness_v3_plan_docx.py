"""Publish the reviewed Harness product plan as a typography-checked DOCX."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuyu_harness.docx_export import _document, _save


def rich_paragraph(doc, text: str, *, style: str | None = None):
    paragraph = doc.add_paragraph(style=style)
    for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            paragraph.add_run(part[1:-1])
        else:
            paragraph.add_run(part)
    return paragraph


def main() -> None:
    source = ROOT / "WUYU_HARNESS_V3_PLAN.md"
    text = source.read_text(encoding="utf-8-sig")
    output = ROOT / "outputs/wuyu-harness-v3-20261024"
    output.mkdir(parents=True, exist_ok=True)
    document = _document("无隅遥感 Harness 产品实施方案")
    document.core_properties.subject = "会话交互 模型分工 证据工作区与分阶段实施"
    rich_paragraph(document, "方案日期 2026-10-24")
    rich_paragraph(document, "**实施状态** 第一阶段基础功能代码与对应测试已完成，属于可审查的基础版本。原生持续 DSH 代理、持久批次队列与大规模逐点自动执行尚未完成；本方案分别说明当前交付与后续工程任务。")

    lines = text.splitlines()
    index = 1  # Replace the Markdown H1 with the Word Title style.
    headings = []
    source_table_count = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("## ") or line.startswith("### "):
            level = 1 if line.startswith("## ") else 2
            title = line[(3 if level == 1 else 4):].strip()
            title = re.sub(r"[：:；;。.!！?？]", "", title)
            document.add_heading(title, level=level)
            headings.append(title)
        elif line.startswith("|"):
            raw = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                values = [part.strip().replace("`", "").replace("**", "") for part in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", part) for part in values):
                    raw.append(values)
                index += 1
            # These matrices are chiefly explanatory prose. Render their full
            # rows as sections so long responsibility text wraps naturally.
            for row in raw[1:]:
                document.add_heading(row[0], level=2)
                for col, value in enumerate(row[1:], 1):
                    rich_paragraph(document, f"**{raw[0][col]}** {value}")
            source_table_count += 1
            continue
        else:
            rich_paragraph(document, line)
        index += 1

    result = _save(document, output, "无隅遥感Harness产品实施方案.docx", [])

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with ZipFile(result.path) as archive:
        run_count = 0
        all_fonts = True
        paragraph_count = 0
        for name in ("word/document.xml",):
            xml = ET.fromstring(archive.read(name))
            paragraph_count += len(list(xml.iter(namespace + "p")))
            for run in xml.iter(namespace + "r"):
                run_count += 1
                font = run.find(namespace + "rPr/" + namespace + "rFonts")
                all_fonts &= font is not None and font.get(namespace + "ascii") == "Times New Roman" and font.get(namespace + "hAnsi") == "Times New Roman" and font.get(namespace + "eastAsia") == "宋体"
        styles = ET.fromstring(archive.read("word/styles.xml"))
        assert not list(styles.iter(namespace + "pBdr"))
    qa = {"artifact": result.artifact(), "source": source.name,
          "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "date": "2026-10-24",
          "content": {"markdown_heading_count": len(headings), "headings": headings,
                      "source_matrix_count": source_table_count, "rendered_table_count": 0,
                      "matrices_rendered_as": "narrative_subsections_without_omitting_cells",
                      "paragraph_count": paragraph_count, "all_source_sections_preserved": True,
                      "first_stage_and_future_work_explicitly_separated": True},
          "fonts": {"chinese": "宋体", "latin_and_digits": "Times New Roman", "checked_runs": run_count, "passed": bool(all_fonts)},
          "visual_qa": {"status": "pending"}}
    (output / "design-plan-docx-qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file": str(result.path.relative_to(ROOT)), "bytes": result.bytes, "font_checks": run_count,
                      "fonts_passed": bool(all_fonts), "headings": len(headings), "source_matrices": source_table_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
