"""Validate the distributed ZIP in an isolated Windows directory, without a tunnel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid
import zipfile

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    archive_path = args.package.resolve()
    fixture = ROOT / 'outputs/deployment-tests' / ('ngrok 最新完整包 ' + uuid.uuid4().hex[:10])
    fixture.mkdir(parents=True, exist_ok=False)
    checks = []
    report = {'tested_at':datetime.now(timezone.utc).isoformat(), 'passed':False,
              'package':str(archive_path), 'fixture':str(fixture), 'public_tunnel_started':False,
              'desktop_omap_contacted':False, 'checks':checks}
    env = {k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'
           and not k.upper().startswith(('OMAP_', 'PYTHON', 'CONDA', 'GDAL', 'PROJ'))}
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    env['PATH'] = str(Path(ps).parent) + os.pathsep + env['PATH']

    def check(name, condition):
        checks.append({'name':name, 'passed':bool(condition)})
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        if not condition:
            raise ValueError(name)

    def powershell(relative, *arguments):
        # Avoid inherited captured pipes keeping the parent alive after Start-Process.
        prefix = fixture / ('.validation-' + uuid.uuid4().hex)
        with prefix.with_suffix('.out').open('wb') as out, prefix.with_suffix('.err').open('wb') as err:
            result = subprocess.run([ps,'-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(fixture/relative),*arguments],
                                    cwd=fixture, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=90)
        return result.returncode, prefix.with_suffix('.out').read_bytes()+prefix.with_suffix('.err').read_bytes()

    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            return sock.getsockname()[1]

    worker_port, gateway_port, absent_omap_port = port(), port(), port()
    try:
        with zipfile.ZipFile(archive_path) as package:
            check('zip_crc_integrity', package.testzip() is None)
            manifest = json.loads(package.read('deployment-manifest.json'))
            names = set(package.namelist())
            check('all_manifest_hashes', names == set(manifest['files'])|{'deployment-manifest.json'} and all(
                hashlib.sha256(package.read(name)).hexdigest() == value for name,value in manifest['files'].items()))
            check('ngrok_entry_and_source_upgrade_included', {'DEPLOY-PUBLIC.cmd','UPDATE-NGROK.cmd',
                'scripts/deploy/expand_sources.py','scripts/deploy/verify_source_alignment.py',
                'runtime/python.exe','tools/ngrok/ngrok.exe','NGROK_WINDOWS_README.md','NGROK_API_EXAMPLES.md'} <= names)
            check('no_cloudflare_launcher_or_binary', not any('cloudflar' in name.lower() or 'quick-tunnel' in name for name in names))
            check('no_machine_configuration_or_images', not any(
                {'.env','.worker.env','.env.wuyu','cluster.json','ngrok.yml','outputs','__pycache__'}.intersection(Path(name).parts)
                or name.endswith(('.ovmap','.ovmapx','.log','.pid','.pyc')) for name in names))
            for item in package.infolist():
                if not (fixture/item.filename).resolve().is_relative_to(fixture.resolve()):
                    raise ValueError('unsafe_archive_path')
            package.extractall(fixture)
        entry = (fixture/'DEPLOY-PUBLIC.cmd').read_text('utf-8-sig').replace(
            'deploy-public.ps1"','deploy-public.ps1" -CheckOnly').replace('pause','exit /b %errorlevel%')
        (fixture/'CHECK-ENTRY.cmd').write_text(entry, 'ascii')
        checked = subprocess.run(['cmd.exe','/d','/c','CHECK-ENTRY.cmd'], cwd=fixture, env=env,
                                 capture_output=True, stdin=subprocess.DEVNULL, timeout=90)
        state = json.loads((fixture/'outputs/deployment-status/deployment-status.json').read_text('utf-8-sig'))
        check('double_click_chain_ps5_native_runtime', checked.returncode == 0 and state['status']=='environment_check_passed'
              and state['tunnel_provider']=='ngrok' and b'NameError' not in checked.stderr)
        code, output = powershell('scripts/deploy/setup-worker.ps1','-SkipDependencies','-OmapWsUrl',f'ws://127.0.0.1:{absent_omap_port}')
        config = dotenv_values(fixture/'.worker.env')
        worker_key = config.get('OMAP_WORKER_API_KEY','')
        check('private_worker_config_created_without_secret_output', code==0 and len(worker_key)>=43 and worker_key.encode() not in output)
        code,_ = powershell('scripts/deploy/setup-worker.ps1','-SkipDependencies')
        check('repeat_setup_preserves_worker_key', code==0 and dotenv_values(fixture/'.worker.env')['OMAP_WORKER_API_KEY']==worker_key)
        code,_ = powershell('scripts/deploy/start-worker.ps1','-Port',str(worker_port))
        check('worker_starts_from_unicode_spaced_path', code==0)
        code,_ = powershell('setup-gateway.ps1')
        check('gateway_private_configuration_created', code==0)
        cluster_path=fixture/'cluster.json'
        cluster=json.loads(cluster_path.read_text('utf-8-sig'))
        for node in cluster['nodes']:
            node['enabled']=node['id']=='windows-1'
            if node['enabled']:
                node.update(base_url=f'http://127.0.0.1:{worker_port}',api_key=worker_key,optical_sources=['arcgis'])
        cluster_path.write_text(json.dumps(cluster), 'utf-8')
        client_key=cluster['clients'][0]['api_key']
        code,output = powershell('start-gateway.ps1','-Port',str(gateway_port))
        check('gateway_starts_without_credentials_in_output', code==0 and client_key.encode() not in output)
        with httpx.Client(base_url=f'http://127.0.0.1:{gateway_port}',trust_env=False,timeout=15) as client:
            check('gateway_health',client.get('/health').status_code==200)
            check('business_api_requires_bearer',client.get('/api/jobs/not-a-job').status_code==401)
            headers={'Authorization':'Bearer '+client_key}
            for path in ('/api/optical/latest','/api/multispectral/search','/api/multispectral/subset','/api/sar/search','/api/sar/crop'):
                response=client.post(path,headers=headers,json={})
                check('request_schema_loaded_'+path, response.status_code==422)
        report['passed']=all(row['passed'] for row in checks)
    except Exception as error:
        report['error_type']=type(error).__name__
    finally:
        cleanup=[]
        for relative in ('stop-gateway.ps1','scripts/deploy/stop-worker.ps1'):
            try:
                if (fixture/relative).exists():
                    code,_=powershell(relative);cleanup.append(code==0)
            except Exception:
                cleanup.append(False)
        report['isolated_services_stopped']=all(cleanup)
        report['passed']=report['passed'] and report['isolated_services_stopped']
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print('NGROK_PACKAGE_ACCEPTANCE '+('PASS' if report['passed'] else 'FAIL'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
