"""Build local Word export QA examples from existing authorized evidence runs.

No model call or network request is made. Run with the bundled artifact Python.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuyu_harness.docx_export import export_batch_docx, export_report_docx


def main() -> None:
    source_root = ROOT / "outputs/portal/engine/runs"
    output = ROOT / "outputs/docx-export-20261024"
    selected = ["e073f28899524b3e9ad14ca9b4d08c12", "3b31957d93d843c19f177c0c3d484859"]
    points = []
    for index, identity in enumerate(selected, 1):
        source = source_root / identity
        package = json.loads((source / "finding-package.json").read_text(encoding="utf-8"))
        relative = f"points/site-{index:02d}"
        target = output / relative
        target.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            if re.fullmatch(r"(?:source|annotated)-[a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|webp)", entry.name) and entry.is_file():
                shutil.copyfile(entry, target / entry.name)
        points.append({"site_id": f"S{index:02d}", "package": package, "artifact_dir": relative})
    single = export_report_docx(points[0]["package"], output / points[0]["artifact_dir"],
                                title="无隅遥感单点疑点核查报告")
    batch = export_batch_docx(points, output, workbook_name="逐点报告样式核验")
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    reports = []
    for result in (single, batch):
        with ZipFile(result.path) as archive:
            body = ET.fromstring(archive.read("word/document.xml"))
            runs = list(body.iter(namespace + "r"))
            fonts = [run.find(namespace + "rPr/" + namespace + "rFonts") for run in runs]
            font_pass = all(font is not None and font.get(namespace + "ascii") == "Times New Roman"
                            and font.get(namespace + "hAnsi") == "Times New Roman"
                            and font.get(namespace + "eastAsia") == "宋体" for font in fonts)
            reports.append({**result.artifact(), "relative_path": str(result.path.relative_to(ROOT)),
                            "run_font_checks": len(runs), "fonts_passed": font_pass,
                            "embedded_images": len([name for name in archive.namelist() if name.startswith("word/media/")])})
    report = {"reports": reports, "font_contract": {"chinese": "宋体", "latin_and_digits": "Times New Roman"},
              "render_status": "pending_packaged_renderer", "source_run_count": len(selected)}
    (output / "qa.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
