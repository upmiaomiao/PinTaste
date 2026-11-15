"""Generate non-live task/report acceptance fixtures and workbook proposals."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wuyu_harness.schemas import TaskKind, TaskRequest
from wuyu_harness.store import Store
from wuyu_harness.task_results import build_task_results, REPORT_HEADINGS
from wuyu_harness.report import render_report
from wuyu_harness.docx_export import export_report_docx
from wuyu_harness.workbook import parse_workbook
from wuyu_harness.workbook_tasks import workbook_task_proposals


def main():
    folder=ROOT/'outputs'/'task-temporal-v6-20261025'
    folder.mkdir(parents=True,exist_ok=True)
    store=Store(folder/'fixtures')
    verified=[]
    for index,kind in enumerate(TaskKind,1):
        identity=f'{index:032x}'
        task=TaskRequest(objective='验收测试 '+str(kind),task_types=[kind],center=[104.06,30.67],search_policy=False)
        result=build_task_results(task,[],{'observations':[],'candidates':[]},[],{'judgment':'现有影像证据不足'})
        package={'run_id':identity,'status':'needs_more_data','task':task.model_dump(mode='json'),'assets':[],
            'recognition':{'observations':[],'candidates':[],'unknowns':['待提供可分析的目标影像']},
            'fusion':{'judgment':'现有影像证据不足','basis':'本例用于检验报告在缺资料时能否保留相应任务的证据要求','level':'insufficient'},
            'task_results':result,'policy':{'status':'disabled_by_user','sources':[]},
            'narrative':{'summary':result['results'][0]['conclusion'],'recommendations':result['results'][0]['next_steps']}}
        render_report(package,store,identity)
        report=export_report_docx(package,store.run_dir(identity),include_images=False)
        html=(store.run_dir(identity)/'report.html').read_text(encoding='utf-8')
        with ZipFile(report.path) as archive:
            text=archive.read('word/document.xml').decode()
            styles=archive.read('word/styles.xml').decode()
        assert REPORT_HEADINGS[kind] in text and REPORT_HEADINGS[kind] in html
        assert '宋体' in styles and 'Times New Roman' in styles
        assert '已确认违法' not in text
        verified.append({'task':kind.value,'html':str(store.run_dir(identity)/'report.html'),
            'docx':str(report.path),'word_xml_verified':True,'html_verified':True,'rendered_pages_verified':False})
    source=Path(r'D:/博士论文/成都数据/生态委/1-6原表/第五批-成都市全域疑似固废清单（新增&面积增加）.xlsx')
    book=parse_workbook(source.read_bytes(),source.name)
    proposals=workbook_task_proposals(book)
    (folder/'chengdu-task-proposals.json').write_text(json.dumps(proposals,ensure_ascii=False,indent=2),encoding='utf-8')
    report={'schema_version':'temporal-task-validation-v1','test_fixtures_not_live_inference':True,
        'live_inference_requests':0,'task_report_checks':verified,'workbook':{
            'record_count':book['profile']['record_count'],'embedded_image_count':book['profile']['embedded_image_count'],
            'sha256':book['profile']['sha256'],'point_validation':proposals['point_validation'],
            'recorded_change_types':proposals['profile']['recorded_change_types'],
            'period_labels':proposals['profile']['period_labels']}}
    (folder/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','task_reports':len(verified),'workbook':report['workbook']},ensure_ascii=False))


if __name__=='__main__':main()
