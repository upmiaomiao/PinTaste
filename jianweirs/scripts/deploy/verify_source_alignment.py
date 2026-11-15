"""Exercise automatic per-job optical alignment through the authenticated gateway."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.deploy.expand_sources import BBOX
from scripts.deploy.verify_public_api import (GatewayClient, PublicVerifier,
    VerificationError, load_private_configuration, write_report)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', required=True)
    p.add_argument('--cluster-config', type=Path, required=True)
    p.add_argument('--sources-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    secrets = []
    try:
        key, secrets = load_private_configuration(a.cluster_config)
        v = PublicVerifier(GatewayClient(a.base_url, key), a.output, poll_timeout=240)
        mapping = json.loads(a.sources_file.read_text(encoding='utf-8-sig'))
        rows = []
        for source in mapping:
            if source not in {'bing','amap','tencent','google','mapbox','mapquest','tianditu','siwei','siwei_daily'}:
                raise ValueError
            row = {'source': source, 'scope': 'this_AOI_asset_only', 'verified': False}
            try:
                job, result = v.run_job('/api/optical/latest', {'source': source, 'bbox': BBOX,
                    'input_crs': 'EPSG:4326', 'coord_type': 0, 'zoom': 17,
                    'width': 768, 'height': 768, 'timeout_seconds': 100,
                    'alignment_reference': 'arcgis', 'registration_mode': 'strict', 'correct_alignment': True})
                audit = result.get('metadata', {}).get('spatial_registration', {})
                if not audit.get('strict_same_point_eligible') or audit.get('status') != 'verified':
                    raise VerificationError('alignment_not_verified')
                data = v.job_file(job, result)
                row.update(verified=True, artifact=v.save_artifact('aligned-'+source+'.png', data),
                    spatial_registration=audit)
            except VerificationError as exc:
                row['error'] = {'code': exc.code, **exc.fields}
                row['interpretation'] = 'context_only_or_unavailable; do_not_claim_same_point'
            rows.append(row)
            print('ALIGNMENT '+source+': '+('VERIFIED' if row['verified'] else 'UNVERIFIED'), flush=True)
        report = {'tested_at': datetime.now(timezone.utc).isoformat(), 'public_origin': a.base_url,
            'bbox_wgs84': BBOX, 'checks': rows, 'passed': all(r['verified'] for r in rows),
            'note': 'Relative optical-to-Esri alignment only. No surveyed accuracy or spectral/SAR registration is certified.'}
        write_report(a.output, report, secrets)
        return 0 if report['passed'] else 1
    except Exception:
        print('ALIGNMENT_VERIFICATION_CONFIGURATION_ERROR')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
