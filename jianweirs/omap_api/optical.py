"""Named optical source catalogue; provider mosaics are not dated acquisitions."""
from __future__ import annotations
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import SnapshotRequest
from .optical_coordinates import coordinate_contract, unknown_registration
from .registration import (RegistrationError, audit_images, persist_audit,
                           read_audit, read_worker_image)
from .service_security import source_map_types_from_environment

router = APIRouter(prefix='/api/optical', tags=['1 · 高分光学现有影像'])
ROOT = Path(__file__).resolve().parents[1]
SOURCE_SPECS = {
    'bing': (337, 'Bing 卫星图', 'Microsoft / 本机自定义地图'),
    'amap': (221, '高德卫星图', '高德 / 本机2026图源'),
    'tencent': (220, '腾讯卫星图', '腾讯 / 本机2026图源'),
    'google': (317, 'GG 卫星高清混合地图（无字版）', '本机 GG 配置'),
    'mapbox': (329, 'MapBox 卫星与天地图路网混合', '本机 MapBox 配置'),
    'mapquest': (330, 'Mapquest 卫星与天地图路网混合', '本机 Mapquest 配置'),
    'arcgis': (401, 'ArcGIS World Imagery', 'Esri 官方 REST 影像服务'),
    'tianditu': (35, '天地图影像', '奥维内置天地图'),
    'siwei': (48, '四维地球卫星影像', '奥维内置四维地球'),
    'siwei_daily': (23, '四维地球日新图', '中国四维 / 奥维内置'),
}
# IDs confirmed through the desktop menu and command 20012. 20861 has other
# configuration IDs; those must not silently replace the observed MapType.
BUILTIN_MAPS = {
    35: {'id':35,'name':'天地图影像','min_zoom':1,'max_zoom':18,'coord_type':0,
         'metadata_basis':'observed desktop MapType; nominal desktop level cap'},
    48: {'id':48,'name':'四维地球卫星影像','min_zoom':1,'max_zoom':18,'coord_type':0,
         'metadata_basis':'observed desktop MapType; nominal desktop level cap'},
    23: {'id':23,'name':'四维地球日新图','min_zoom':1,'max_zoom':17,'coord_type':0,
         'metadata_basis':'observed desktop MapType; published daily-map level cap'},
}
SourceName = Literal['bing','amap','tencent','google','mapbox','mapquest',
                     'arcgis','tianditu','siwei','siwei_daily']

class OpticalRequest(SnapshotRequest):
    source: SourceName = 'bing'
    input_crs: Literal['EPSG:4326','EPSG:4490','GCJ-02'] | None = None
    registration_mode: Literal['context','strict'] = 'context'
    reference_image_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    correct_alignment: bool = False
    alignment_reference: Literal['arcgis'] | None = None

    @model_validator(mode='after')
    def source_matches_map(self):
        if self.source == 'arcgis' and self.map_type is not None:
            raise ValueError('ArcGIS uses the public Esri export service; no desktop map_type is used.')
        if self.input_crs == 'GCJ-02' and self.coord_type != 1:
            raise ValueError('Explicit GCJ-02 input requires coord_type=1; no silent coordinate relabeling.')
        if self.input_crs in {'EPSG:4326','EPSG:4490'} and self.coord_type != 0:
            raise ValueError('Geographic WGS84/CGCS2000 input requires coord_type=0.')
        if self.alignment_reference and (self.reference_image_id or self.source == 'arcgis'):
            raise ValueError('Automatic ArcGIS reference requires a non-ArcGIS source and no explicit reference_image_id.')
        if self.alignment_reference and (self.coord_type != 0 or self.input_crs == 'GCJ-02'):
            raise ValueError('Automatic reference requires explicit geographic WGS84/CGCS2000 input, not GCJ-02.')
        if self.correct_alignment and not (self.reference_image_id or self.alignment_reference):
            raise ValueError('Correction requires an explicit or automatic reference.')
        if self.registration_mode == 'strict' and self.reference_image_id is None and not self.alignment_reference:
            raise ValueError('Strict same-point imagery requires a reference_image_id for this capture.')
        return self


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_image_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    reference_image_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    correct: bool = False


