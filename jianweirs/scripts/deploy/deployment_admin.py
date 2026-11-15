"""Small local deployment operations; stdout never contains credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def local_origin(value: str) -> str:
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError('URL contains control or whitespace')
    parts = urlsplit(value)
    if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {'', '/'}):
        raise ValueError('Local origin required')
    if not parts.port:
        raise ValueError('Explicit local port required')
    return value.rstrip('/')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['probe', 'configure-gateway', 'export-connection'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--sources', type=Path)
    parser.add_argument('--public-url')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == 'probe':
            origin = local_origin(args.base_url)
            key = dotenv_values(root / '.worker.env')['OMAP_WORKER_API_KEY']
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=18) as client:
                response = client.get(origin + '/api/maps', headers={'Authorization': f'Bearer {key}'})
            code = 'omap_unavailable'
            if (response.status_code == 200 and isinstance(response.json(), dict)
                    and isinstance(response.json().get('maps'), list)):
                print(json.dumps({'status': 'ok', 'omap_authenticated': True}))
                return 0
            detail = response.json().get('detail', {})
            if isinstance(detail, dict) and detail.get('code') in {'omap_auth_failed', 'omap_unavailable', 'omap_timeout', 'omap_permission_denied', 'worker_authentication_required'}:
                code = detail['code']
            print(json.dumps({'status': 'error', 'code': code}))
            return 1
        config_path = root / 'cluster.json'
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if args.command == 'configure-gateway':
            from omap_api.gateway import Cluster
            mapping = json.loads(args.sources.read_text(encoding='utf-8-sig')) if args.sources else {}
            worker = dotenv_values(root / '.worker.env')
            node = next(n for n in config['nodes'] if n['id'] == 'windows-1')
            node.update(base_url=local_origin(args.base_url), api_key=worker['OMAP_WORKER_API_KEY'],
                        optical_sources=sorted(set(mapping) | {'arcgis'}))
            Cluster.model_validate(config)
            text = json.dumps(config, ensure_ascii=False, indent=2)
            changed = config_path.read_text(encoding='utf-8-sig') != text
            if changed:
                # Bootstrap creates this empty temporary file and protects its ACL first.
                if (not args.output or not args.output.is_file() or args.output.is_symlink()
                        or args.output.resolve().parent != root or args.output.resolve() == config_path):
                    raise ValueError('Protected temporary file required')
                with args.output.open('w', encoding='utf-8') as destination:
                    destination.write(text)
                    destination.flush()
                    os.fsync(destination.fileno())
                args.output.replace(config_path)
            print(json.dumps({'status': 'ok', 'changed': changed, 'sources': node['optical_sources']}))
            return 0
        if not args.public_url or any(ord(c) <= 32 or ord(c) == 127 for c in args.public_url):
            raise ValueError('Invalid HTTPS origin')
        parts = urlsplit(args.public_url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {'', '/'}:
            raise ValueError('HTTPS public origin required')
        if not args.output or not args.output.is_file() or args.output.is_symlink():
            raise ValueError('Protected destination must be created first')
        _ = parts.port
        key = config['clients'][0]['api_key']
        if not isinstance(key, str) or len(key) < 32 or any(ord(c) <= 32 or ord(c) == 127 for c in key):
            raise ValueError('Invalid API key')
        args.output.write_text(f'REMOTE_SENSING_API_BASE_URL={args.public_url.rstrip("/")}\nREMOTE_SENSING_API_KEY={key}\n', encoding='utf-8')
        print(json.dumps({'status': 'ok', 'credentials_printed': False}))
        return 0
    except (OSError, ValueError, KeyError, StopIteration, httpx.HTTPError):
        print(json.dumps({'status': 'error', 'code': 'local_deployment_configuration_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
