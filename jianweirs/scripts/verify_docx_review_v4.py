"""Render fixture preparation using saved, real same-image review results."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuyu_harness.docx_export import export_report_docx


def main():
    source = ROOT / 'outputs/portal/engine/runs/a77b059569ba4498b9125c3270baf90c'
    live = ROOT / 'outputs/optical-review-v4-live-20261024'
    output = ROOT / 'outputs/docx-review-v4-20261024'
    output.mkdir(parents=True, exist_ok=True)
    original = json.loads((source / 'finding-package.json').read_text(encoding='utf-8'))
    cases = json.loads((live / 'acceptance.json').read_text(encoding='utf-8'))['cases']
    identities = {case['asset_id'] for case in cases}
    package = deepcopy(original)
    package['task']['objective'] = '复核既有高分影像中的建筑误报，分别记录同图判读和地理对应的适用范围。'
    package['assets'] = [asset for asset in original['assets'] if asset['id'] in identities]
    package['recognition'] = {'observations': [], 'candidates': [], 'unknowns': [], 'quality_issues': []}
    package['image_assessments'] = []
    package['native_rs_results'] = [row for row in original['native_rs_results'] if row['asset_id'] in identities]
    for case in cases:
        identity = case['asset_id']
        package['recognition']['observations'].extend(case['effective_recognition']['observations'])
        assessment = deepcopy(next(row for row in original['image_assessments'] if row['asset_id'] == identity))
        assessment['optical_false_positive_review'] = case['review']
        assessment['candidate_count'] = case['effective_candidate_count']
        assessment['decision'] = case['review']['effective_decision'] if assessment.get('same_point_eligible') else 'unknown'
        package['image_assessments'].append(assessment)
        shutil.copyfile(source / f'source-{identity}.png', output / f'source-{identity}.png')
        shutil.copyfile(live / case['effective_annotations'], output / f'annotated-{identity}.png')
    package['scientific_evidence'] = []
    package['policy'] = {'sources': []}
    package['narrative'] = {'recommendations': ['如需核查当前地面状态，请补充具有明确拍摄日期的近期影像或现场照片。']}
    package['fusion'] = {'judgment': '同图建筑误报已排除；任务点位对应另行核实', 'basis': '同图复核与地理对应分开记录'}
    package['evidence_summary'] = [
        {'figure': f'图 {index}', 'source': asset['metadata']['source'],
         'acquisition_time': asset['metadata'].get('acquisition_time'),
         'spatial_status': assessment.get('spatial_status'), 'same_point_eligible': assessment.get('same_point_eligible')}
        for index, (asset, assessment) in enumerate(zip(package['assets'], package['image_assessments']), 1)]
    result = export_report_docx(package, output, title='无隅见微遥感误报复核报告')
    namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    with ZipFile(result.path) as archive:
        body = ET.fromstring(archive.read('word/document.xml'))
        text = '\n'.join(row.text or '' for row in body.iter(namespace + 't'))
        fonts = [run.find(namespace + 'rPr/' + namespace + 'rFonts') for run in body.iter(namespace + 'r')]
        font_pass = all(font is not None and font.get(namespace + 'ascii') == 'Times New Roman'
                        and font.get(namespace + 'eastAsia') == '宋体' for font in fonts)
        image_count = len([name for name in archive.namelist() if name.startswith('word/media/')])
    report = {'artifact': result.artifact(), 'raw_source_run_preserved': True,
              'same_image_review_cases': len(cases), 'font_contract_passed': font_pass,
              'qualified_negative_scope_preserved': '不能形成该点位结论' in text,
              'effective_annotation_label_present': '复核后有效标注' in text,
              'raw_audit_retention_explained': '原始见微结果与原始框保存在完整证据包中' in text,
              'embedded_image_count': image_count, 'visual_qa': 'pending_render'}
    (output / 'qa.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