def configure_optical_sources(application):
    defaults={source:spec[0] for source,spec in SOURCE_SPECS.items()}
    mapping,overrides=source_map_types_from_environment(defaults)
    application.state.optical_source_map_types=mapping
    application.state.optical_source_map_overrides=overrides
    profile=os.getenv('OMAP_OPTICAL_COORDINATE_PROFILE','documented')
    if profile not in {'documented','wgs84_code1'}:
        raise ValueError('OMAP_OPTICAL_COORDINATE_PROFILE must be documented or wgs84_code1.')
    application.state.optical_coordinate_profile=profile
    application.state.optical_registration_lock=asyncio.Lock()
    builtins={}
    for source in ('tianditu','siwei','siwei_daily'):
        default_id=SOURCE_SPECS[source][0]
        local_id=mapping[source]
        item={**BUILTIN_MAPS[default_id],'id':local_id}
        if source in overrides:
            item['metadata_basis']='configured worker MapType; nominal desktop level cap; not verified by a capture'
        builtins[local_id]=item
    application.state.optical_builtin_maps=builtins

def probe_statuses() -> dict:
    try:
        checks=json.loads((ROOT/'outputs/expanded/latest-probe.json').read_text(encoding='utf-8'))['checks']
        return {item['source']:item for item in checks}
    except (OSError, ValueError, KeyError, TypeError):
        return {}

@router.get('/sources')
async def sources(request: Request):
    desktop_status='connected'
    try:
        maps=await request.app.state.available_maps_function()
    except HTTPException:
        maps=[]
        desktop_status='unavailable'
    indexed={item['id']:item for item in maps}
    statuses=probe_statuses()
    result=[]
    mapping=request.app.state.optical_source_map_types
    builtins=request.app.state.optical_builtin_maps
    for source,(default_map_type,label,provider) in SOURCE_SPECS.items():
        map_type=mapping[source]
        item=indexed.get(map_type) or builtins.get(map_type)
        check=statuses.get(source,{})
        if check.get('map_type') != map_type:
            check={}
        code=check.get('error_code')
        status=('permission_denied' if code in {-14,-15} else
                'captured_in_test' if check.get('status')=='captured' else
                'not_ready_in_test' if check else 'not_tested')
        public_esri=source=='arcgis'
        if public_esri:
            try:
                public_verified=(ROOT/'research/esri-latest-live-test.json').is_file()
            except OSError:
                public_verified=False
            status='captured_in_test' if public_verified else 'not_tested'
        result.append({'source':source,'map_type':None if public_esri else map_type,'name':label,
            'map_type_basis':'worker_configuration_override' if source in request.app.state.optical_source_map_overrides and not public_esri else 'default_source_mapping',
            'provider':provider,'configured':public_esri or item is not None,'map_metadata':item,
            'availability':status,'last_error_code':code,
            'satellite_sensor':None,'acquisition_time':None,'native_resolution_m':None,
            'latest_semantics':'current configured provider mosaic; acquisition date not established',
            'requires_desktop':not public_esri,'transport':'esri_rest' if public_esri else 'ovital_websocket',
            'input_coordinate_system':'EPSG:4326',
            'coordinate_contract':coordinate_contract(source,None if public_esri else map_type,item,
                runtime_profile=request.app.state.optical_coordinate_profile),
            'spatial_registration':{**unknown_registration('per_asset_audit_required_not_a_global_provider_status'),
                                    'audit_endpoint':'/api/optical/register',
                                    'engine_available':importlib.util.find_spec('cv2') is not None},
            'available_through_named_api':True})
    return {'sources':result,'desktop_status':desktop_status,
            'availability_basis':'last local probe; not a guarantee of every area or current upstream state',
            'note':'底图服务商不是单颗卫星。真实传感器、拍摄日期和原生分辨率未提供时保持未知。'}

