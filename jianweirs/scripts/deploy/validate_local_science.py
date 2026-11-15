"""Manual acceptance against an isolated loopback gateway, never a public claim."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment_admin import local_origin
from verify_public_api import PublicVerifier, Reply, load_private_configuration, write_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--cluster-config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    origin = local_origin(args.base_url)
    key, secrets = load_private_configuration(args.cluster_config)
    with httpx.Client(trust_env=False, follow_redirects=False) as http:
        class LocalClient:
            base_url = origin

            def request(self, method, path, payload=None, *, authenticated=True, maximum=5*1024*1024, timeout=25):
                headers = {'Authorization': 'Bearer ' + key} if authenticated else {}
                response = http.request(method, origin + path, json=payload, headers=headers, timeout=timeout)
                if len(response.content) > maximum:
                    raise ValueError('Response too large')
                return Reply(response.status_code, response.content, response.headers.get('content-type', ''))

        verifier = PublicVerifier(LocalClient(), args.output, optical_sources=['arcgis'], poll_interval=1)
        for group in ('multispectral', 'sar'):
            selected = []

            def catalogue():
                fields, scenes = verifier.catalog(group)
                selected.extend(scenes)
                return fields

            result = verifier.attempt('catalog', group + '_actual_scene_search', catalogue)
            if result is not None:
                verifier.attempt('science', group + '_actual_numerical_geotiff',
                                 lambda: verifier.science_crop(group, selected))
        passed = len(verifier.checks) == 4 and all(c['passed'] for c in verifier.checks)
        report = {'tested_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Isolated local portable worker/gateway on loopback; ngrok/public/remote machine NOT tested',
                  'base_url': origin, 'passed': passed, 'checks': verifier.checks}
        write_report(args.output, report, secrets)
        print('LOCAL_SCIENCE_ACCEPTANCE ' + ('PASS' if passed else 'FAIL'), flush=True)
        return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
