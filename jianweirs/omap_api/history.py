"""Historical scenes and mosaic versions with explicit time semantics."""
from __future__ import annotations
import asyncio
from datetime import date
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import SnapshotRequest

router=APIRouter(prefix='/api/history',tags=['2 · 历史时序影像'])
DesktopHistory=Literal['history246','ovital_history','siwei_daily','century']
HistorySource=Literal['wayback','sentinel2','landsat','hls_s30','hls_l30',
                      'history246','ovital_history','siwei_daily','century']
DESKTOP_HISTORY={
    'history246':{'name':'246历史影像','map_type':246,'alternative_map_types':[215]},
    'ovital_history':{'name':'奥维高清历史影像','map_type':291},
    'siwei_daily':{'name':'四维地球日新图','map_type':23},
    'century':{'name':'世纪空间卫星影像','map_type':60},
}

def unavailable(source: str):
    return HTTPException(409,detail={'code':'temporal_selection_unavailable',
        'source':source,'message':'本机9.8.5的公开WebSocket取图命令没有日期选择或历史日期列表字段，无法可靠按日期取图。',
        'required_action':'246/奥维自定义历史需验证新版官方HTTP多时相服务；四维/世纪空间需正式允许按日期导出的接口及相应授权。',
        'date_was_not_applied':True})

class HistorySearchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    source: HistorySource='wayback'
    start_date: date=date(2020,1,1)
    end_date: date=Field(default_factory=date.today)
    bbox: tuple[float,float,float,float] | None=None
    limit: int=Field(default=10,ge=1,le=50)
    max_cloud_cover: float | None=Field(default=None,ge=0,le=100)

    @model_validator(mode='after')
    def validate_query(self):
        if self.start_date>self.end_date:
            raise ValueError('start_date must be <= end_date')
        if self.source=='wayback' and self.max_cloud_cover is not None:
            raise ValueError('Wayback mosaics do not support scene cloud-cover filtering')
        if self.bbox is not None:
            from .spectral import _check_bbox
            _check_bbox(self.bbox,5.0)
        if self.source in {'sentinel2','landsat','hls_s30','hls_l30'} and self.bbox is None:
            raise ValueError('bbox is required for satellite scene searches')
        return self

class HistoryCaptureRequest(SnapshotRequest):
    source: Literal['wayback','history246','ovital_history','siwei_daily','century']='wayback'
    coord_type: Literal[0]=Field(default=0,description='Wayback requires WGS84 coordinates; GCJ-02 is unsupported.')
    release_id: int | None=Field(default=None,gt=0)

    @model_validator(mode='after')
    def validate_release(self):
        if self.source=='wayback' and self.release_id is None:
            raise ValueError('wayback requires release_id from /api/history/search')
        if self.map_type is not None:
            raise ValueError('use source; map_type is not a temporal selection parameter')
        return self

@router.get('/sources')
async def history_sources():
    sources=[{'source':'wayback','name':'Esri World Imagery Wayback',
        'availability':'public_catalog_and_imagery','time_semantics':'mosaic_release_date',
        'date_selection':True,'requires_account':False}]
    for key,value in DESKTOP_HISTORY.items():
        sources.append({'source':key,**value,'availability':'temporal_selection_unavailable',
            'date_selection':False,'client_version':'9.8.5','requires_desktop':True,
            'reason':'The documented desktop WebSocket interface has no date enumeration/selection fields.',
            'upgrade_note':'世纪空间2025多时相在10.5.5/10.5.6加入；升级是否恢复当前区域仍需实测。' if key=='century' else None})
    sources += [{'source':key,'name':name,'availability':'public_catalog',
        'time_semantics':'satellite_acquisition_time','date_selection':True,
        'requires_account_for_original_files':key.startswith('hls')}
        for key,name in [('sentinel2','Sentinel-2 L2A'),('landsat','Landsat C2 L2'),
                         ('hls_s30','NASA HLS S30'),('hls_l30','NASA HLS L30')]]
    return {'sources':sources,'note':'时序中相同底图的两个发布版本，未必是两次不同拍摄。'}

@router.post('/search')
async def history_search(body:HistorySearchRequest):
    if body.source in DESKTOP_HISTORY:
        raise unavailable(body.source)
    if body.source=='wayback':
        from .wayback import WaybackClient, WaybackError
        try:
            result=await WaybackClient().search(body.start_date.isoformat(),body.end_date.isoformat(),body.limit)
        except WaybackError as exc:
            raise HTTPException(exc.status_code,detail={'code':'history_provider_unavailable','message':str(exc)}) from None
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(502,detail={'code':'history_provider_unavailable','message':'Wayback provider could not complete the query.'}) from None
        result['spatial_filter_applied']=False
        result['requested_bbox']=body.bbox
        return result
    from .spectral import SpectralSearchRequest, search_spectral
    return await search_spectral(SpectralSearchRequest(source=body.source,bbox=body.bbox,
        datetime=f'{body.start_date.isoformat()}T00:00:00Z/{body.end_date.isoformat()}T23:59:59.999999Z',
        limit=body.limit,max_cloud_cover=body.max_cloud_cover))

@router.post('/capture')
async def history_capture(body:HistoryCaptureRequest,request:Request):
    if body.source in DESKTOP_HISTORY:
        raise unavailable(body.source)
    from .wayback import WaybackClient, WaybackError
    try:
        result=await WaybackClient(timeout=body.timeout_seconds).capture(list(body.normalized_bbox()),body.release_id,
            width=body.width,height=body.height,zoom=body.zoom,timeout_seconds=body.timeout_seconds)
    except WaybackError as exc:
        raise HTTPException(exc.status_code,detail={'code':'history_capture_failed','message':str(exc)}) from None
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(502,detail={'code':'history_capture_failed','message':'Cannot retrieve the selected historical mosaic.'}) from None
    return await request.app.state.store_external_image_function(result)
