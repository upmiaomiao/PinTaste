"""Bounded diagnostics for only the two new product services; no central login."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def child(launcher):
    """Print public routing fields only, never cookies or request bodies."""
    import runpy
    import sys
    import uvicorn
    original_run = uvicorn.run
    def observed_run(app, **kwargs):
        async def observed(scope, receive, send):
            if scope['type'] == 'http':
                print('ROUTING ' + json.dumps({
                    'scheme': scope.get('scheme'), 'client': scope.get('client'),
                    'path': scope.get('path'), 'root_path': scope.get('root_path'),
                    'host': next((v.decode() for k,v in scope.get('headers',[]) if k==b'host'),None),
                }), flush=True)
            await app(scope, receive, send)
        return original_run(observed, **kwargs)
    uvicorn.run = observed_run
    sys.argv = [launcher, *sys.argv[3:]]
    runpy.run_path(launcher, run_name='__main__')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--shared', type=Path, default=Path('/opt/wuyu-jianweirs/shared'))
    parser.add_argument('--seconds', type=int, choices=range(6,31), default=20)
    args = parser.parse_args()
    import httpx
    release = args.release.resolve(strict=True)
    if release.parent != Path('/opt/wuyu-jianweirs/releases'):
        raise SystemExit('Release is outside the product deployment directory')
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', WUYU_NODE_PATH='/opt/node/bin/node')
    processes = []
    logs = []
    try:
        for launcher, output in [('jianweirs_science_server.py', 'science'), ('jianweirs_server.py', 'portal')]:
            log = args.shared / ('readiness-' + output + '.log')
            stream = log.open('ab')
            logs.append(stream)
            os.chmod(log, 0o600)
            process = subprocess.Popen([
                str(release / '.venv/bin/python'), str(Path(__file__).resolve()), '--child-launcher', str(release / 'scripts/deploy' / launcher),
                '--env-file', str(args.shared / 'server.env'), '--output-dir', str(args.shared / output),
            ], cwd=release, env=env, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
        for delay in range(args.seconds):
            time.sleep(1)
            if delay not in (1, 4, 9, 19):
                continue
            records = []
            for url in ('http://127.0.0.1:8171/health', 'http://172.17.0.1:8170/health',
                        'http://172.17.0.1:8170/portal/api/session'):
                for forwarded in (False, True):
                    headers = {'Host': 'ai.wuyullm.com'}
                    if forwarded:
                        headers['X-Forwarded-Proto'] = 'https'
                    try:
                        response = httpx.get(url, headers=headers, timeout=2, trust_env=False, follow_redirects=False)
                        item = {'path': url.split(':817')[1], 'forwarded_https': forwarded, 'status': response.status_code}
                        if response.status_code != 200:
                            body = response.json()
                            item['detail'] = body.get('detail') if isinstance(body, dict) else None
                    except (httpx.HTTPError, ValueError) as error:
                        item = {'path': url.split(':817')[1], 'forwarded_https': forwarded, 'error': type(error).__name__}
                    records.append(item)
                if ':8170/' in url:
                    try:
                        with httpx.Client(transport=httpx.HTTPTransport(local_address='127.0.0.1'), trust_env=False, timeout=2) as session:
                            response = session.get(url, headers={'Host':'ai.wuyullm.com','X-Forwarded-Proto':'https'})
                        records.append({'path':url.split(':817')[1], 'source_loopback':True, 'status':response.status_code})
                    except httpx.HTTPError as error:
                        records.append({'path':url.split(':817')[1], 'source_loopback':True, 'error':type(error).__name__})
            print(json.dumps({'elapsed': delay + 1, 'alive': [p.poll() is None for p in processes], 'probes': records}), flush=True)
            if delay == 4:
                import importlib.util
                spec = importlib.util.spec_from_file_location('deployment_ready', Path(__file__).with_name('jianweirs_activate.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                ready_status = {}
                for endpoint in ('http://127.0.0.1:8171/health', 'http://172.17.0.1:8170/health',
                                 'http://172.17.0.1:8170/portal/api/session'):
                    ready_status[endpoint] = module.ready(endpoint, seconds=3).status_code
                proxy = subprocess.run(['docker','exec','nginx-1.29','curl','--silent','--fail','--max-time','3',
                    '-H','Host: ai.wuyullm.com','-H','X-Forwarded-Proto: https',
                    'http://172.17.0.1:8170/health'], capture_output=True, timeout=5)
                print(json.dumps({'activation_ready':ready_status,'nginx_private_health_passed':proxy.returncode == 0}),flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in logs:
            stream.close()
        for name in ('science', 'portal'):
            p = args.shared / ('readiness-' + name + '.log')
            for line in p.read_text(errors='replace').splitlines():
                if line.startswith('ROUTING '):
                    print(line, flush=True)


if __name__ == '__main__':
    import sys
    if len(sys.argv)>2 and sys.argv[1]=='--child-launcher':
        child(sys.argv[2])
    else:
        main()
