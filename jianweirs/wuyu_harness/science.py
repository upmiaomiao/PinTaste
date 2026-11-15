"""Native scientific evidence tools and a dedicated multimodal analyst.

Scientific previews never enter the high-resolution solid-waste recognizer.
Provider URLs stay in memory; provider clients never receive local API headers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from datetime import date,datetime,timezone,timedelta
import xml.etree.ElementTree as ET

import httpx
from PIL import Image
from pydantic import Field

from .data import DataClientError
from .evidence import reject_unsupported_claims
from .models import ProviderError
from .schemas import Record, ImageMetadata, UploadRequest


class ScienceObservation(Record):
    asset_id: str
    statement: str = Field(min_length=1, max_length=1600)
    evidence_type: Literal['surface_context', 'spectral_pattern', 'radar_scattering', 'quality', 'temporal_comparison', 'high_resolution_context']


class ScienceCandidate(Record):
    label: str = Field(min_length=1, max_length=200)
    evidence_asset_ids: list[str] = Field(min_length=1, max_length=8)
    alternative_explanations: list[str] = Field(min_length=1, max_length=6)


class ScienceNextAction(Record):
    action: Literal['stop', 'high_resolution_alternate', 'expand_context', 'detail_view', 'multispectral', 'sar', 'timeseries', 'field_verification']
    reason: str = Field(min_length=1, max_length=800)
    asset_ids: list[str] = Field(default_factory=list, max_length=8)


class ScienceAnalysis(Record):
    summary: str = Field(max_length=6000)
    key_evidence_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    observations: list[ScienceObservation] = Field(default_factory=list, max_length=30)
    candidates: list[ScienceCandidate] = Field(default_factory=list, max_length=12)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    recommended_next_actions: list[ScienceNextAction] = Field(default_factory=list, max_length=4)
    stop_recommendation: bool = False


SCIENCE_PROMPT = '''分析当前提供的多光谱或SAR证据，先核查日期、原生分辨率、SCL/QA、标定和空间范围，再说明可复核观测、竞争解释及仍缺证据。
这是独立科学复核角色。光谱低NDVI、裸地、高亮度、SAR强散射均不能独立识别固废、物料成分或非法倾倒；必须与高分影像形态及现场资料关联。
TCI是显示产品，禁止用TCI亮度作反射率时序。raw DN未有真实XML/明确scale-offset证据时不能计算反射率或NDVI。统计值只采用程序已计算且标明质量掩膜的字段。
SAR是已注明辐射量的dB灰度，不是光学RGB；说明斑点、湿度、入射角、轨道、建筑与植被混淆。无实际多时相、配准、轨道可比性时禁止认定雷达变化。
spatial_basis若说明光学目标对应未核验，只能分析独立声明的任务AOI地表，不能将科学图的目标指认为未对齐光学图的候选；光谱/SAR不能修复光学配准。
仅引用给定asset_id；候选可为地表场景解释，不输出危险属性/污染/违法认定或虚构测量。unknown和misaligned空间状态不能作为同点正证据。
在key_evidence_asset_ids中按对当前问题的证据价值选最多4张实际输入的scientific_evidence影像：优先清晰且可比较的前后期基线、关键异常或有解释价值的遮挡对照；不要照输入顺序选前三张，也不要让多张重复薄云图挤占关键清晰时相。只填真实给定的科学影像asset_id，不填高分上下文影像或未展示的场景；没有合适关键图可留空。
根据用户目标与剩余缺口建议最少必要next_actions；已有证据足够回答则stop，不为凑来源数调用其他传感器。只输出结构化观测摘要，不输出内部思考。'''


def parse_product_calibration(payload: bytes) -> dict:
    """Only explicit product XML values are adopted; offsets are never assumed."""
    if len(payload) > 5 * 1024 * 1024 or b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
        raise ValueError('Unsupported product metadata XML')
    root = ET.fromstring(payload)
    name = lambda element: element.tag.rsplit('}', 1)[-1]
    bands = ['B01','B02','B03','B04','B05','B06','B07','B08','B8A','B09','B10','B11','B12']
    mapping = {}
    for element in root.iter():
        if name(element) == 'Spectral_Information':
            physical = element.attrib.get('physicalBand', '').upper()
            if physical.startswith('B') and physical[1:].isdigit(): physical = 'B' + physical[1:].zfill(2)
            if physical in bands: mapping[element.attrib.get('bandId')] = physical
    quantities=[]; offsets={}; baselines=[]
    for element in root.iter():
        key = name(element); text = (element.text or '').strip()
        if key in {'BOA_QUANTIFICATION_VALUE','QUANTIFICATION_VALUE'}: quantities.append(float(text))
        elif key == 'PROCESSING_BASELINE': baselines.append(text)
        elif key == 'BOA_ADD_OFFSET':
            index=element.attrib.get('band_id',element.attrib.get('bandId',''))
            band=mapping.get(index) or (bands[int(index)] if index.isdigit() and int(index)<len(bands) else None)
            if band: offsets.setdefault(band,[]).append(float(text))
    q=quantities[0] if quantities and len(set(quantities))==1 and quantities[0]>0 and math.isfinite(quantities[0]) else None
    values={key:items[0] for key,items in offsets.items() if len(set(items))==1 and math.isfinite(items[0])}
    verified=q is not None and all(band in values for band in ('B04','B08'))
    return {'status':'verified' if verified else 'unknown','quantification_value':q,'boa_add_offset':values,
            'processing_baseline':baselines[0] if baselines and len(set(baselines))==1 else None,
            'source':'actual_product_metadata_XML','offset_assumed':False,'nodata_dn':0,
            'reflectance_formula':'(DN + BOA_ADD_OFFSET[band]) / QUANTIFICATION_VALUE',
            'reference':'https://sentiwiki.copernicus.eu/web/s2-products'}


def _native_crop(href: str, bbox: list, destination: Path, expected_count: int, resolution: float) -> dict:
    import numpy as np
    import rasterio
    from rasterio.windows import Window, from_bounds
    from rasterio.warp import transform_bounds,transform
    from omap_api.range_reader import HttpRangeReader

    readers=[];failures=[]
    virtual='native-science-'+uuid.uuid4().hex+'.tif'
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as provider:
        def opener(fp,mode='rb'):
            if fp!=virtual:raise FileNotFoundError('Sidecar disabled')
            try:
                reader=HttpRangeReader(href,client=provider)
            except Exception as exc:
                failures.append(exc)
                raise
            readers.append(reader);return reader
        try:
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',VSI_CACHE=False), rasterio.open(virtual,opener=opener) as src:
                if src.crs is None or src.count!=expected_count or any(abs(abs(x)-resolution)>1e-6 for x in src.res):raise ValueError('Unexpected native raster schema')
                bounds=transform_bounds('EPSG:4326',src.crs,*bbox,densify_pts=21)
                floating=from_bounds(*bounds,transform=src.transform)
                left,top=math.floor(floating.col_off),math.floor(floating.row_off)
                desired=Window(left,top,math.ceil(floating.col_off+floating.width)-left,math.ceil(floating.row_off+floating.height)-top)
                window=desired.intersection(Window(0,0,src.width,src.height))
                if window.width*window.height>4_000_000:raise ValueError('Native window pixel budget exceeded')
                values=src.read(window=window);mask=src.dataset_mask(window=window)>0;affine=src.window_transform(window)
                with rasterio.open(destination,'w',driver='GTiff',height=values.shape[1],width=values.shape[2],count=src.count,
                    dtype=values.dtype,crs=src.crs,transform=affine,nodata=src.nodata,compress='deflate') as dst:dst.write(values)
                result={'status':'completed','artifact':destination.name,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                        'bytes':destination.stat().st_size,'crs':str(src.crs),'transform':list(affine)[:6],
                        'native_source_transform':list(src.transform)[:6],'width':values.shape[2],'height':values.shape[1],
                        'count':src.count,'dtype':str(values.dtype),'native_resolution_m':list(src.res),
                        'bbox':list(transform_bounds(src.crs,'EPSG:4326',*rasterio.windows.bounds(window,src.transform),densify_pts=21)),
                        'full_task_aoi_covered':desired==window,'valid_fraction':float(mask.mean()),'resampling':'none',
                        'dataset_scales':list(src.scales),'dataset_offsets':list(src.offsets),
                        'transferred_bytes':sum(reader.transferred_bytes for reader in readers),'range_requests':sum(reader.range_requests for reader in readers),
                        'transport_retries':sum(reader.transport_retries for reader in readers),'request_timeout_seconds':30,'provider_authorization_header_sent':False}
                if destination.stem=='SCL':
                    classes,counts=np.unique(values[0],return_counts=True)
                    result['quality']={'class_counts':{str(int(k)):int(n) for k,n in zip(classes,counts)},
                        'class_fractions_all_pixels':{str(int(k)):float(n/values[0].size) for k,n in zip(classes,counts)},
                        'quality_mask_meaning':'SCL categories; 4/5/6 are surface classes, not guaranteed cloud-free truth'}
                    cx,cy=transform('EPSG:4326',src.crs,[(bbox[0]+bbox[2])/2],[(bbox[1]+bbox[3])/2])
                    column,row=(~affine)*(cx[0],cy[0]);column,row=math.floor(column),math.floor(row)
                    yy,xx=np.indices(values[0].shape);xs=affine.c+(xx+.5)*affine.a;ys=affine.f+(yy+.5)*affine.e
                    neighborhood=(xs-cx[0])**2+(ys-cy[0])**2<=60**2
                    local,n=np.unique(values[0][neighborhood],return_counts=True)
                    result['quality'].update(center_class=int(values[0][row,column]) if 0<=row<values.shape[1] and 0<=column<values.shape[2] else None,
                        neighborhood_radius_m=60,neighborhood_pixel_count=int(neighborhood.sum()),
                        neighborhood_class_fractions={str(int(k)):float(count/n.sum()) for k,count in zip(local,n)} if n.size else {})
                return result
        except Exception:
            # Rasterio wraps opener exceptions as RasterioIOError. Restore the
            # safe transport error so callers can retry only transient failures.
            if failures:
                raise failures[-1] from None
            if any(reader.last_error is not None for reader in readers):
                raise next(reader.last_error for reader in reversed(readers) if reader.last_error is not None) from None
            raise
        finally:
            for reader in readers:reader.close()


def _product_xml(href: str, destination: Path) -> dict:
    parsed=urlsplit(href)
    if parsed.scheme!='https' or parsed.username or parsed.password or not (parsed.hostname or '').endswith('.blob.core.windows.net') or parsed.port not in (None,443):raise ValueError('Unsupported metadata host')
    payload=bytearray()
    from omap_api.range_reader import ScientificAssetReadError
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as provider,provider.stream('GET',href) as response:
        if response.status_code in {401,403}:raise ScientificAssetReadError('scientific_asset_signature_rejected',status_code=response.status_code)
        if response.status_code in {408,429,500,502,503,504}:raise ScientificAssetReadError('scientific_asset_http_transient',status_code=response.status_code,retryable=True)
        if response.status_code!=200:raise ValueError('Metadata download unavailable')
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload)>5*1024*1024:raise ValueError('Metadata size budget exceeded')
    calibration=parse_product_calibration(bytes(payload));destination.write_bytes(payload)
    return {'status':'completed','artifact':destination.name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'calibration':calibration}


async def acquire_native_s2(data,scene: dict,bbox: list,output_dir: Path,*,asset_ids=('visual','SCL','B04','B08','product-metadata')) -> dict:
    from .usage_context import imagery_reservation
    event_id = 's2:' + str(output_dir.resolve()) + ':' + str(scene.get('id') or scene.get('scene_id')) + ':' + json.dumps(list(bbox))
    async with imagery_reservation(event_id) as reservation:
        result = await _acquire_native_s2(data,scene,bbox,output_dir,asset_ids=asset_ids)
        if not any(value.get('status') == 'completed' for key,value in result.get('assets',{}).items() if key != 'product-metadata'):
            reservation.cancel()
        return result


async def _acquire_native_s2(data,scene: dict,bbox: list,output_dir: Path,*,asset_ids=('visual','SCL','B04','B08','product-metadata')) -> dict:
    """Actual native COG windows; signed hrefs are deliberately not persisted."""
    identity=scene.get('id') or scene.get('scene_id')
    if not identity:raise ValueError('Missing scene identifier')
    output_dir.mkdir(parents=True,exist_ok=True);assets={};checkpoint=output_dir/'metadata.json'
    if checkpoint.exists():
        try:
            saved=json.loads(checkpoint.read_text(encoding='utf-8'))
            if saved.get('scene_id')==identity and saved.get('requested_bbox')==list(bbox):assets=saved.get('assets',{})
        except (OSError,ValueError):pass
    for key in asset_ids:
        if key not in {'visual','SCL','B04','B08','product-metadata'}:raise ValueError('Unsupported scientific asset')
        old=assets.get(key,{})
        expected_name=key+('.xml' if key=='product-metadata' else '.tif');previous=output_dir/expected_name
        if old.get('status')=='completed' and old.get('artifact')==expected_name and previous.is_file() and hashlib.sha256(previous.read_bytes()).hexdigest()==old.get('sha256'):
            continue
        from omap_api.range_reader import ScientificAssetReadError
        href=None;signature_refresh=False
        for attempt in range(3):
            try:
                if href is None:
                    resolved=await data.execute('multispectral.asset',{'source':'sentinel2','scene_id':identity,'asset_id':key})
                    href=resolved.get('href')
                if not isinstance(href,str):raise ValueError('Missing real asset')
                if signature_refresh:
                    from omap_api.planetary_signing import sign_href
                    # Only the vetted public Blob asset can be re-signed. No
                    # account key, local worker credential or signed URL is saved.
                    signed=await sign_href(urlsplit(href)._replace(query='',fragment='').geturl(),force_refresh=True)
                    href=signed['href'];signature_refresh=False
                if key=='product-metadata':item=await asyncio.to_thread(_product_xml,href,output_dir/(key+'.xml'))
                else:item=await asyncio.to_thread(_native_crop,href,list(bbox),output_dir/(key+'.tif'),3 if key=='visual' else 1,20 if key=='SCL' else 10)
                item['acquisition_attempts']=attempt+1
                break
            except Exception as error:
                code=getattr(error,'code',type(error).__name__)
                signature_refresh=isinstance(error,ScientificAssetReadError) and code=='scientific_asset_signature_rejected'
                retryable=(isinstance(error,httpx.TransportError) or isinstance(error,ScientificAssetReadError) and error.retryable or signature_refresh
                    or isinstance(error,DataClientError) and code in {'spectral_provider_timeout','spectral_provider_unavailable','spectral_provider_error','planetary_signing_timeout','planetary_signing_error','invalid_asset_signature'})
                item={'status':'unavailable','reason':code,'failure_scope':'scene_asset_window','retryable':bool(retryable),'acquisition_attempts':attempt+1}
                if not retryable or attempt==2:break
                await asyncio.sleep(.5*(attempt+1))
        assets[key]=item
        temporary=checkpoint.with_suffix('.tmp')
        temporary.write_text(json.dumps({'source':'sentinel2','scene_id':identity,'requested_bbox':list(bbox),'assets':assets},ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(checkpoint)
    return {'source':'sentinel2','scene_id':identity,'assets':assets,
            'calibration':assets.get('product-metadata',{}).get('calibration',{'status':'unknown','offset_assumed':False}),
            'provider_authorization_header_sent':False,'resampling':'none','whole_scene_downloaded':False}


def scene_search_periods(start: date,end: date,*,temporal=False) -> list[dict]:
    """Sample endpoint neighborhoods explicitly; never claim a full history."""
    if not temporal or (end-start).days<14:
        return [{'label':'requested_period','start':start,'end':end}]
    span=(end-start).days;days=min(45,max(7,math.ceil(span/4)))
    return [{'label':'early_endpoint','start':start,'end':min(end,start+timedelta(days=days))},
            {'label':'late_endpoint','start':max(start,end-timedelta(days=days)),'end':end}]


def rank_science_scenes(scenes: list[dict],*,anchor: date | None=None,earliest=False) -> list[dict]:
    """Scene cloud is only an ordering hint. Even 100% scenes remain eligible."""
    unique={str(scene.get('id') or scene.get('scene_id')):scene for scene in scenes if scene.get('id') or scene.get('scene_id')}
    def key(scene):
        try:instant=datetime.fromisoformat(str(scene.get('acquired_at')).replace('Z','+00:00'))
        except ValueError:instant=datetime.min.replace(tzinfo=timezone.utc)
        if instant.tzinfo is None:instant=instant.replace(tzinfo=timezone.utc)
        try:cloud=float(scene.get('cloud_cover'))
        except (TypeError,ValueError):cloud=101
        if not math.isfinite(cloud) or not 0<=cloud<=100:cloud=101
        tier=0 if math.isfinite(cloud) and 0<=cloud<=35 else 1
        stamp=(instant-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()
        proximity=abs((instant.date()-anchor).days) if anchor else stamp if earliest else -stamp
        return tier,proximity,cloud,str(scene.get('id') or scene.get('scene_id'))
    return sorted(unique.values(),key=key)


def scl_quality_screen(folder: Path,native: dict,task,bbox: list) -> dict:
    """Check actual center/target SCL, never infer local clouds from scene EO cloud."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform
    assets=native.get('assets',{})
    if any(assets.get(key,{}).get('status')!='completed' for key in ('visual','SCL')):
        return {'status':'unknown','usable':False,'reason':'missing_visual_or_SCL','cloud_free_proven':False}
    with rasterio.open(folder/'SCL.tif') as src:
        values=src.read(1);valid=src.read_masks(1)>0
        cx,cy=transform('EPSG:4326',src.crs,[(bbox[0]+bbox[2])/2],[(bbox[1]+bbox[3])/2])
        inverse=~src.transform
        col=math.floor(inverse.a*cx[0]+inverse.b*cy[0]+inverse.c)
        row=math.floor(inverse.d*cx[0]+inverse.e*cy[0]+inverse.f)
        center=int(values[row,col]) if 0<=row<src.height and 0<=col<src.width else None
        rr,cc=np.indices(values.shape);xs=src.transform.c+(cc+.5)*src.transform.a;ys=src.transform.f+(rr+.5)*src.transform.e
        radius=math.sqrt(task.site_area_m2/math.pi) if task.site_area_m2 else 60.
        if task.site_area_m2:
            target=(xs-cx[0])**2+(ys-cy[0])**2<=radius**2;scope='declared_site_area_circle_approximation'
        elif task.bbox:
            # A supplied AOI is the target, rather than an invented tiny point.
            x,y=transform(src.crs,'EPSG:4326',xs.ravel().tolist(),ys.ravel().tolist())
            x=np.array(x).reshape(values.shape);y=np.array(y).reshape(values.shape)
            target=(x>=bbox[0])&(x<=bbox[2])&(y>=bbox[1])&(y<=bbox[3]);scope='requested_task_bbox'
        else:
            target=(xs-cx[0])**2+(ys-cy[0])**2<=60**2;scope='task_center_60m_quality_neighborhood_not_target_boundary'
        count=int(target.sum());surface=target&valid&np.isin(values,[4,5,6]);fraction=float(surface.sum()/count) if count else 0.
        classes,n=np.unique(values[target],return_counts=True)
        complete=all(assets.get(key,{}).get('full_task_aoi_covered') is True for key in ('visual','SCL'))
        usable=count>0 and center in {4,5,6} and fraction>=.8 and (complete or not task.bbox)
        return {'status':'screened_usable' if usable else 'obscured_or_unusable','usable':usable,
            'reason':'local_SCL_surface_screen_passed' if usable else 'incomplete_requested_task_bbox' if task.bbox and not complete else 'target_SCL_cloud_shadow_nodata_or_unclassified',
            'center_class':center,'target_quality_scope':scope,'target_radius_m':radius if not task.bbox or task.site_area_m2 else None,
            'target_pixel_count':count,'target_surface_fraction':fraction,'minimum_surface_fraction':.8,
            'target_class_fractions':{str(int(k)):float(count/n.sum()) for k,count in zip(classes,n)} if n.size else {},
            'whole_window_valid_fraction':float(valid.mean()),'scene_cloud_used_as_local_quality':False,'cloud_free_proven':False,
            'full_requested_window_covered':complete,
            'limitation':'SCL通过仅表示该区域的保守可用筛查；薄云、霾、误分类与地物配准仍须结合影像检查'}


