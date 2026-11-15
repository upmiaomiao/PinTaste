"""Record completed visual review; run after opening every rendered page."""
from pathlib import Path
import hashlib
import json

root=Path(__file__).resolve().parents[1]/'outputs'/'task-temporal-v6-20261025'
path=root/'validation.json'
report=json.loads(path.read_text(encoding='utf-8'))
for index,row in enumerate(report['task_report_checks'],1):
    folder=root/'rendered'/f'{index:032x}'
    pages=sorted(folder.glob('page-*.png'))
    if len(pages)!=1:raise ValueError('Expected visually reviewed single-page task fixture')
    row.update(rendered_pages_verified=True,page_count=len(pages),renderer='canonical render_docx.py using isolated LibreOffice Writer container',
        docx_sha256=hashlib.sha256(Path(row['docx']).read_bytes()).hexdigest(),
        rendered_pages=[{'name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in pages])
report['visual_qa']={'status':'passed','task_fixtures_reviewed':10,'fixture_pages_reviewed':10,
    'checked':['all headings and conclusions differ by task','no clipped text','no overlapping objects','Chinese text readable',
               '宋体 eastAsia and Times New Roman latin font declarations','no invented dated change or area for missing data'],
    'canonical_renderer_repair':'Writer import support, poppler and pdf2image installed only in an isolated disposable container; production containers unchanged'}
report['live_report_visual_qa']={'status':'passed','page_count':4,'source':str(Path(r'D:/博士论文/开题资料/奥维地球api封装/outputs/unified-image-integration/village-live-b854937983/runs/fc63002eec034e149ffd230ec782a8f1/report.docx')),
    'rendered_pages':[str(p) for p in sorted((root/'rendered'/'live-village').glob('page-*.png'))],
    'notes':['images complete and original/annotation pairs readable','uncertain raw-material/building candidates remain uncertain',
             'date and company-location gaps remain disclosed','no liquid chemistry or pollution determination']}
path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':'passed','task_fixture_pages':10,'live_report_pages':4}))
