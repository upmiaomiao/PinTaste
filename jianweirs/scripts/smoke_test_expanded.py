"""Accept the four API groups over the running HTTP service, with real data."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'outputs/expanded'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8765')
    args=parser.parse_args()
    FOLDER.mkdir(parents=True,exist_ok=True)
    checks=[]
    def record(name,passed,**fields):
        checks.append({'name':name,'passed':bool(passed),**fields})
        print(json.dumps(checks[-1],ensure_ascii=False),flush=True)
    with httpx.Client(base_url=args.base_url,timeout=130) as client:
        catalogs={}
        spec=client.get('/openapi.json')
        paths=spec.json().get('paths',{})
        record('four HTTP groups',spec.status_code==200 and all(p in paths for p in (
            '/api/optical/latest','/api/history/search','/api/spectral/search','/api/sar/search')),
            version=spec.json().get('info',{}).get('version'))
        for group in ('optical','history','spectral','sar'):
            response=client.get(f'/api/{group}/sources')
            body=response.json()
            catalogs[group]=body
            record(f'{group} source catalog',response.status_code==200 and len(body.get('sources',[]))>0,
                status=response.status_code,count=len(body.get('sources',[])))
        response=client.post('/api/history/capture',json={'source':'wayback','release_id':29260,
            'bbox':[116.446,39.849,116.456,39.859],'coord_type':1})
        record('Wayback GCJ-02 rejected',response.status_code==422,status=response.status_code)
        response=client.post('/api/history/search',json={'source':'wayback','max_cloud_cover':20})
        record('Wayback unsupported cloud filter rejected',response.status_code==422,status=response.status_code)
        hls=[s for s in catalogs['spectral']['sources'] if s.get('id','').startswith('hls_')]
        record('HLS product grid distinguished from sensor resolution',len(hls)==2 and
            all(s.get('resolution_semantics')=='harmonized_product_grid' and
                s.get('product_grid_spacing_m')==30 for s in hls))
        bbox=[116.446,39.849,116.456,39.859]
        def validate_png(name,response):
            info={'status':response.status_code}
            if response.status_code!=200:
                record(name,False,**info,error=response.json())
                return
            body=response.json()
            image=client.get(body['image_url'])
            valid=False
            if image.status_code==200:
                with Image.open(io.BytesIO(image.content)) as opened:
                    valid=opened.format=='PNG' and opened.size==(512,512)
                    colors=opened.convert('RGB').getcolors(maxcolors=512*512)
                    color_count=len(colors or [])
                    valid=valid and color_count>1000
                (FOLDER/f'{name}.png').write_bytes(image.content)
                info.update(sha256=hashlib.sha256(image.content).hexdigest(),color_count=color_count)
            record(name,valid,**info,image_url=body.get('image_url'),metadata=body.get('metadata'))
        for source in ('bing','amap','tencent','google','mapbox','mapquest','arcgis'):
            response=client.post('/api/optical/latest',json={'source':source,'bbox':bbox,
                'zoom':16,'width':512,'height':512,'timeout_seconds':45})
            validate_png(f'http-{source}',response)
        for source in ('tianditu','siwei'):
            response=client.post('/api/optical/latest',json={'source':source,'bbox':bbox,
                'zoom':16,'width':512,'height':512,'timeout_seconds':15})
            record(f'{source} restriction reported',response.status_code==403,
                status=response.status_code,response=response.json(),imagery_available=False)
        for source in ('history246','ovital_history','siwei_daily','century'):
            response=client.post('/api/history/search',json={'source':source,
                'start_date':'2020-01-01','end_date':'2024-12-31','bbox':bbox})
            record(f'{source} date selection restriction',response.status_code==409 and
                response.json().get('detail',{}).get('date_was_not_applied') is True,
                status=response.status_code,response=response.json(),date_selection_available=False)
        response=client.post('/api/history/search',json={'source':'wayback',
            'start_date':'2020-01-01','end_date':'2020-12-31','limit':1})
        body=response.json()
        record('Wayback 2020 version search',response.status_code==200 and len(body.get('versions',[]))>0,
            status=response.status_code,response=body)
        if body.get('versions'):
            response=client.post('/api/history/capture',json={'source':'wayback','bbox':bbox,
                'release_id':body['versions'][0]['release_id'],'zoom':16,
                'width':512,'height':512,'timeout_seconds':60})
            validate_png('http-wayback-2020',response)
        chengdu=[103.8,30.5,104.3,30.9]
        response=client.post('/api/history/search',json={'source':'sentinel2','bbox':chengdu,
            'start_date':'2025-01-01','end_date':'2025-12-31','limit':2})
        body=response.json()
        record('satellite history acquisition query',response.status_code==200 and body.get('count',0)>0,
            status=response.status_code,count=body.get('count'),
            acquired_at=[s.get('acquired_at') for s in body.get('scenes',[])])
        for source in ('emit','enmap'):
            response=client.post('/api/spectral/search',json={'source':source,'bbox':chengdu,
                'datetime':'2022-01-01/2026-10-11','limit':2})
            body=response.json()
            record(f'{source} true hyperspectral scenes',response.status_code==200 and body.get('count',0)>0,
                status=response.status_code,count=body.get('count'),science_files_downloaded=False,
                acquired_at=[s.get('acquired_at') for s in body.get('scenes',[])],
                assets_per_scene=[len(s.get('assets',{})) for s in body.get('scenes',[])])
        response=client.post('/api/sar/search',json={'source':'sentinel1-rtc','bbox':chengdu,'limit':2})
        body=response.json()
        record('latest Sentinel-1 RTC scenes',response.status_code==200 and body.get('returned',0)>0,
            status=response.status_code,returned=body.get('returned'),
            acquired_at=[s.get('acquired_at') for s in body.get('items',[])])
        # Serve and reopen the already verified actual science crops. Separate
        # upstream acceptance reports contain the real download/crop evidence.
        import rasterio
        for group,subfolder,count in [('spectral','spectral/subsets',4),('sar','sar',2)]:
            files=sorted((ROOT/'outputs'/subfolder).glob('*.tif'),key=lambda p:p.stat().st_mtime,reverse=True)
            if not files:
                record(f'{group} science file HTTP',False,error='No actual upstream crop file exists')
                continue
            path=files[0]
            response=client.get(f'/api/{group}/files/{path.name}')
            valid=False
            if response.status_code==200:
                with rasterio.MemoryFile(response.content) as memory:
                    with memory.open() as dataset:
                        data=dataset.read(masked=True)
                        valid=dataset.count==count and dataset.crs is not None and not data.mask.all()
                        info={'bands':dataset.count,'dimensions':[dataset.width,dataset.height],
                              'crs':str(dataset.crs),'dtype':dataset.dtypes[0]}
            else:
                info={}
            record(f'{group} science file HTTP',valid,status=response.status_code,**info)
    report={'tested_at':datetime.now(timezone.utc).isoformat(),'base_url':args.base_url,
        'scope':'Running unified HTTP service; expected access restrictions are separate from image availability',
        'checks':checks,'pass_count':sum(c['passed'] for c in checks),'total_checks':len(checks),
        'passed':all(c['passed'] for c in checks)}
    (FOLDER/'http-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"RESULT {report['pass_count']}/{report['total_checks']} checks",flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':
    raise SystemExit(main())
