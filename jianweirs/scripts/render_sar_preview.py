"""Render the explicitly selected, existing SAR crop without remote downloads."""
from pathlib import Path
import hashlib
import json

import numpy as np
from PIL import Image,ImageDraw,ImageFont
import rasterio
from rasterio.warp import transform_bounds


ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'outputs/portal-smoke/engine/runs/0c3c045d728f44d2bc34c7826e6ce5bb'
OUT=ROOT/'outputs/imagery-preview-2026'


def stats(values):
    percentiles=[0,1,2,25,50,75,98,99,100]
    return {'count':int(values.size),'minimum':float(values.min()),'maximum':float(values.max()),
            'mean':float(values.mean()),'standard_deviation':float(values.std()),
            'percentiles':{str(p):float(v) for p,v in zip(percentiles,np.percentile(values,percentiles))}}


def main():
    OUT.mkdir(exist_ok=True)
    package=json.loads((RUN/'finding-package.json').read_text(encoding='utf-8'))
    assert package['task']['center']==[104.461117,30.858473]
    science=next(item for item in package['scientific_evidence'] if item['source']=='sentinel1-rtc')
    subset=science['subset'];source=RUN/subset['artifact'];raw_bytes=source.read_bytes()
    digest=hashlib.sha256(raw_bytes).hexdigest();assert digest==subset['sha256']
    scene=json.loads((OUT/'S1-20261013-scene-metadata.json').read_text(encoding='utf-8'))
    assert scene['id']==subset['scene_id']
    with rasterio.open(source) as raster:
        assert raster.count==1 and raster.descriptions[0]=='VV'
        data=raster.read(1,masked=True).astype('float64')
        valid=~np.ma.getmaskarray(data)&np.isfinite(data.data)
        tags=raster.tags();band_tags=raster.tags(1)
        units=band_tags.get('units','')
        linear_power='linear' in units.lower() and 'power' in units.lower()
        display_valid=valid&(data.data>0) if linear_power else valid
        values=data.data[display_valid]
        transformed=np.full(data.shape,np.nan,dtype='float64')
        transformed[display_valid]=10*np.log10(values) if linear_power else values
        display_values=transformed[display_valid]
        low,high=np.percentile(display_values,[2,98]);assert high>low
        clipped=np.clip((transformed-low)/(high-low),0,1)
        gray=np.zeros(data.shape,dtype='uint8');gray[display_valid]=np.rint(clipped[display_valid]*255).astype('uint8')
        native=OUT/'S1-20261013-VV-native80.png';Image.fromarray(gray).save(native)
        bbox=list(raster.bounds);bbox_wgs84=list(transform_bounds(raster.crs,'EPSG:4326',*raster.bounds,densify_pts=21))
        width_m=raster.width*abs(raster.transform.a);height_m=raster.height*abs(raster.transform.e)
        metadata={'source_tiff':str(source.resolve()),'source_sha256':digest,'source_bytes':len(raw_bytes),
            'scene':scene,'source_file_tags':tags,'band_tags':band_tags,
            'native_crop':{'dimensions':[raster.width,raster.height],'crs':str(raster.crs),'bounds_crs':bbox,'bounds_wgs84':bbox_wgs84,
                'transform':list(raster.transform)[:6],'extent_metres':[width_m,height_m],
                'pixel_spacing_metres':list(raster.res),'source_grd_resolution_metres':scene.get('source_resolution_m'),
                'source_scale':raster.scales[0],'source_offset':raster.offsets[0],'polarization':'VV',
                'valid_pixels':int(valid.sum()),'total_pixels':int(data.size),'valid_fraction':float(valid.mean()),
                'nonpositive_values_excluded_from_db':int((valid&(data.data<=0)).sum()) if linear_power else 0},
            'display':{'unit':'gamma0 dB' if linear_power else units or 'raw unit unknown',
                'conversion':'10*log10(linear gamma0 power)' if linear_power else 'none; raw-value stretch',
                'units_verified_from':'GeoTIFF band units and radiometry tags',
                'stretch':'2nd–98th percentile, linear grayscale after unit conversion',
                'lower_bound':float(low),'upper_bound':float(high),'below_lower_bound':int((display_values<low).sum()),
                'above_upper_bound':int((display_values>high).sum()),'filtering':'none','speckle_preserved':True,
                'interpolation':'nearest for display only','gamma':1.0,'raw_native_png':str(native.resolve())},
            'linear_gamma0_power_statistics':stats(data.data[valid]),
            'display_unit_statistics':stats(display_values),
            'coordinate_caveat':'原任务坐标系未声明；API按WGS84查询，未独立验证该输入点位的坐标基准。',
            'interpretation_boundary':'灰度只显示后向散射强弱，未识别物料、固废、变化或环境风险；10米网格不等于10米实际分辨率。',
            'remote_downloads_for_this_preview':0,'model_calls':0}
    font_path='C:/Windows/Fonts/msyh.ttc'
    title=ImageFont.truetype(font_path,38);font=ImageFont.truetype(font_path,26)
    canvas=Image.new('RGB',(1160,1200),'white');draw=ImageDraw.Draw(canvas)
    draw.text((140,25),'Sentinel-1D｜2026-10-13｜VV',font=title,fill='black')
    draw.text((140,82),'RTC gamma0：10log10(线性功率)转dB' if linear_power else '单位未确认，采用原始数值显示',font=font,fill='black')
    draw.text((140,124),f'P2–P98灰度拉伸：{low:.2f} 至 {high:.2f} '+('dB' if linear_power else ''),font=font,fill='black')
    x,y,size=140,210,720
    canvas.paste(Image.fromarray(gray).resize((size,size),Image.Resampling.NEAREST),(x,y))
    draw.rectangle((x,y,x+size,y+size),outline='black',width=2)
    for value in range(0,801,200):
        tx=x+round(value/width_m*size);ty=y+size-round(value/height_m*size)
        draw.line((tx,y+size,tx,y+size+8),fill='black',width=2)
        draw.text((tx,y+size+13),str(value),font=font,fill='black',anchor='ma')
        draw.line((x-8,ty,x,ty),fill='black',width=2)
        draw.text((x-15,ty),str(value),font=font,fill='black',anchor='rm')
    draw.text((20,164),'UTM北向（米）',font=font,fill='black')
    draw.text((x+size/2,y+size+57),'UTM东向（米，自裁图西缘）',font=font,fill='black',anchor='ma')
    gradient=np.repeat(np.rint(np.linspace(255,0,size)).astype('uint8')[:,None],40,axis=1)
    bx=940;canvas.paste(Image.fromarray(gradient).convert('RGB'),(bx,y))
    draw.rectangle((bx,y,bx+40,y+size),outline='black',width=2)
    draw.text((bx+20,y-45),'dB' if linear_power else '原值',font=font,fill='black',anchor='ma')
    for fraction in np.linspace(0,1,5):
        ty=y+size-round(fraction*size);value=low+fraction*(high-low)
        draw.line((bx+40,ty,bx+48,ty),fill='black',width=2)
        draw.text((bx+57,ty),f'{value:.2f}',font=font,fill='black',anchor='lm')
    draw.text((70,1040),'80 × 80 原始像元；10米网格；源GRD分辨率约20 × 22米。',font=font,fill='black')
    draw.text((70,1083),'升轨，轨道55；800 × 800米窗口；显示放大不增加空间细节。',font=font,fill='black')
    draw.text((70,1126),'无滤波，保留原始斑点；亮暗仅表示后向散射强弱。',font=font,fill='black')
    image=OUT/'S1-20261013-VV.png';canvas.save(image)
    metadata['display']['preview_png']=str(image.resolve())
    (OUT/'S1-20261013-VV.metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'preview':str(image.resolve()),'metadata':str((OUT/'S1-20261013-VV.metadata.json').resolve()),
                      'valid_pixels':metadata['native_crop']['valid_pixels'],'stretch_bounds':[float(low),float(high)],
                      'db_statistics':metadata['display_unit_statistics']},ensure_ascii=True))


if __name__=='__main__':main()
