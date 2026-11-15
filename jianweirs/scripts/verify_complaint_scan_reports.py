"""Representative coverage-report QA fixture; no live data/model calls."""
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wuyu_harness.complaints import plan_scan, scan_status
from wuyu_harness.complaints_reports import export_scan_report_docx, render_scan_report_html

folder=ROOT/'outputs'/'complaint-scan'
folder.mkdir(parents=True,exist_ok=True)
plan=plan_scan({'text':'验收测试资料：四窗口区域，含一处待核查候选、失败窗口、日期未知和未完成判读窗口。不是实际举报核查。',
    'region_bbox':[104.06,30.67,104.067,30.677],'region_confirmed':True,'window_pixels':1024,'max_windows':4})
assert plan['status']=='ready' and len(plan['windows'])==4,plan
ledger=[];tasks=[]
for index,frame in enumerate(plan['windows'],1):
    run_id=f'{index:032x}'
    row={'window_id':frame['window_id'],'source':'gaofen2026' if index%2 else 'century','run_id':run_id,
        'asset_id':'fixture-asset-'+str(index),'status':'succeeded','downloaded':True,
        'requested_bbox':frame['bbox'],'footprint_bbox':frame['bbox'],'spatial_verified':True,'quality_passed':True,
        'analyzed':True,'actual_dates':['2026-02-25'],'actual_date_scope':'full_footprint','negative_review_complete':False}
    if index==1:row.update(reviewable_finding=True)
    elif index==2:row.update(status='failed',downloaded=False,quality_passed=False,analyzed=False,footprint_bbox=None,spatial_verified=False,actual_dates=[])
    elif index==3:row.update(actual_dates=[],actual_date_scope='unknown',negative_review_complete=True)
    else:row.update(analyzed=False)
    ledger.append(row)
    tasks.append({'id':run_id,'status':row['status'],'task':{'complaint_scan':{'window_id':frame['window_id']}},
        'artifacts':{'报告':'/portal/api/runs/'+run_id+'/artifacts/report.html'}})
summary=scan_status(plan,ledger,'2026-02-01','2026-03-01',trusted=True)
assert summary['status']=='findings_pending_review' and len(summary['findings'])==1
assert summary['visual_reviewed_windows']==2 and len(summary['missing_visual_window_ids'])==2
assert summary['time_qualified_windows']==1
assert 0<summary['time_qualified_coverage']['fraction']<summary['visual_reviewed_coverage']['fraction']<summary['planned_coverage']['fraction']==1
summary['conclusion']='验收测试示例，不代表实际举报核查。'+summary['conclusion']
summary['limitations'].insert(0,'本文件为格式和证据口径验收测试，不代表已开展实际区域取图或模型核查。')
docx=export_scan_report_docx(summary,tasks,folder/'report-fixture.docx')
html=render_scan_report_html(summary,tasks)
(folder/'report-fixture.html').write_text(html,encoding='utf-8')
(folder/'report-fixture-input.json').write_text(json.dumps({'plan':plan,'raw_ledger':ledger,'summary':summary,'tasks':tasks},ensure_ascii=False,indent=2),encoding='utf-8')
with ZipFile(docx) as archive:
    xml=archive.read('word/document.xml').decode()
    styles=archive.read('word/styles.xml').decode()
    assert '宋体' in styles and 'Times New Roman' in styles
    assert all(frame['window_id'] in xml and frame['window_id'] in html for frame in plan['windows'])
result={'fixture_not_live':True,'live_model_calls':0,'status':'structural_passed_pending_visual',
    'docx_sha256':hashlib.sha256(docx.read_bytes()).hexdigest(),
    'windows':4,'visual_reviewed_windows':summary['visual_reviewed_windows'],'time_qualified_windows':summary['time_qualified_windows'],
    'coverage':{key:summary[key]['fraction'] for key in ('planned_coverage','downloaded_requested_coverage','visual_reviewed_coverage','time_qualified_coverage')},
    'summary_status':summary['status'],'word_fonts_verified':True,'html_escaped':True,'report_page_visual_qa':False}
(folder/'report-fixture-qa.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