@router.post('/latest')
async def latest(body: OpticalRequest, request: Request):
    if body.alignment_reference:
        # Make a new worker-owned reference inside this job. No public caller
        # can supply another client's worker image ID through the gateway.
        reference = await latest(OpticalRequest(
            source='arcgis', bbox=body.normalized_bbox(), coord_type=0,
            input_crs=body.input_crs or 'EPSG:4326', width=body.width,
            height=body.height, timeout_seconds=body.timeout_seconds), request)
        params = body.model_dump()
        params.update(alignment_reference=None, reference_image_id=reference['image_id'])
        return await latest(OpticalRequest.model_validate(params), request)
    input_crs=body.input_crs or ('GCJ-02' if body.coord_type==1 else 'EPSG:4326')
    maps=await request.app.state.available_maps_function() if body.source!='arcgis' else []
    map_type=request.app.state.optical_source_map_types[body.source]
    item=next((m for m in maps if m.get('id')==map_type),None)
    contract=coordinate_contract(body.source,None if body.source=='arcgis' else map_type,item,
                                 list(body.normalized_bbox()),input_crs=input_crs,
                                 request_coord_type=body.coord_type,
                                 runtime_profile=request.app.state.optical_coordinate_profile)
    # Validate a requested reference before any provider call or desktop mutation.
    if body.reference_image_id:
        try:
            from .registration import anchor_extent
            _,reference_metadata=await asyncio.to_thread(read_worker_image,request.app.state.output_dir,body.reference_image_id)
            anchor_extent(reference_metadata)
        except RegistrationError as exc:
            raise HTTPException(422,detail={'code':str(exc),'message':'Reference must be an existing worker-owned Esri export with actual extent.'}) from None
    if body.source == 'arcgis':
        if body.coord_type != 0 or input_crs == 'GCJ-02':
            raise HTTPException(422,detail={'code':'coordinate_conversion_required',
                'message':'ArcGIS独立服务输入须为WGS84经纬度；不能将GCJ-02直接代入。'})
        from .esri_imagery import EsriImageryClient, EsriImageryError
        try:
            # Esri accepts WGS84. Explicit CGCS input is transformed through PROJ;
            # no claim of epoch-aware geodetic accuracy is made.
            bbox=contract['requested_bbox_wgs84']
            result=await EsriImageryClient(timeout=body.timeout_seconds).capture(
                bbox,width=body.width,height=body.height,
                timeout_seconds=body.timeout_seconds)
        except EsriImageryError as exc:
            raise HTTPException(exc.status_code,detail={'code':'optical_provider_error','message':str(exc)}) from None
        result['metadata']['source_key']='arcgis'
        result['metadata']['input_coordinate_system']='EPSG:4326'
        result['metadata']['zoom_parameter_applied']=False
        result['metadata']['coordinate_contract']=contract
        result['metadata']['spatial_registration']=unknown_registration('actual_export_extent_known_but_no_control_point_audit')
        response=await request.app.state.store_external_image_function(result)
        if body.reference_image_id:
            response=await register(RegistrationRequest(source_image_id=response['image_id'],
                                    reference_image_id=body.reference_image_id,correct=body.correct_alignment),request)
        return _strict_response(body,response)
    if body.map_type is not None and body.map_type != map_type:
        raise HTTPException(422,detail={'code':'source_map_type_conflict',
            'message':'map_type conflicts with this worker source mapping. Use /api/snapshot for a custom map ID.'})
    params=body.model_dump(exclude={'source','input_crs','registration_mode','reference_image_id','correct_alignment','alignment_reference'})
    params.update(bbox=contract['request_bbox'],center=None,radius_m=None,map_type=map_type,
                  coord_type=contract['request_coord_type'])
    base=SnapshotRequest.model_validate(params)
    image_id,_,metadata=await request.app.state.snapshot_function(base)
    metadata.update(source_key=body.source,
        latest_semantics='current configured provider mosaic; acquisition date not established',
        coordinate_system=contract['request_crs'],coordinate_system_verified=False,
        coordinate_system_basis='explicit_local_runtime_profile; relative image control audit required',
        coordinate_contract=contract,spatial_registration=unknown_registration())
    await asyncio.to_thread(_update_metadata,request.app.state.output_dir,image_id,metadata)
    response={'image_id':image_id,'image_url':f'/api/images/{image_id}.png','metadata':metadata}
    if body.reference_image_id:
        response=await register(RegistrationRequest(source_image_id=image_id,
                                reference_image_id=body.reference_image_id,correct=body.correct_alignment),request)
    return _strict_response(body,response)


