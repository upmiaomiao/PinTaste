"""Reproduce the local v1 coordinate audit using already captured PNGs only.

No network calls, no inference models, no modification of production artifacts.
The code0/code1 live captures and original metadata remain in coordinate-v1.
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from omap_api.app import create_app
from omap_api.optical_coordinates import coordinate_contract,unknown_registration
from omap_api.image_quality import validate_optical_preview


def main():
    folder=ROOT/'outputs/coordinate-v1';work=folder/'code1-worker-artifacts';work.mkdir(parents=True,exist_ok=True)
    original=json.loads((folder/'registration-live-results.json').read_text(encoding='utf-8'))
    old={r['source']:r for r in original['sources']}
    source_specs=json.loads((folder/'sources-before.json').read_text(encoding='utf-8'))['sources']
    source_specs={r['source']:r for r in source_specs}
    ref=old['arcgis'];reference_id=ref['image_id'];reference=Path(ref['file']).read_bytes();ref_meta=ref['raw_metadata']
    (work/f'{reference_id}.png').write_bytes(reference)
    (work/f'{reference_id}.json').write_text(json.dumps(ref_meta,ensure_ascii=False,indent=2),encoding='utf-8')
    client=TestClient(create_app(output_dir=work))
    rows=[]
    for source in ['bing','amap','tencent','google']:
        cached=json.loads((folder/f'{source}-code1-response.json').read_text(encoding='utf-8'))
        response=cached['response'];identifier=response['image_id'];raw=(folder/f'{source}-code1-original.png').read_bytes()
        meta=response['metadata'].copy();spec=source_specs[source]
        meta.update(source_key=source,coordinate_system='EPSG:4326',coordinate_system_verified=False,
                    coordinate_contract=coordinate_contract(source,spec['map_type'],spec['map_metadata'],cached['request']['bbox'],runtime_profile='wgs84_code1'),
                    spatial_registration=unknown_registration())
        (work/f'{identifier}.png').write_bytes(raw)
        (work/f'{identifier}.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        raw_result=client.post('/api/optical/register',json={'source_image_id':identifier,'reference_image_id':reference_id}).json()
        corrected=client.post('/api/optical/register',json={'source_image_id':identifier,'reference_image_id':reference_id,'correct':True})
        corrected.raise_for_status();aligned=corrected.json()
        row={'source':source,'provider_map_type':spec['map_type'],'http_status':cached['http_status'],
             'raw_file':str((folder/f'{source}-code1-original.png').resolve()),
             'raw_sha256':hashlib.sha256(raw).hexdigest(),'raw_quality':validate_optical_preview(raw),
             'raw_spatial_registration':raw_result['metadata']['spatial_registration'],
             'corrected_file':str((work/(aligned['image_id']+'.png')).resolve()),
             'corrected_response':aligned,
             'code0_center_displacement_pixels':old[source]['audit']['raster_center_displacement_reference_pixels'],
             'acquisition_time':None,'native_resolution_m':None}
        rows.append(row)
    reference_status=client.get('/api/optical/registration/'+reference_id).json()['spatial_registration']
    evidence={'schema_version':'1.0','point':[104.461117,30.858473],
              'bbox_wgs84':original['bbox_wgs84'],
              'profile':'wgs84_code1','profile_scope':'explicit_local_runtime_only_not_vendor_global_default',
              'request_contract':'Caller input_crs EPSG:4326 and coord_type 0 -> native desktop CoordType 1; same numerical WGS84 bbox, no fixed pixel shift.',
              'official_manual_profile':'documented code0 CGCS2000 / code1 GCJ-02; runtime discrepancy explicitly retained',
              'evidence':{'same_view_coordinate_pair':str(folder/'omap-current-coordinate-pair.json'),
                          'code0_live_audit':str(folder/'registration-live-results.json'),
                          'reference_sha256':ref['sha256'],'reference_image_id':reference_id},
              'reference_spatial_registration':reference_status,
              'sources':rows,
              'limitations':['relative registration, not absolute surveyed accuracy',
                             'basemap date/native GSD not established for these four whole images',
                             'different provider names do not prove independent satellites',
                             'corrected alpha=0 has no imagery coverage, never negative evidence',
                             'code0 captures locally warped to 800m frame had only about46% coverage; do not use as full coverage',
                             'No production process restart during this offline audit']}
    (folder/'coordinate-profile-audit.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',25)
    small=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',21)
    canvas=Image.new('RGB',(1700,1000),'white');draw=ImageDraw.Draw(canvas)
    draw.text((15,8),'本机坐标profile核验｜同点800m：原生code1后，逐图局部注册',font=font,fill='black')
    draw.text((15,45),'上：code1原图；下：配准到Esri实际范围。灰底为透明无覆盖；不是地图日期或物料鉴定。',font=small,fill='black')
    for i,row in enumerate(rows):
        x=15+i*420
        a=row['raw_spatial_registration'];b=row['corrected_response']['metadata']['spatial_registration']
        draw.text((x,82),row['source']+f" | {row['provider_map_type']}",font=font,fill='black')
        for y,path in [(122,row['raw_file']),(562,row['corrected_file'])]:
            with Image.open(path) as im:
                rgba=im.convert('RGBA');background=Image.new('RGBA',rgba.size,(210,210,210,255));background.alpha_composite(rgba)
                tile=background.convert('RGB').resize((390,390),Image.Resampling.NEAREST)
            td=ImageDraw.Draw(tile);td.line((184,195,206,195),fill='red',width=2);td.line((195,184,195,206),fill='red',width=2)
            canvas.paste(tile,(x,y))
        draw.text((x,516),f"中心偏差 {a['raster_center_displacement_reference_pixels']:.1f}px / {a['unique_inlier_count']}点",font=small,fill='black')
        draw.text((x,956),f"校正有效覆盖 {b['valid_coverage_fraction']:.1%}",font=small,fill='black')
    canvas.save(folder/'coordinate-profile-comparison.png')
    table=[]
    for r in rows:
        a=r['raw_spatial_registration'];b=r['corrected_response']['metadata']['spatial_registration']
        table.append('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in [r['source'],r['provider_map_type'],
            f"{r['code0_center_displacement_pixels']:.1f}px",f"{a['raster_center_displacement_reference_pixels']:.1f}px",
            a['unique_inlier_count'],f"{a['rms_pixels']:.2f}px",a['status'],f"{b['valid_coverage_fraction']:.1%}",'未核实'])+'</tr>')
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本机光学坐标核验</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;max-width:1450px;margin:32px auto;line-height:1.7;color:#18212c}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bdc8d2;padding:8px}th{background:#eef4f8}img{max-width:100%}p{max-width:1150px}</style>
<h1>本机高分影像坐标核验 v1</h1><p>本次点位104.461117,30.858473，相同800米范围。原生code0出现约360米位移；经code1对照、共同地标与逐图注册，中心偏差降至约4–6个显示像素。只在本机显式启用wgs84_code1，保留与官方手册不一致的诊断；不把局部偏移变成全球常量。</p>
<p>调用者仍传WGS84（input_crs=EPSG:4326、coord_type=0），由本机profile转换为原生code1。Esri以服务实际export范围为参考，跨图控制点仅支持本AOI的相对空间对应，不证明绝对测绘精度、独立卫星来源或整图拍摄日期。</p>
<table><tr><th>来源</th><th>MapType</th><th>旧code0中心偏差</th><th>code1中心偏差</th><th>分散内点</th><th>拟合RMS</th><th>原图相对状态</th><th>局部校正后有效覆盖</th><th>整图日期/原生GSD</th></tr>'''+''.join(table)+'''</table>
<p>Google已有命名API与本机MapType317；本次取到可解码且通过占位图检查的真实场景，并通过局部控制点验证。不是仅凭HTTP200判断可用。</p>
<img src="coordinate-profile-comparison.png" alt="本机四来源原图及局部校正图"><p>灰色区域代表透明缺覆盖；只能对有效范围解释。旧code0图靠局部warp只剩约46%覆盖，不能用来判断整框无目标。新图仍不能替代人工核查或鉴定。原图和校正图分别保留，JSON记录哈希、变换、参数与范围。</p></html>'''
    (folder/'coordinate-profile-audit.html').write_text(document,encoding='utf-8')
    print(json.dumps({'report':str(folder/'coordinate-profile-audit.html'),
        'sources':[{'source':r['source'],'raw':r['raw_spatial_registration']['status'],
                    'coverage':r['corrected_response']['metadata']['spatial_registration']['valid_coverage_fraction']} for r in rows]},ensure_ascii=True))


if __name__=='__main__':main()
