"""Real local authenticated worker+gateway checks; never prints private keys."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import time
import httpx
from PIL import Image
import rasterio

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8766')
    args=parser.parse_args()
    config=json.loads((ROOT/'cluster.json').read_text(encoding='utf-8-sig'))
    bearer=config['clients'][0]['api_key']
    checks=[]
    def record(name,passed,**values):
        item={'name':name,'passed':bool(passed),**values}
        checks.append(item)
        print(json.dumps(item,ensure_ascii=False),flush=True)
    with httpx.Client(base_url=args.base_url,timeout=20,
                      headers={'Authorization':'Bearer '+bearer,'ngrok-skip-browser-warning':'1'}) as client:
        record('gateway rejects anonymous requests',httpx.get(args.base_url+'/api/nodes',timeout=5).status_code==401)
        node=next(n for n in config['nodes'] if n.get('enabled',True))
        record('worker rejects anonymous API',httpx.get(node['base_url']+'/api/maps',timeout=5).status_code==401)
        for _ in range(40):
            nodes=client.get('/api/nodes').json()['nodes']
            if any(n['http_online'] and n['desktop_ready'] for n in nodes):break
            time.sleep(.2)
        record('real local node connected',any(n['http_online'] and n['desktop_ready'] for n in nodes),
               enabled_node_count=len(nodes),real_three_machine_tested=False)
        record('public history route omitted',client.post('/api/history/search',json={}).status_code==404)
        plans=[('optical','/api/optical/latest',{'source':'bing','bbox':[116.446,39.849,116.456,39.859],
                   'zoom':16,'width':512,'height':512,'timeout_seconds':45}),
               ('multispectral','/api/multispectral/search',{'source':'sentinel2','bbox':[103.8,30.5,104.3,30.9],
                   'datetime':'2026-09-01/2026-10-13','limit':1}),
               ('hyperspectral','/api/hyperspectral/search',{'source':'emit','bbox':[103.8,30.5,104.3,30.9],
                   'datetime':'2022-01-01/2026-10-13','limit':1}),
               ('sar','/api/sar/search',{'source':'sentinel1-rtc','bbox':[103.8,30.5,104.3,30.9],'limit':1})]
        jobs={}
        for name,path,params in plans:
            r=client.post(path,json=params)
            record(name+' accepted',r.status_code==202)
            if r.status_code==202:jobs[name]=r.json()['job_id']
        def wait(job):
            end=time.monotonic()+200
            while time.monotonic()<end:
                r=client.get('/api/jobs/'+job)
                body=r.json()
                if body.get('state') in {'succeeded','failed'}:return body
                time.sleep(.25)
            return {'state':'timeout'}
        outcomes={}
        for name,job in jobs.items():
            body=wait(job)
            outcomes[name]=body
            result=body.get('result') or {}
            record(name+' completed',body.get('state')=='succeeded',state=body.get('state'),
                   node=body.get('node'),error=body.get('error'))
            if name=='optical' and result.get('image_url'):
                r=client.get(result['image_url'])
                with Image.open(io.BytesIO(r.content)) as image:
                    valid=r.status_code==200 and image.size==(512,512) and len(image.convert('RGB').getcolors(512*512) or [])>1000
                (ROOT/'outputs'/'gateway-bing.png').write_bytes(r.content)
                record('gateway centralized real PNG download',valid,bytes=len(r.content))
            if name in {'multispectral','hyperspectral'}:
                record(name+' actual scene catalog',result.get('count',0)>0,
                       count=result.get('count'),science_cube_downloaded=False,
                       acquired_at=[s.get('acquired_at') for s in result.get('scenes',[])])
            if name=='sar':
                record('SAR actual scene catalog',result.get('returned',0)>0,
                       returned=result.get('returned'),acquired_at=[s.get('acquired_at') for s in result.get('items',[])])
        # Download real numerical reflectance DN through a fresh gateway task.
        r=client.post('/api/multispectral/subset',json={'source':'sentinel2',
            'scene_id':'S2B_MSIL2A_20260905T034529_R104_T48RVV_20260905T073723',
            'bbox':[104.046,30.656,104.054,30.664],'assets':['B02','B03','B04','B08'],'width':128,'height':128})
        body=wait(r.json()['job_id']) if r.status_code==202 else {'state':'rejected'}
        result=body.get('result') or {}
        if result.get('file_url'):
            data=client.get(result['file_url'])
            with rasterio.MemoryFile(data.content) as mem:
                with mem.open() as image:
                    valid=data.status_code==200 and image.count==4 and image.crs is not None and image.read(masked=True).count()>0
                    fields={'bands':image.count,'crs':str(image.crs),'dimensions':[image.width,image.height]}
            (ROOT/'outputs'/'gateway-sentinel2.tif').write_bytes(data.content)
            record('gateway real multispectral GeoTIFF copy',valid,**fields)
        else:
            record('gateway real multispectral GeoTIFF copy',False,state=body.get('state'),error=body.get('error'))
        # RTC canonical example has already been validated against this ROI;
        # this exercises gateway file retrieval, storage, and numerical output.
        r=client.post('/api/sar/crop',json={'source':'sentinel1-rtc',
            'item_id':'S1A_IW_GRDH_1SDV_20250120T230451_20250120T230516_057534_07161B_rtc',
            'bbox':[104.046,30.656,104.054,30.664],'polarizations':['vv','vh']})
        body=wait(r.json()['job_id']) if r.status_code==202 else {'state':'rejected'}
        result=body.get('result') or {}
        if result.get('file_url'):
            data=client.get(result['file_url'])
            with rasterio.MemoryFile(data.content) as mem:
                with mem.open() as image:
                    valid=data.status_code==200 and image.count==2 and image.crs is not None and image.read(masked=True).count()>0
                    fields={'bands':image.count,'crs':str(image.crs),'dimensions':[image.width,image.height]}
            (ROOT/'outputs'/'gateway-sentinel1.tif').write_bytes(data.content)
            record('gateway real VV-VH GeoTIFF copy',valid,**fields)
        else:
            record('gateway real VV-VH GeoTIFF copy',False,state=body.get('state'),error=body.get('error'))
    report={'tested_at':datetime.now(timezone.utc).isoformat(),'base_url':args.base_url,
            'scope':'One real local node behind authenticated gateway; external public ingress and other physical Windows nodes not tested',
            'checks':checks,'pass_count':sum(i['passed'] for i in checks),'total_checks':len(checks),
            'passed':all(i['passed'] for i in checks)}
    (ROOT/'outputs'/'cluster-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"RESULT {report['pass_count']}/{report['total_checks']}")
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