async def probe_s2_scene(pipeline,run_id,task,scene,bbox) -> dict:
    identity=scene.get('id') or scene.get('scene_id')
    if not identity:raise ValueError('Missing scene identity')
    suffix=hashlib.sha256(str(identity).encode()).hexdigest()[:12]
    folder=pipeline.store.run_dir(run_id)/f'science-sentinel2-{suffix}'
    native=await acquire_native_s2(pipeline.data,scene,list(bbox),folder,asset_ids=('visual','SCL'))
    screen=await asyncio.to_thread(scl_quality_screen,folder,native,task,list(bbox))
    screen_path=folder/'quality-screening.json';screen_path.write_text(json.dumps(screen,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'scene_id':identity,'scene_acquired_at':scene.get('acquired_at'),'scene_cloud_cover':scene.get('cloud_cover'),
            'status':'usable' if screen['usable'] else 'download_failed' if screen['status']=='unknown' else 'quality_rejected',
            'catalogue_scene_found':True,'failure_scope':None if screen['usable'] else 'scene_asset_window',
            'quality_screening':screen,
            'native':native,'artifact':str(screen_path.relative_to(pipeline.store.run_dir(run_id))).replace('\\','/'),
            'full_history_complete':False}


def calibrated_ndvi(folder: Path,native: dict) -> dict:
    """ROI context statistic only, with actual calibration and native SCL mask."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform
    calibration=native.get('calibration',{});assets=native.get('assets',{})
    if calibration.get('status')!='verified' or any(assets.get(key,{}).get('status')!='completed' for key in ('B04','B08','SCL')):
        return {'status':'unknown','reason':'missing_explicit_calibration_or_quality_mask','material_classification_supported':False}
    with rasterio.open(folder/'B04.tif') as red,rasterio.open(folder/'B08.tif') as nir,rasterio.open(folder/'SCL.tif') as scl:
        if red.crs!=nir.crs or red.transform!=nir.transform or red.shape!=nir.shape:
            return {'status':'unknown','reason':'native_red_nir_grids_differ','material_classification_supported':False}
        r=red.read(1).astype('float64');n=nir.read(1).astype('float64')
        rows,cols=np.indices(r.shape);xs=red.transform.c+(cols+.5)*red.transform.a;ys=red.transform.f+(rows+.5)*red.transform.e
        if scl.crs!=red.crs:
            x,y=transform(red.crs,scl.crs,xs.ravel().tolist(),ys.ravel().tolist());xs=np.array(x).reshape(r.shape);ys=np.array(y).reshape(r.shape)
        sc=(xs-scl.transform.c)/scl.transform.a;sr=(ys-scl.transform.f)/scl.transform.e
        inside=(sc>=0)&(sr>=0)&(sc<scl.width)&(sr<scl.height);classes=np.zeros(r.shape,dtype='uint8')
        classes[inside]=scl.read(1)[sr[inside].astype('int64'),sc[inside].astype('int64')]
        valid=inside&np.isin(classes,[4,5,6])&(r>0)&(n>0)&(red.read_masks(1)>0)&(nir.read_masks(1)>0)
        q=calibration['quantification_value'];r=(r+calibration['boa_add_offset']['B04'])/q;n=(n+calibration['boa_add_offset']['B08'])/q
        valid&=np.isfinite(r)&np.isfinite(n)&(abs(r+n)>1e-8)
        values=(n[valid]-r[valid])/(n[valid]+r[valid])
        if not values.size:return {'status':'unknown','reason':'no_valid_surface_pixels_after_SCL_and_nodata_mask','material_classification_supported':False}
        return {'status':'computed','formula':'(calibrated_B08 - calibrated_B04)/(calibrated_B08 + calibrated_B04)',
            'calibration_source':'actual_product_metadata_XML','quality_mask':'nearest native SCL class at each native 10m pixel center; classes 4/5/6',
            'pixel_count':int(values.size),'valid_fraction':float(valid.mean()),
            'p05_median_p95':[float(v) for v in np.percentile(values,[5,50,95])],
            'scope':'whole_requested_ROI_context_not_delineated_waste_target','material_classification_supported':False,
            'limitation':'低NDVI可能来自正常裸土、屋顶、道路、阴影或其他地物；不能独立判断固废或倾倒，时序还需地物配准与可比性核验'}


def _register_preview(pipeline,payload,source,scene,*,modality,resolution,display,science_artifact):
    if not isinstance(resolution,(int,float)) or isinstance(resolution,bool) or not math.isfinite(resolution) or resolution<=0:
        resolution=None
    stream=io.BytesIO();payload.save(stream,'PNG')
    request=UploadRequest(filename=f'{source}-科学证据.png',content_base64=base64.b64encode(stream.getvalue()).decode(),
        metadata=ImageMetadata(role='sar' if modality=='sar' else 'target_crop',modality=modality,source=source,
            acquisition_time=scene.get('acquired_at'),resolution_m=resolution,metadata_scope='scientific_native_window_display'))
    registered=pipeline.store.upload(request);asset=pipeline.store.get(registered['id'],'image')
    asset.update(visual_preview=True,science_preview=True,science_artifact=science_artifact,scene_id=scene.get('id') or scene.get('scene_id'),
                 analysis_scope='scientific_context',display_transform=display,acquisition_phase='adaptive_science')
    pipeline.store.put('image',asset);return asset


def calibrated_sar_preview(path: Path,metadata: dict):
    """Only explicitly calibrated gamma0 power becomes 10log10 dB grayscale."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    with rasterio.open(path) as src:
        declared=metadata.get('units')=='linear gamma0 power' or 'gamma0 linear power' in src.tags().get('radiometry','')
        if not declared:raise ValueError('SAR radiometric calibration is unknown')
        values=src.read(1).astype('float64');valid=(src.read_masks(1)>0)&np.isfinite(values)&(values>0)
        if not valid.any():raise ValueError('No valid calibrated SAR pixels')
        db=np.full(values.shape,np.nan);db[valid]=10*np.log10(values[valid]);limits=(-30.,5.)
        gray=np.zeros(values.shape,dtype='uint8');gray[valid]=(np.clip((db[valid]-limits[0])/(limits[1]-limits[0]),0,1)*255).astype('uint8')
        return Image.fromarray(gray,'L'),{'radiometry':'gamma0','input_units':'linear gamma0 power','display_units':'dB',
            'formula':'10*log10(gamma0_power)','display_limits_db':list(limits),'valid_fraction':float(valid.mean()),
            'db_p05_median_p95':[float(x) for x in np.percentile(db[valid],[5,50,95])],
            'polarization':src.descriptions[0] or (metadata.get('bands') or ['unknown'])[0],
            'native_pixel_spacing_m':metadata.get('pixel_spacing_m'),'effective_resolution_m':metadata.get('source_resolution_m'),
            'crs':str(src.crs),'native_transform':list(src.transform)[:6],'dimensions':[src.width,src.height],
            'actual_bbox':list(transform_bounds(src.crs,'EPSG:4326',*src.bounds,densify_pts=21)) if src.crs else None,
            'display_is_optical_RGB':False,'material_classification_supported':False}


async def acquire_science_window(pipeline,run_id,task,group,source,scene,bbox):
    import numpy as np
    import rasterio
    scene={**scene,'id':scene.get('id') or scene.get('scene_id')}
    if not scene['id']:raise ValueError('Missing scene identity')
    suffix=hashlib.sha256(str(scene.get('id') or scene.get('scene_id')).encode()).hexdigest()[:12]
    root=pipeline.store.run_dir(run_id);folder=root/f'science-{source}-{suffix}';folder.mkdir(exist_ok=True)
    if source=='sentinel2' and group=='multispectral':
        probe=await probe_s2_scene(pipeline,run_id,task,scene,list(bbox))
        if probe['status']!='usable':
            download_failed=probe['status']=='download_failed'
            return None,{'status':probe['status'],'operation':'multispectral.asset','source':source,'scene_id':scene['id'],
                'scene_acquired_at':scene.get('acquired_at'),'bbox':bbox,'native':probe['native'],
                'quality_screening':probe['quality_screening'],'artifact':probe['artifact'],
                'reason':probe['quality_screening']['reason'],'numeric_material_classification':False,
                'catalogue_scene_found':True,'failure_scope':'scene_asset_window',
                'limitation':'目录存在该景，但部分窗口资产读取失败，尚不能评估SCL质量；这不代表该时段无影像或整个图源不可用' if download_failed else '当前点位SCL质量未通过，未下载B04/B08或XML，也未把云图当地表证据'}
        native=await acquire_native_s2(pipeline.data,scene,list(bbox),folder)
        native['ndvi']=await asyncio.to_thread(calibrated_ndvi,folder,native)
        visual=native['assets'].get('visual',{});scl=native['assets'].get('SCL',{})
        evidence={'status':'science_window_downloaded' if any(native['assets'].get(k,{}).get('status')=='completed' for k in ('visual','SCL','B04','B08')) else 'unavailable',
            'operation':'multispectral.asset','source':source,'scene_id':scene['id'],'native':native,'bbox':bbox,
            'scene_acquired_at':scene.get('acquired_at'),'numeric_material_classification':False,'calibration':native['calibration'],'ndvi':native['ndvi'],
            'quality_screening':probe['quality_screening'],
            'limitation':'TCI仅显示；光谱或低NDVI不能独立识别固废，原生10/20米可能不足以分辨小目标'}
        evidence['artifact']=str((folder/(visual.get('artifact') or scl.get('artifact') or 'metadata.json')).relative_to(root)).replace('\\','/')
        if visual.get('status')!='completed':return None,evidence
        with rasterio.open(folder/visual['artifact']) as src:rgb=np.moveaxis(src.read(),0,-1)
        image=Image.fromarray(rgb.astype('uint8'),'RGB')
        # A categorical QA panel is kept separate from the TCI pixels, with an
        # explicit legend in metadata. It never adds detail to the native grid.
        if scl.get('status')=='completed':
            with rasterio.open(folder/scl['artifact']) as src:classes=src.read(1)
            colors=np.array([[0,0,0],[255,0,0],[70,70,70],[100,60,20],[0,140,0],[180,130,75],[0,70,200],[150,150,150],[210,210,210],[255,255,255],[170,220,255],[250,100,220]],dtype='uint8')
            qa=Image.fromarray(colors[np.clip(classes,0,11)],'RGB').resize(image.size,Image.Resampling.NEAREST)
            panel=Image.new('RGB',(image.width*2+8,image.height),'white');panel.paste(image,(0,0));panel.paste(qa,(image.width+8,0));image=panel
            evidence['preview_panels']={'left':'provider TCI display, native 10m','right':'SCL categorical quality mask, native 20m shown by nearest for display',
                'SCL_class_legend':{'0':'nodata','1':'defective','2':'dark/topographic shadows','3':'cloud shadow','4':'vegetation','5':'not vegetated','6':'water','7':'unclassified','8':'medium cloud','9':'high cloud','10':'cirrus','11':'snow/ice'}}
        asset=_register_preview(pipeline,image,source,scene,modality='multispectral',resolution=10,
            display='provider_TCI_display_not_quantitative_reflectance; native grid retained in TIFF',science_artifact=evidence['artifact'])
        asset['source_bands']=['visual'];pipeline.store.put('image',asset)
        evidence.update(preview_asset_id=asset['id'],valid_fraction=visual.get('valid_fraction'),full_task_aoi_covered=visual.get('full_task_aoi_covered'),
                        dimensions=[visual['width'],visual['height']],crs=visual['crs'],analysis_scope='scientific_context')
        return asset,evidence
    if source=='sentinel1-rtc' and group=='sar':
        result=await pipeline.data.execute('sar.crop',{'source':source,'item_id':scene['id'],'bbox':bbox,'polarizations':['vv']},
            idempotency_key=f'{run_id}-sar-{suffix}')
        content,_=await pipeline.data.download(result['file_url']);path=folder/'VV.tif';path.write_bytes(content)
        image,calibration=calibrated_sar_preview(path,result)
        calibration['acquisition_geometry']={key:scene.get(key) for key in ('orbit_direction','relative_orbit','instrument_mode','polarizations','band','product')}
        calibration['incidence_angle_verified']=False
        artifact=str(path.relative_to(root)).replace('\\','/')
        asset=_register_preview(pipeline,image,source,scene,modality='sar',resolution=result.get('source_resolution_m'),
            display='calibrated_gamma0_dB_grayscale_fixed_-30_to_5',science_artifact=artifact)
        return asset,{'status':'science_window_downloaded','operation':'sar.crop','source':source,'scene_id':scene['id'],
            'artifact':artifact,'sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content),'bbox':bbox,
            'scene_acquired_at':scene.get('acquired_at'),'preview_asset_id':asset['id'],'calibration':calibration,
            'valid_fraction':calibration['valid_fraction'],'analysis_scope':'scientific_context','numeric_material_classification':False,
            'limitation':'SAR校准dB灰度只反映散射，不能冒充光学RGB或独立判断固废'}
    return None,{'status':'not_attempted','source':source,'reason':'native_scientific_adapter_not_supported','numeric_material_classification':False}


def science_context(asset,evidence):
    """No hrefs, credentials, arbitrary provider text or raw XML in prompts."""
    meta=asset.get('metadata',{})
    context={'asset_id':asset['id'],'metadata':{key:meta.get(key) for key in ('source','modality','acquisition_time','resolution_m','crs')},
             'display_transform':asset.get('display_transform'),'analysis_scope':asset.get('analysis_scope')}
    for key in ('source','scene_id','scene_acquired_at','bbox','full_task_aoi_covered','calibration','valid_fraction','limitation','ndvi','preview_panels','quality_screening','spatial_basis'):
        if key in evidence:context[key]=evidence[key]
    native=evidence.get('native',{})
    if native:context['native_assets']={key:{k:item.get(k) for k in ('status','width','height','crs','transform','native_resolution_m','quality','values') if k in item} for key,item in native.get('assets',{}).items()}
    return context


async def analyze_science(pipeline,task,assets,evidence,traces,*,context_assets=None):
    config=pipeline.settings.models.get('science_analyst')
    if not config or not config.ready or 'image' not in config.input_modalities:
        return {'status':'not_configured','reason':'science_analyst_requires_configured_multimodal_model','science_pixels_analyzed':False}
    if not assets:return {'status':'unavailable','reason':'no_real_scientific_preview','science_pixels_analyzed':False}
    # A single verified optical anchor may help associate land features, but its
    # RGB pixels are not scientific band measurements and are labelled separately.
    context=[]
    for asset in context_assets or []:
        registration=asset.get('spatial_registration') or asset.get('data_api_metadata',{}).get('spatial_registration') or {}
        if asset.get('metadata',{}).get('modality')=='optical' and registration.get('status')=='verified' and registration.get('strict_same_point_eligible') is True:
            context=[asset];break
    parts=[]
    for asset in [*assets,*context]:
        raw=pipeline.store.asset_path(asset).read_bytes()
        role='high_resolution_context' if asset in context else 'scientific_evidence'
        parts.extend([{'type':'text','text':f"asset_id={asset['id']}; input_role={role}"},{'type':'image_url','image_url':{'url':f"data:{asset['mime']};base64,{base64.b64encode(raw).decode()}"}}])
    try:
        contexts=[]
        for asset in assets:
            matching=next((row for row in evidence.get('windows',[]) if row.get('preview_asset_id')==asset['id']),evidence)
            if evidence.get('spatial_basis'):matching={**matching,'spatial_basis':evidence['spatial_basis']}
            contexts.append({'input_role':'scientific_evidence',**science_context(asset,matching)})
        optical_context=[]
        for asset in context:
            registration=asset.get('spatial_registration') or asset.get('data_api_metadata',{}).get('spatial_registration') or {}
            optical_context.append({'asset_id':asset['id'],'input_role':'high_resolution_context',
                'metadata':{key:asset['metadata'].get(key) for key in ('source','modality','acquisition_time','resolution_m')},
                'spatial_registration':{key:registration[key] for key in ('status','strict_same_point_eligible','reference_extent','valid_coverage_fraction','full_requested_bbox_covered','target_center_covered','uncertainty_m') if key in registration},
                'limitation':'已核验对应的高分地物上下文；不是科学光谱或雷达测量，不能为它编造NDVI/散射值'})
        result,trace=await pipeline.models.complete(config,SCIENCE_PROMPT,
            {'task':task.model_dump(mode='json'),'evidence':contexts,'high_resolution_context':optical_context},ScienceAnalysis,parts)
        ids={a['id'] for a in [*assets,*context]};science_ids={a['id'] for a in assets};context_ids={a['id'] for a in context}
        references={row.asset_id for row in result.observations}|{i for row in result.candidates for i in row.evidence_asset_ids}|{i for row in result.recommended_next_actions for i in row.asset_ids}|set(result.key_evidence_asset_ids)
        if not references<=ids:raise ValueError('Scientific analyst cited unknown image')
        if not set(result.key_evidence_asset_ids)<=science_ids:
            raise ValueError('Key scientific evidence must reference actual scientific inputs')
        if any(row.asset_id in context_ids and row.evidence_type in {'spectral_pattern','radar_scattering'} for row in result.observations):
            raise ValueError('Optical context cannot supply scientific measurements')
        if any(not set(row.evidence_asset_ids)&science_ids for row in result.candidates):
            raise ValueError('Scientific candidates must reference scientific evidence')
        reject_unsupported_claims(result.model_dump_json());traces.append(trace)
        return {'status':'completed','science_pixels_analyzed':True,'analyst_role':'science_analyst',**result.model_dump()}
    except (ProviderError,ValueError) as error:
        failure_kind = error.failure_kind if isinstance(error, ProviderError) else 'invalid_response'
        return {'status':'unavailable','reason':type(error).__name__,
                'failure_kind':failure_kind if failure_kind in ProviderError.FAILURE_KINDS else 'provider_error',
                'science_pixels_analyzed':False}
