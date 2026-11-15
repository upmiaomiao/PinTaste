"""Loopback-only discovery, delivery probes and conservative runtime calibration.

No provider URL or credentials leave this process. Successful delivery is not a
claim about survey accuracy, satellite identity, freshness or commercial rights.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from PIL import Image
from dotenv import dotenv_values
from omap_api.image_quality import validate_optical_preview
from omap_api.registration import audit_images
from scripts.deploy.configure_sources import SOURCES, _local_origin, discover_sources, fetch_maps

# Same urban/rural AOI for every source and coordinate code; about 800 m wide.
BBOX = [104.45693120048739, 30.854879755300036, 104.46530279951261, 30.862066244699964]
AUDIT_FIELDS = ('status', 'reason', 'strict_same_point_eligible', 'rms_pixels',
                'unique_inlier_count', 'raster_center_displacement_reference_pixels',
                'reference_display_sampling_m', 'scale_factor', 'rotation_degrees',
                'transform_source_to_reference', 'relative_fit_uncertainty_m')


class ProbeError(Exception):
    def __init__(self, code, status=None):
        self.code, self.status = code, status


class LocalProbe:
    def __init__(self, origin, key, folder, *, transport=None):
        self.folder = folder
        self.client = httpx.Client(base_url=_local_origin(origin), trust_env=False,
            follow_redirects=False, timeout=100, transport=transport,
            headers={'Authorization': 'Bearer '+key})

    def request(self, method, path, body=None, *, binary=False):
        if not path.startswith('/api/') or '//' in path or '?' in path or '..' in path:
            raise ProbeError('unsafe_local_path')
        try:
            with self.client.stream(method, path, json=body) as response:
                if response.status_code != 200:
                    # Never reflect provider messages or exception strings.
                    code = 'worker_http_error'
                    raw = response.read()
                    try:
                        candidate = json.loads(raw).get('detail', {}).get('code', '')
                        if candidate in {'omap_permission_denied', 'omap_timeout', 'omap_capture_failed',
                            'omap_incomplete_image', 'unknown_map', 'invalid_zoom', 'omap_unavailable',
                            'omap_auth_failed', 'optical_provider_error'}:
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                    raise ProbeError(code, response.status_code)
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > (32*1024*1024 if binary else 4*1024*1024):
                        raise ProbeError('local_response_too_large')
            return bytes(chunks) if binary else json.loads(chunks)
        except (httpx.HTTPError, ValueError):
            raise ProbeError('local_transport_or_json_error') from None

    def capture(self, source, map_id, code, zoom=17):
        body = {'bbox': BBOX, 'coord_type': code, 'zoom': zoom,
                'width': 768, 'height': 768, 'timeout_seconds': 75}
        if source == 'arcgis':
            body['source'] = source
            body['coord_type'] = 0
            endpoint = '/api/optical/latest'
        else:
            body['map_type'] = map_id
            endpoint = '/api/snapshot'
        result = self.request('POST', endpoint, body)
        image_id = result.get('image_id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', image_id):
            raise ProbeError('invalid_worker_image_id')
        image = self.request('GET', '/api/images/'+image_id+'.png', binary=True)
        with Image.open(io.BytesIO(image)) as decoded:
            if decoded.format != 'PNG' or decoded.size != (768, 768):
                raise ProbeError('unexpected_image_dimensions_or_format')
        quality = validate_optical_preview(image)
        if not quality.get('valid'):
            raise ProbeError('image_quality_rejected')
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f'{source}-{map_id or 0}-code{code}.png'
        target = self.folder/name
        if target.is_symlink():
            raise ProbeError('unsafe_artifact_path')
        target.write_bytes(image)
        return image, result.get('metadata', {}), name


def candidate_plan(maps, source_rows, current):
    _, discovered = discover_sources(maps)
    plan = {row['source']: [dict(c) for c in row['candidates'] if c['score'] > 0]
            for row in discovered['sources']}
    # Built-in MapTypes are supplied by this worker's versioned catalogue and
    # accepted only after a real capture. Do not substitute 20861 config IDs.
    for row in source_rows:
        source = row.get('source')
        meta = row.get('map_metadata')
        map_id = row.get('map_type')
        if source in {'tianditu', 'siwei', 'siwei_daily'} and row.get('configured') is True and isinstance(meta, dict):
            if (isinstance(map_id, int) and not isinstance(map_id, bool) and map_id > 0
                    and meta.get('id') == map_id and not plan[source]):
                plan[source] = [{'map_type': map_id, 'name': row['name'], 'score': 1,
                    'max_zoom': meta.get('max_zoom', 17),
                    'reason': 'builtin_worker_catalogue_requires_capture'}]
    for source, candidates in plan.items():
        # Preserve an explicit local choice when it still matches the product.
        candidates.sort(key=lambda c: (c['map_type'] != current.get(source), -c['score'], c['map_type']))
    return plan, discovered


def recommend_profile(rows, current_profile):
    votes = []
    for source, pair in rows.items():
        good = [code for code, audit in pair.items() if audit.get('status') == 'verified'
                and audit.get('strict_same_point_eligible') is True]
        if len(good) == 1:
            votes.append(good[0])
    # Two logical sources support a local runtime interpretation. They need not
    # be independent satellite acquisitions; this does not establish a datum.
    if len(votes) >= 2 and len(set(votes)) == 1:
        return ('wgs84_code1' if votes[0] == 1 else 'documented'), 'two_source_local_control_point_consensus'
    return current_profile, 'insufficient_consensus_preserved_existing_profile'


def expand(origin, config_path, output, *, transport=None):
    origin = _local_origin(origin)
    values = dotenv_values(config_path, encoding='utf-8-sig', interpolate=False)
    maps = fetch_maps(origin, config_path, transport=transport)
    current = json.loads(values.get('OMAP_SOURCE_MAP_TYPES_JSON') or '{}')
    current_profile = values.get('OMAP_OPTICAL_COORDINATE_PROFILE') or 'documented'
    code = 1 if current_profile == 'wgs84_code1' else 0
    probe = LocalProbe(origin, values['OMAP_WORKER_API_KEY'], output/'artifacts', transport=transport)
    report = {'schema_version': 1, 'tested_at': datetime.now(timezone.utc).isoformat(),
        'bbox_wgs84': BBOX, 'capture_verified_only_at_test_aoi': True,
        'old_coordinate_profile': current_profile, 'sources': [], 'calibration': {},
        'limitations': ['Delivery is not source identity, capture-date or survey accuracy verification.',
            'Mapbox and Mapquest are explicitly named imagery/road composites.',
            'No fixed shift is applied to other locations, sources, multispectral or SAR.']}
    mapping, images = {}, {}
    try:
        source_rows = probe.request('GET', '/api/optical/sources').get('sources', [])
        plan, discovered = candidate_plan(maps, source_rows, current)
        report['name_discovery'] = discovered
        for source in SOURCES:
            row = {'source': source, 'enabled': False, 'attempts': []}
            for candidate in plan[source][:4]:
                map_id = candidate['map_type']
                zoom = min(17, candidate.get('max_zoom', 17))
                try:
                    image, metadata, artifact = probe.capture(source, map_id, code, zoom)
                    mapping[source] = map_id
                    images[source] = (image, candidate, zoom)
                    row.update(enabled=True, map_type=map_id, name=candidate['name'], artifact=artifact,
                               availability='captured_at_test_aoi', spatial_registration='not_yet_verified')
                    row['attempts'].append({'map_type': map_id, 'passed': True})
                    break
                except ProbeError as exc:
                    row['attempts'].append({'map_type': map_id, 'passed': False,
                                            'error': exc.code, 'http_status': exc.status})
            report['sources'].append(row)
            print('SOURCE '+source+': '+('CAPTURED' if row['enabled'] else 'NOT_ENABLED'), flush=True)
        calibration = {}
        try:
            ref, refmeta, _ = probe.capture('arcgis', None, 0)
            # Prefer unlabelled optical products with good urban landmarks.
            selected = [s for s in ['bing', 'google', 'amap', 'tencent'] if s in images][:3]
            for source in selected:
                raw, candidate, zoom = images[source]
                pair = {}
                for native_code in [code, 1-code]:
                    try:
                        image = raw if native_code == code else probe.capture(source, mapping[source], native_code, zoom)[0]
                        audit, _ = audit_images(image, ref, refmeta)
                        pair[native_code] = {k: audit[k] for k in AUDIT_FIELDS if k in audit}
                    except (ProbeError, ValueError):
                        pair[native_code] = {'status': 'unknown', 'reason': 'capture_or_registration_unavailable'}
                calibration[source] = pair
            profile, reason = recommend_profile(calibration, current_profile)
        except (ProbeError, ValueError):
            profile, reason = current_profile, 'reference_unavailable_preserved_existing_profile'
        report.update(calibration=calibration, recommended_coordinate_profile=profile,
                      profile_selection_reason=reason, source_map_types=mapping,
                      gateway_optical_sources=sorted(set(mapping)|{'arcgis'}),
                      unavailable_sources=sorted(set(SOURCES)-set(mapping)))
        output.mkdir(parents=True, exist_ok=True)
        for name, data in [('source-map-types.json', mapping), ('source-upgrade.json', report)]:
            target = output/name
            if target.is_symlink():
                raise ProbeError('unsafe_output_path')
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    finally:
        probe.client.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', default='http://127.0.0.1:8765')
    p.add_argument('--worker-config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    try:
        r = expand(args.base_url, args.worker_config, args.output)
        print(json.dumps({'status': 'ok', 'enabled_sources': r['gateway_optical_sources'],
            'coordinate_profile': r['recommended_coordinate_profile'],
            'unavailable_sources': r['unavailable_sources']}))
        return 0
    except Exception:
        # Full exceptions may contain private paths or signed upstream URLs.
        print(json.dumps({'status': 'error', 'code': 'source_upgrade_probe_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