def _strict_response(body: OpticalRequest, response: dict) -> dict:
    if body.registration_mode=='strict' and not response['metadata']['spatial_registration'].get('strict_same_point_eligible'):
        raise HTTPException(422,detail={'code':'same_point_registration_unverified',
            'message':'This image has insufficient local spatial verification; it is available only as context.',
            'image_id':response['image_id'],'spatial_registration':response['metadata']['spatial_registration']})
    return response


def _update_metadata(folder: Path, image_id: str, metadata: dict):
    from .registration import IMAGE_ID
    if not IMAGE_ID.fullmatch(image_id):
        raise RegistrationError('invalid_image_id')
    path=folder/f'{image_id}.json'
    if path.is_symlink():
        raise RegistrationError('invalid_metadata_path')
    temporary=folder/f'.{image_id}.optical.tmp'
    temporary.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


@router.post('/register')
async def register(body: RegistrationRequest, request: Request):
    async with request.app.state.optical_registration_lock:
        return await _register_locked(body,request)


async def _register_locked(body: RegistrationRequest, request: Request):
    if body.source_image_id==body.reference_image_id:
        raise HTTPException(422,detail={'code':'independent_reference_required','message':'An image cannot verify itself.'})
    folder=request.app.state.output_dir
    try:
        source,metadata=await asyncio.to_thread(read_worker_image,folder,body.source_image_id)
        reference,reference_metadata=await asyncio.to_thread(read_worker_image,folder,body.reference_image_id)
        audit,corrected=await asyncio.to_thread(audit_images,source,reference,reference_metadata,correct=body.correct)
    except RegistrationError as exc:
        raise HTTPException(422,detail={'code':str(exc),'message':'Local registration inputs did not meet the coordinate contract.'}) from None
    audit['source_image_id']=body.source_image_id
    audit['reference_image_id']=body.reference_image_id
    audit['configuration_fingerprint']=metadata.get('coordinate_contract',{}).get('configuration_fingerprint')
    # Mutual spatial support may validate the anchor relative to these source
    # landmarks. Never call this independent satellite or survey verification.
    reference_audit=None
    if audit.get('unique_inlier_count',0)>=8 and audit.get('reason') in {
            'local_control_points_consistent','local_transform_required','local_corrected_target_covered'}:
        previous=reference_metadata.get('spatial_registration',{})
        supporters=list(previous.get('supporting_source_sha256',[])) if isinstance(previous,dict) else []
        if audit['source_sha256'] not in supporters:
            supporters.append(audit['source_sha256'])
        reference_audit={**unknown_registration(),'status':'verified',
            'strict_same_point_eligible':True,'scope':'relative_at_this_aoi',
            'reason':'cross_image_landmarks_support_reference_extent',
            'method':'cross_image_control_point_support',
            'source_sha256':audit['reference_asset_sha256'],
            'configuration_fingerprint':reference_metadata.get('coordinate_contract',{}).get('configuration_fingerprint'),
            'reference_extent':audit['reference_extent'],
            'supporting_source_sha256':supporters[-20:],
            'supporting_image_count':len(supporters),
            'latest_unique_inlier_count':audit['unique_inlier_count'],
            'latest_relative_rms_pixels':audit['rms_pixels'],
            'warning':'Relative image-location support, not independent satellite acquisition, absolute surveyed accuracy or date verification.'}
        reference_metadata['spatial_registration']=reference_audit
        await asyncio.to_thread(persist_audit,folder,body.reference_image_id,reference_audit)
        await asyncio.to_thread(_update_metadata,folder,body.reference_image_id,reference_metadata)
    if corrected is not None:
        # Preserve the raw source and its misalignment result. A corrected raster
        # is a new artifact, with transparency for areas absent in the source.
        raw_audit={**audit,'status':'misaligned' if audit.get('raster_center_displacement_reference_pixels',0)>8 else 'unknown',
                   'strict_same_point_eligible':False,'reason':'use_separate_locally_corrected_artifact','corrected':False}
        await asyncio.to_thread(persist_audit,folder,body.source_image_id,raw_audit)
        metadata['spatial_registration']=raw_audit
        await asyncio.to_thread(_update_metadata,folder,body.source_image_id,metadata)
        extent=audit['reference_extent']
        derived={**metadata,**extent,'source_key':metadata.get('source_key'),
                 'derived_from_image_id':body.source_image_id,'source_original_sha256':audit['source_sha256'],
                 'reference_image_id':body.reference_image_id,'spatial_registration':audit,
                 'georeferencing_basis':'local_similarity_to_esri_actual_extent',
                 'image_contains_embedded_georeferencing':False,'georeferenced':False,
                 'acquisition_time':None,'native_resolution_m':None,
                 'valid_coverage_fraction':audit['valid_coverage_fraction'],
                 'full_requested_bbox_covered':audit['full_requested_bbox_covered'],
                 'coverage_verified':audit['target_center_covered'],
                 'warnings':list(metadata.get('warnings',[]))+['Locally aligned display image. Transparent pixels have no source coverage. Source date/native GSD remain unverified.']}
        response=await request.app.state.store_external_image_function({'image_bytes':corrected,'metadata':derived})
        audit={**audit,'original_source_sha256':audit['source_sha256'],
               'source_sha256':audit['corrected_sha256'],'corrected_image_id':response['image_id']}
        response['metadata']['spatial_registration']=audit
        await asyncio.to_thread(persist_audit,folder,response['image_id'],audit)
        await asyncio.to_thread(_update_metadata,folder,response['image_id'],response['metadata'])
        response['reference_image_id']=body.reference_image_id
        response['reference_spatial_registration']=reference_audit or reference_metadata.get('spatial_registration',unknown_registration())
        return response
    metadata['spatial_registration']=audit
    await asyncio.to_thread(persist_audit,folder,body.source_image_id,audit)
    await asyncio.to_thread(_update_metadata,folder,body.source_image_id,metadata)
    return {'image_id':body.source_image_id,'image_url':f'/api/images/{body.source_image_id}.png','metadata':metadata,
            'reference_image_id':body.reference_image_id,
            'reference_spatial_registration':reference_audit or reference_metadata.get('spatial_registration',unknown_registration())}


@router.get('/registration/{image_id}')
async def registration_result(image_id: str, request: Request):
    try:
        png,metadata=await asyncio.to_thread(read_worker_image,request.app.state.output_dir,image_id)
    except RegistrationError:
        raise HTTPException(404,detail={'code':'image_not_found','message':'Image not found.'}) from None
    audit=await asyncio.to_thread(read_audit,request.app.state.output_dir,image_id,png,
             metadata.get('coordinate_contract',{}).get('configuration_fingerprint'))
    return {'image_id':image_id,'spatial_registration':audit}
