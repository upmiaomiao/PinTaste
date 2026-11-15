"""Bounded real OMAP source/date verification; keys stay in the local env file."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from dotenv import dotenv_values
from PIL import Image


ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'outputs/year-priority-imagery'
SAFE_SOURCE_KEYS={'id','source','name','configured','enabled','available','status','min_zoom','max_zoom','zoom_min','zoom_max',
                  'native_max_zoom','preferred_zoom','recommended_zoom','years','supported_years','requires_year','supports_times',
                  'supports_date','max_native_zoom','supported_zooms','zoom_levels','native_zoom','edition_year','year'}


async def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    task=json.loads((OUTPUT/'prod-task-80901d12.json').read_text(encoding='utf-8'))
    lon,lat=task['task']['center']
    private=dotenv_values(ROOT/'.local/unified-access.env')
    key=private.get('WUYU_UNIFIED_API_KEY','')
    base=private.get('WUYU_UNIFIED_BASE_URL','').rstrip('/')
    if not key or not base:raise SystemExit('Private imagery configuration is missing')
    def clean(text):
        value=str(text).replace(key,'[hidden]')
        value=re.sub(r'https?://\S+','[upstream_url]',value)
        value=re.sub(r'(?i)(api[_-]?key|token|psk|authorization)\s*[:=]\s*\S+',r'\1=[hidden]',value)
        return value[:600]
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'task_id':task['task_id'],'center':[lon,lat],
            'provider_origin':base,'secrets_included':False,'checks':[]}
    headers={'X-API-Key':key,'ngrok-skip-browser-warning':'1'}
    async with httpx.AsyncClient(timeout=120,trust_env=False,follow_redirects=False,headers=headers) as client:
        async def get(path,params=None,filename=None):
            began=time.monotonic()
            try:response=await client.get(base+path,params=params)
            except httpx.HTTPError as error:
                return {'path':path,'params':params,'error':type(error).__name__,'elapsed_seconds':round(time.monotonic()-began,2)}
            row={'path':path,'params':params,'status':response.status_code,'mime':response.headers.get('content-type','').split(';')[0],
                 'elapsed_seconds':round(time.monotonic()-began,2)}
            if response.is_success and row['mime'].startswith('image/'):
                with Image.open(io.BytesIO(response.content)) as image:
                    image.verify();row['dimensions']=list(image.size)
                row.update(bytes=len(response.content),sha256=hashlib.sha256(response.content).hexdigest())
                try:metadata=json.loads(response.headers.get('x-imagery-metadata','{}'))
                except ValueError:metadata={}
                keys={'source','year','edition','actual_date','actual_dates','actual_date_method','actual_date_scope','metadata_scope',
                      'native_max_zoom','zoom','coordinate_system','native_coordinate_system','requested_year','dataset_year','config_group_year'}
                row['metadata']={k:v for k,v in metadata.items() if k in keys}
                if filename:
                    (OUTPUT/filename).write_bytes(response.content);row['artifact']=filename
            else:
                try:body=response.json()
                except ValueError:body={}
                if response.is_error:
                    detail=body.get('detail',{}) if isinstance(body,dict) else {}
                    row['error_detail']=({k:clean(v) for k,v in detail.items() if k in {'code','message','reason','error','detail','hint'}}
                                         if isinstance(detail,dict) else clean(detail))
                    row['response_fields']=list(body) if isinstance(body,dict) else []
                elif path=='/sources':
                    rows=body if isinstance(body,list) else body.get('sources',[])
                    row['sources']=[{k:v for k,v in item.items() if k in SAFE_SOURCE_KEYS} for item in rows if isinstance(item,dict)]
                elif path=='/times':
                    values=body if isinstance(body,list) else body.get('dates',body.get('times',[]))
                    dates=[]
                    for value in values:
                        candidate=value.get('date',value.get('actual_date')) if isinstance(value,dict) else value
                        if isinstance(candidate,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',candidate):dates.append(candidate)
                    row['dates']=sorted(set(dates));row['dates_count']=len(row['dates'])
                    target=date(2025,12,1)
                    row['nearest_to_2025_12_01']=sorted(row['dates'],key=lambda d:abs((date.fromisoformat(d)-target).days))[:5]
                elif path=='/openapi.json':
                    row['image_parameters']=[{'name':p.get('name'),'required':p.get('required'),'schema':p.get('schema')}
                                             for p in body.get('paths',{}).get('/image',{}).get('get',{}).get('parameters',[])]
            return row
        for path in ('/sources','/openapi.json'):
            report['checks'].append(await get(path))
        for source in ('siwei2025','century'):
            report['checks'].append(await get('/image',{'source':source,'year':2025,'lon':lon,'lat':lat,'zoom':18,
                'width':1024,'height':1024,'labels':'false'},source+'-2025-native-z18.png'))
        report['checks'].append(await get('/times',{'source':'history246','lon':lon,'lat':lat,'zoom':18}))
    target=OUTPUT/'provider-live-verification.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'report':str(target),'checks':report['checks']},ensure_ascii=True))


if __name__=='__main__':asyncio.run(main())
