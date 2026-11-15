"""Force same-location live PNG checks after the upstream SDK repair."""
from __future__ import annotations
import asyncio
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from wuyu_harness.unified_imagery import UnifiedImageryClient
from wuyu_harness.data import DataClientError
from omap_api.image_quality import validate_optical_preview


async def main():
    root=Path(__file__).resolve().parents[1]
    folder=root/'outputs/year-priority-imagery';folder.mkdir(parents=True,exist_ok=True)
    private=dotenv_values(root/'.local/unified-access.env')
    client=UnifiedImageryClient({'enabled':True,'base_url':private.get('WUYU_UNIFIED_BASE_URL',''),
        'api_key':private.get('WUYU_UNIFIED_API_KEY',''),'timeout_seconds':120,'max_concurrency':2})
    lon,lat=104.13161622,25.3436014
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'center':[lon,lat],
            'forced_refresh':True,'upstream_repair_reported_version':'2.0.1','secrets_included':False,'checks':[]}
    cases=[{'source':'century','year':2025,'zoom':18,'width':1024,'height':1024},
           {'source':'siwei2025','year':2025,'zoom':18,'width':1024,'height':1024},
           {'source':'tianditu','zoom':18,'width':1024,'height':1024},
           {'source':'gaofen2026','zoom':20,'width':2048,'height':2048}]
    try:
        catalog=await client.catalog()
        report['sources']=catalog['sources']
        for case in cases:
            request={**case,'lon':lon,'lat':lat,'refresh':True}
            try:
                result=await client.image(request)
                content,mime=client.download(result['image_url'])
                name=f"provider-repaired-{stamp}-{case['source']}.png"
                (folder/name).write_bytes(content)
                quality=validate_optical_preview(content)
                row={'request':request,'png_received':True,'status':200,'mime':mime,'bytes':len(content),
                     'sha256':hashlib.sha256(content).hexdigest(),'metadata':result['metadata'],
                     'preview_quality':quality,'artifact':name}
            except DataClientError as error:
                row={'request':request,'png_received':False,'status':error.status_code,'code':error.code,
                     'upstream_code':error.upstream_code,'trace_id':error.trace_id,'message':str(error)}
            report['checks'].append(row)
    finally:await client.aclose()
    report['all_pngs_received']=len(report['checks'])==4 and all(row['png_received'] for row in report['checks'])
    report['all_known_placeholder_checks_passed']=report['all_pngs_received'] and all(row['preview_quality']['valid'] for row in report['checks'])
    report['date_policy']='Requested years, OCR dates and unknown dates are never promoted to verified whole-scene capture dates.'
    target=folder/f'provider-repaired-{stamp}.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'report':str(target),'all_pngs_received':report['all_pngs_received'],
        'checks':[{'source':row['request']['source'],'png_received':row['png_received'],'status':row['status'],
          'code':row.get('code'),'trace_id':row.get('trace_id'),'bytes':row.get('bytes'),
          'zoom':row.get('metadata',{}).get('zoom'),'actual_date':row.get('metadata',{}).get('actual_date'),
          'acquisition_time':row.get('metadata',{}).get('acquisition_time'),
          'date_scope':row.get('metadata',{}).get('acquisition_time_scope')} for row in report['checks']]},ensure_ascii=True))


if __name__=='__main__':asyncio.run(main())
