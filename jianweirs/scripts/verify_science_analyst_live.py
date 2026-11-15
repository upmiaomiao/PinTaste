"""Explicit live DMX image-input acceptance with saved S2/SAR evidence."""
import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
import httpx
from PIL import Image
import numpy as np
import rasterio
from wuyu_harness.config import Settings
from wuyu_harness.models import ModelClient,AUDIT_DIR
from wuyu_harness.schemas import TaskRequest,ImageMetadata,UploadRequest
from wuyu_harness.science import analyze_science,calibrated_sar_preview
from wuyu_harness.store import Store


async def main():
    load_dotenv(ROOT/'.env.wuyu');out=ROOT/'outputs/adaptive-v1-evaluation'
    out.mkdir(exist_ok=True);audit=out/'model-calls';audit.mkdir(exist_ok=True)
    settings=replace(Settings.from_env(),output_dir=out/'engine');store=Store(settings.output_dir)
    def register(im,name,modality,source,date,resolution):
        mem=io.BytesIO();im.save(mem,'PNG')
        item=store.upload(UploadRequest(filename=name,content_base64=base64.b64encode(mem.getvalue()).decode(),
            metadata=ImageMetadata(modality=modality,source=source,acquisition_time=date,resolution_m=resolution,role='sar' if modality=='sar' else 'target_crop')))
        asset=store.get(item['id'],'image');asset.update(science_preview=True,analysis_scope='scientific_context');store.put('image',asset);return asset
    with rasterio.open(ROOT/'outputs/march-2026-check/S2-20260311-RGB.tif') as src:
        raw=src.read();rgb=np.stack([np.uint8(np.clip((b-np.percentile(b,2))/(np.percentile(b,98)-np.percentile(b,2))*255,0,255)) for b in raw],axis=-1)
    optical=register(Image.fromarray(rgb).resize((512,512),Image.Resampling.NEAREST),'S2-science-check.png','multispectral','sentinel2','2026-03-11T03:35:51.025Z',10)
    sar_path=ROOT/'outputs/imagery-preview-2026/S1-20261013-VV.tif'
    im,calibration=calibrated_sar_preview(sar_path,{'source_resolution_m':[20,22],'pixel_spacing_m':[10,10]})
    radar=register(im.resize((512,512),Image.Resampling.NEAREST),'SAR-science-check.png','sar','sentinel1-rtc','2026-10-13T11:00:19.943511Z',None)
    task=TaskRequest(objective='核查这些科学影像能对固废堆存提供哪些辅助证据，分别说明可见线索和无法确定的事项。两期不同日期不能当作同一时间，不据颜色或散射判材质。',task_types=['suspected_site_discovery'],center=[104.461117,30.858473],coordinate_system='wgs84')
    token=AUDIT_DIR.set(audit)
    try:
        async with httpx.AsyncClient(timeout=180,follow_redirects=False) as client:
            pipeline=SimpleNamespace(settings=settings,store=store,models=ModelClient(client))
            async def run(label,asset,evidence):
                traces=[];result=await analyze_science(pipeline,task,[asset],evidence,traces)
                (out/(label+'-gemini-result.json')).write_text(json.dumps({'analysis':result,'model_calls':traces},ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'label':label,'model':settings.models['science_analyst'].model,'status':result['status'],'image_input_count':sum(t.get('image_input_count',0) for t in traces),'observations':len(result.get('observations',[]))},ensure_ascii=False),flush=True)
            await asyncio.gather(run('s2',optical,{'source':'sentinel2','scene_acquired_at':'2026-03-11T03:35:51.025Z','limitation':'RGB DN仅作2%-98%显示拉伸；本次输入未包含NIR/SWIR或质量mask，不计算指数或断言无云。'}),
                                 run('sar',radar,{'source':'sentinel1-rtc','scene_acquired_at':'2026-10-13T11:00:19.943511Z','calibration':calibration,'limitation':'仅VV单期，无同轨前期与精确目标边界，不能推断变化或材质。'}))
    finally:AUDIT_DIR.reset(token)


if __name__=='__main__':asyncio.run(main())
