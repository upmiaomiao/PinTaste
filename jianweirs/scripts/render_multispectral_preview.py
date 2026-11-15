"""Render a saved Sentinel-2 science window without creating new image detail."""
import hashlib
import json
import argparse
from pathlib import Path

import numpy as np
from PIL import Image,ImageDraw,ImageFont
import rasterio
from rasterio.warp import transform

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'outputs/portal-smoke/engine/runs/0c3c045d728f44d2bc34c7826e6ce5bb/science-sentinel2.tif'
OUT=ROOT/'outputs/imagery-preview-2026'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=SOURCE)
    args=parser.parse_args()
    source=args.input
    OUT.mkdir(exist_ok=True,parents=True)
    with rasterio.open(source) as ds:
        values=ds.read().astype('float64');valid=ds.dataset_mask()>0
        names=list(ds.descriptions);tags=ds.tags()
        assert names==['B04','B03','B02']
        assert tags['acquired_at'].startswith('2026-')
        acquired_date=tags['acquired_at'][:10]
        sensor={'S2A':'Sentinel-2A','S2B':'Sentinel-2B','S2C':'Sentinel-2C'}.get(tags['scene_id'][:3],'Sentinel-2')
        stem='S2-'+acquired_date.replace('-','')+'-RGB'
        rgb=np.zeros((ds.height,ds.width,3),dtype='uint8');limits=[]
        for band in range(3):
            mask=valid&np.isfinite(values[band]);lo,hi=np.percentile(values[band][mask],[2,98]);hi=max(hi,lo+1)
            rgb[:,:,band]=np.clip((values[band]-lo)/(hi-lo)*255,0,255).astype('uint8')
            limits.append([float(lo),float(hi)])
        extent=[ds.bounds.left,ds.bounds.right,ds.bounds.bottom,ds.bounds.top]
        east,north=transform('EPSG:4326',ds.crs,[104.461117],[30.858473])
        metadata={'source_file':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'source':sensor+' L2A','scene_id':tags['scene_id'],'acquired_at':tags['acquired_at'],
            'rgb_bands':names,'native_band_resolution_m':10,'output_raster_dimensions':[ds.width,ds.height],
            'output_grid_spacing_m':list(ds.res),'crs':str(ds.crs),'raster_bounds':list(ds.bounds),
            'valid_fraction':float(valid.mean()),'display':'per-band 2nd–98th percentile linear stretch',
            'display_limits_raw_dn':limits,'reflectance_scale_applied':False,
            'resampling_for_display':'nearest, no enhancement','marker':'requested center, input coordinate reference remains unverified'}
    rgb[~valid]=255
    Image.fromarray(rgb).save(OUT/(stem+'-native.png'))
    canvas=Image.new('RGB',(1000,1170),'white');draw=ImageDraw.Draw(canvas)
    font_path='C:/Windows/Fonts/msyh.ttc'
    title=ImageFont.truetype(font_path,34);body=ImageFont.truetype(font_path,25);small=ImageFont.truetype(font_path,23)
    draw.text((50,24),sensor+' 多光谱 · 自然彩色',font=title,fill='black')
    draw.text((50,78),acquired_date+'｜红/绿/蓝 = B04/B03/B02',font=body,fill='black')
    canvas.paste(Image.fromarray(rgb).resize((900,900),Image.Resampling.NEAREST),(50,140))
    cx=50+int((east[0]-extent[0])/(extent[1]-extent[0])*900)
    cy=140+int((extent[3]-north[0])/(extent[3]-extent[2])*900)
    for width,color in ((7,'white'),(3,'black')):
        draw.line((cx-17,cy,cx+17,cy),fill=color,width=width);draw.line((cx,cy-17,cx,cy+17),fill=color,width=width)
    sx,sy=100,990;bar=round(200/(extent[1]-extent[0])*900)
    draw.rectangle((sx-12,sy-45,sx+bar+12,sy+14),fill='white')
    draw.line((sx,sy,sx+bar,sy),fill='black',width=5)
    draw.text((sx+bar/2,sy-40),'200 m',font=body,fill='black',anchor='mt')
    draw.rectangle((866,160,930,260),fill='white');draw.text((898,166),'N',font=body,fill='black',anchor='mt')
    draw.line((898,246,898,205),fill='black',width=4);draw.polygon([(898,201),(889,217),(907,217)],fill='black')
    draw.text((50,1060),'原生波段分辨率 10 m｜约 800 m × 800 m｜十字为请求中心',font=small,fill='black')
    draw.text((50,1110),'2%–98% 显示拉伸；未做 AI 增强，放大不增加卫星细节',font=small,fill='#444')
    path=OUT/(stem+'.png');canvas.save(path)
    metadata['preview_file']=str(path)
    (OUT/(stem+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'preview':str(path),'date':metadata['acquired_at'],'native_resolution_m':10,'valid_fraction':metadata['valid_fraction']},ensure_ascii=False))


if __name__=='__main__':main()
