"""Wait for TLS/DNS/edge propagation before transmitting API credentials."""
from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urlsplit

import httpx


def wait_ready(base_url: str, timeout: float = 45, *, transport=None, sleep=time.sleep, monotonic=time.monotonic) -> dict:
    if (not isinstance(base_url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in base_url)
            or not 1 <= timeout <= 60):
        raise ValueError('Invalid public origin or timeout')
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in {'', '/'} or parsed.port not in {None, 443}):
        raise ValueError('HTTPS public origin required')
    origin = base_url.rstrip('/')
    end = monotonic() + timeout
    attempts = 0
    with httpx.Client(follow_redirects=False, timeout=3, transport=transport) as client:
        while monotonic() < end:
            attempts += 1
            try:
                remaining = max(.1, min(3, end-monotonic()))
                response = client.get(origin + '/health', timeout=remaining)
                if (response.status_code == 200 and response.json().get('status') == 'ok'
                        and response.json().get('service') == 'imagery_gateway'):
                    if monotonic() >= end:
                        break
                    auth = client.get(origin + '/api/nodes', timeout=max(.1, min(3, end-monotonic())))
                    if auth.status_code == 401:
                        return {'status': 'ok', 'https_health': True, 'anonymous_rejected': True, 'attempts': attempts}
                    if auth.status_code == 200:
                        return {'status': 'error', 'code': 'public_authentication_not_enabled', 'attempts': attempts}
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            sleep(max(0, min(2, end-monotonic())))
    return {'status': 'error', 'code': 'public_gateway_not_ready', 'attempts': attempts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args()
    try:
        result = wait_ready(args.base_url, args.timeout)
    except ValueError:
        result = {'status': 'error', 'code': 'invalid_public_origin'}
    print(json.dumps(result))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
