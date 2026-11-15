"""Native Landsat C2 windows with explicit STAC calibration and QA_PIXEL.

QA definition: https://www.usgs.gov/landsat-missions/landsat-collection-2-quality-assessment-bands
No fixed scale/offset or spacecraft identity is invented from a collection name.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math

from .data import DataClientError


def landsat_surface_mask(qa):
    # C2 QA_PIXEL: fill, dilated cloud, cirrus (8/9), cloud, shadow, snow.
    return (qa.astype('uint32') & 0b111111) == 0


def explicit_band_calibration(asset):
    bands = asset.get('raster_bands') or []
    if not bands or not all(key in bands[0] for key in ('scale', 'offset')):
        return None
    scale, offset = bands[0]['scale'], bands[0]['offset']
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (scale, offset)) or scale <= 0:
        return None
    return {'scale': scale, 'offset': offset, 'source': 'actual_STAC_raster_bands'}


def landsat_target_quality(good, affine, crs, task, bbox):
    import numpy as np
    from rasterio.warp import transform
    x, y = transform('EPSG:4326', crs, [(bbox[0] + bbox[2]) / 2], [(bbox[1] + bbox[3]) / 2])
    rr, cc = np.indices(good.shape)
    xs, ys = affine.c + (cc + .5) * affine.a, affine.f + (rr + .5) * affine.e
    radius = math.sqrt(task.site_area_m2 / math.pi) if task.site_area_m2 else 60.
    if task.bbox and not task.site_area_m2:
        lon, lat = transform(crs, 'EPSG:4326', xs.ravel().tolist(), ys.ravel().tolist())
        lon, lat = np.asarray(lon).reshape(good.shape), np.asarray(lat).reshape(good.shape)
        target = (lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])
        scope = 'requested_task_bbox'
    else:
        target = (xs - x[0]) ** 2 + (ys - y[0]) ** 2 <= radius ** 2
        scope = 'declared_site_area_circle_approximation' if task.site_area_m2 else 'task_center_60m_not_target_boundary'
    inv = ~affine
    col, row = math.floor(inv.a * x[0] + inv.b * y[0] + inv.c), math.floor(inv.d * x[0] + inv.e * y[0] + inv.f)
    center_clear = bool(good[row, col]) if 0 <= row < good.shape[0] and 0 <= col < good.shape[1] else False
    fraction = float(good[target].mean()) if target.any() else 0.
    return {'status': 'screened_usable' if center_clear and fraction >= .8 else 'obscured_or_unusable',
        'usable': center_clear and fraction >= .8, 'center_clear': center_clear,
        'target_radius_m': None if task.bbox and not task.site_area_m2 else radius,
        'target_quality_scope': scope, 'target_surface_fraction': fraction,
        'whole_window_valid_fraction': float(good.mean()),
        'mask': 'actual_C2_QA_PIXEL_bits_0_to_5_clear_and_RGB_nonzero', 'cloud_free_proven': False}


async def acquire_landsat_window(pipeline, run_id, task, scene, bbox):
    from .usage_context import imagery_reservation
    async with imagery_reservation('landsat:' + str(run_id) + ':' + str(scene.get('id') or scene.get('scene_id')) + ':' + json.dumps(list(bbox))) as reservation:
        result = await _acquire_landsat_window(pipeline, run_id, task, scene, bbox)
        if result[0] is None:
            reservation.cancel()
        return result


async def _acquire_landsat_window(pipeline, run_id, task, scene, bbox):
    import numpy as np
    import rasterio
    from rasterio.warp import transform
    from PIL import Image
    from .science import _native_crop, _register_preview

    identity = scene['id']
    root = pipeline.store.run_dir(run_id)
    folder = root / ('science-landsat-' + hashlib.sha256(identity.encode()).hexdigest()[:12])
    folder.mkdir(exist_ok=True)
    checkpoint = folder / 'metadata.json'
    native = {}
    if checkpoint.exists():
        old = json.loads(checkpoint.read_text(encoding='utf-8'))
        if old.get('scene_id') == identity and old.get('bbox') == list(bbox):
            native = old.get('assets', {})
    for key in ('red', 'green', 'blue', 'qa_pixel', 'nir08', 'qa_radsat'):
        path = folder / (key + '.tif')
        old = native.get(key, {})
        if old.get('status') == 'completed' and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == old.get('sha256'):
            continue
        try:
            resolved = await pipeline.data.execute('multispectral.asset', {'source': 'landsat', 'scene_id': identity, 'asset_id': key})
            row = await asyncio.to_thread(_native_crop, resolved['href'], list(bbox), path, 1, 30)
            row['calibration'] = explicit_band_calibration(resolved)
        except (DataClientError, ValueError, OSError, KeyError) as error:
            row = {'status': 'unavailable', 'reason': error.code if isinstance(error, DataClientError) else type(error).__name__}
        native[key] = row
        checkpoint.write_text(json.dumps({'scene_id': identity, 'bbox': list(bbox), 'assets': native}, ensure_ascii=False, indent=2), encoding='utf-8')
    evidence = {'source': 'landsat', 'scene_id': identity, 'scene_acquired_at': scene.get('acquired_at'),
        'platform': scene.get('platform'), 'bbox': list(bbox), 'operation': 'multispectral.asset',
        'status': 'unavailable', 'native': {'assets': native}, 'bands': ['red', 'green', 'blue', 'nir08', 'qa_pixel', 'qa_radsat'],
        'numeric_material_classification': False, 'limitation': '30米地表背景证据；混合像元及农事变化不能直接认定为堆存变化'}
    if any(native.get(key, {}).get('status') != 'completed' for key in ('red', 'green', 'blue', 'qa_pixel')):
        return None, {**evidence, 'reason': 'missing_native_RGB_or_QA_PIXEL'}
    values, grid = {}, None
    for key in ('red', 'green', 'blue', 'qa_pixel', 'nir08', 'qa_radsat'):
        if native.get(key, {}).get('status') != 'completed':
            continue
        with rasterio.open(folder / (key + '.tif')) as src:
            current = (src.crs, src.transform, src.shape)
            if grid is not None and current != grid:
                return None, {**evidence, 'reason': 'native_Landsat_band_grids_differ'}
            grid = current
            values[key] = src.read(1)
    good = landsat_surface_mask(values['qa_pixel'])
    for key in ('red', 'green', 'blue'):
        good &= values[key] > 0
    crs, affine, shape = grid
    quality = landsat_target_quality(good, affine, crs, task, bbox)
    evidence['quality_screening'] = quality
    if not quality['usable']:
        return None, {**evidence, 'status': 'quality_rejected', 'reason': 'local_QA_PIXEL_cloud_shadow_fill_or_snow'}
    calibrated = all(native[key].get('calibration') for key in ('red', 'green', 'blue'))
    rgb = []
    for key in ('red', 'green', 'blue'):
        raw = values[key].astype('float64')
        if calibrated:
            cal = native[key]['calibration']
            stretched = (raw * cal['scale'] + cal['offset']) / .4
        else:
            low, high = np.percentile(raw[good], [2, 98])
            stretched = (raw - low) / max(1, high - low)
        channel = (np.clip(stretched, 0, 1) * 255).astype('uint8')
        channel[~good] = 180
        rgb.append(channel)
    ndvi = {'status': 'unknown', 'reason': 'missing_actual_red_nir_scale_offset', 'material_classification_supported': False}
    if 'nir08' in values and 'qa_radsat' in values and all(native[key].get('calibration') for key in ('red', 'nir08')):
        rcal, ncal = native['red']['calibration'], native['nir08']['calibration']
        red = values['red'] * rcal['scale'] + rcal['offset']
        nir = values['nir08'] * ncal['scale'] + ncal['offset']
        mask = good & (values['qa_radsat'] == 0) & (values['nir08'] > 0) & (abs(nir + red) > 1e-8)
        index = (nir[mask] - red[mask]) / (nir[mask] + red[mask])
        if index.size:
            ndvi = {'status': 'computed', 'formula': '(calibrated_nir08-calibrated_red)/(calibrated_nir08+calibrated_red)',
                'p05_median_p95': [float(v) for v in np.percentile(index, [5, 50, 95])], 'pixel_count': int(index.size),
                'scope': 'whole_requested_ROI_context_not_delineated_waste_target', 'material_classification_supported': False,
                'saturation_mask': 'actual_QA_RADSAT_zero'}
    elif 'qa_radsat' not in values:
        ndvi['reason'] = 'missing_QA_RADSAT_for_quantitative_index'
    artifact = str((folder / 'red.tif').relative_to(root)).replace('\\', '/')
    evidence.update(status='science_window_downloaded', artifact=artifact, ndvi=ndvi,
        calibration={'status': 'verified' if calibrated else 'unknown',
            'source': 'actual_STAC_raster_bands' if calibrated else None, 'offset_assumed': False},
        dimensions=[shape[1], shape[0]], crs=str(crs), valid_fraction=float(good.mean()),
        full_task_aoi_covered=all(native[key].get('full_task_aoi_covered') for key in ('red', 'green', 'blue', 'qa_pixel')))
    asset = _register_preview(pipeline, Image.fromarray(np.stack(rgb, axis=-1)), 'landsat', scene,
        modality='multispectral', resolution=30, display='actual_reflectance_fixed_0_to_0.4' if calibrated else 'DN_percentile_display_only_not_reflectance', science_artifact=artifact)
    asset['source_bands'] = evidence['bands']
    pipeline.store.put('image', asset)
    evidence['preview_asset_id'] = asset['id']
    return asset, evidence
