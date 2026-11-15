"""Regression acceptance with real Windows PowerShell 5 and bundled Python.

Uses an isolated copy and synthetic credentials; no ngrok process or account.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import zipfile

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs' / 'deployment'
SOURCE_ZIP = OUT / 'omap-public-windows-v1-20261018.zip'
REPLACEMENTS = ['deploy-public.ps1', 'scripts/deploy/setup-worker.ps1', 'scripts/deploy/probe_runtime.py']


def main():
    fixture = ROOT / 'outputs' / 'deployment-tests' / ('中文 桌面 ' + uuid.uuid4().hex[:12])
    fixture.mkdir(parents=True)
    checks = []
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'
           and not k.upper().startswith(('OMAP_', 'CONDA', 'GDAL', 'PROJ'))}
    env['PYTHONIOENCODING'] = 'utf-8'
    ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')

    def record(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        if not passed:
            raise RuntimeError(name)

    def powershell(relative, *args, timeout=75):
        # A detached Start-Process child can inherit a captured pipe on PS5,
        # keeping subprocess.communicate blocked after its parent has exited.
        stem = '.validation-' + uuid.uuid4().hex
        output = fixture / (stem + '.stdout')
        errors = fixture / (stem + '.stderr')
        with output.open('wb') as out, errors.open('wb') as err:
            result = subprocess.run([ps, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(fixture / relative), *args], cwd=fixture, env=env,
                                    stdout=out, stderr=err, timeout=timeout, stdin=subprocess.DEVNULL)
        result.stdout, result.stderr = output.read_bytes(), errors.read_bytes()
        return result

    def write_ps(relative, source):
        (fixture / relative).write_text(source, encoding='utf-8-sig')

    try:
        with zipfile.ZipFile(SOURCE_ZIP) as package:
            for item in package.infolist():
                if not (fixture / item.filename).resolve().is_relative_to(fixture.resolve()):
                    raise RuntimeError('Unsafe ZIP path')
            package.extractall(fixture)
        patch_path = OUT / 'omap-startup-fix-20261018.zip'
        if patch_path.exists():
            # Verify the exact distributed bytes, including BOM and line endings.
            with zipfile.ZipFile(patch_path) as patch:
                for name in REPLACEMENTS:
                    (fixture / name).write_bytes(patch.read(name))
        else:
            for name in REPLACEMENTS:
                source = (ROOT / name).read_text(encoding='utf-8-sig')
                (fixture / name).write_text(source, encoding='utf-8-sig' if name.endswith('.ps1') else 'utf-8')
        write_ps('legacy-repro.ps1',
                 "$p=Join-Path $PSScriptRoot 'runtime\\python.exe'\n"
                 "& $p -c 'print(\"运行环境检查通过\")'\nexit $LASTEXITCODE\n")
        legacy = powershell('legacy-repro.ps1')
        record('real PowerShell 5 reproduces original NameError',
               legacy.returncode != 0 and b'NameError' in legacy.stderr)

        # Same CMD -> Windows PowerShell -File chain as the double-click entry.
        entry = (fixture / 'DEPLOY-PUBLIC.cmd').read_text(encoding='ascii')
        entry = entry.replace('deploy-public.ps1"', 'deploy-public.ps1" -CheckOnly')
        entry = entry.replace('pause', 'exit /b %errorlevel%')
        (fixture / 'CHECK-ENTRY.cmd').write_text(entry, encoding='ascii')
        launch = subprocess.run(['cmd.exe', '/d', '/c', 'CHECK-ENTRY.cmd'], cwd=fixture,
                                env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=75)
        status = json.loads((fixture / 'outputs/deployment-status/deployment-status.json').read_text(encoding='utf-8-sig'))
        record('fixed CMD entry passes real bundled Python environment check',
               launch.returncode == 0 and status['status'] == 'environment_check_passed'
               and b'NameError' not in launch.stderr)

        setup = powershell('scripts/deploy/setup-worker.ps1', '-SkipDependencies')
        record('real PowerShell 5 setup-worker portable probe succeeds', setup.returncode == 0)
        worker = dotenv_values(fixture / '.worker.env')
        key = worker['OMAP_WORKER_API_KEY']
        record('isolated worker key generated without disclosure', len(key) >= 43 and key.encode() not in setup.stdout + setup.stderr)
        again = powershell('scripts/deploy/setup-worker.ps1', '-SkipDependencies')
        record('repeated setup preserves generated key', again.returncode == 0 and dotenv_values(fixture / '.worker.env')['OMAP_WORKER_API_KEY'] == key)
        write_ps('check-acl.ps1',
                 "$a=Get-Acl -LiteralPath (Join-Path $PSScriptRoot '.worker.env'); "
                 "if (-not $a.AreAccessRulesProtected) {exit 1}; exit 0")
        record('generated private worker file has protected ACL', powershell('check-acl.ps1').returncode == 0)
        for port in (18865, 18866):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        started = powershell('scripts/deploy/start-worker.ps1', '-Port', '18865')
        record('worker starts from Chinese and spaced path using PowerShell 5', started.returncode == 0)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
            record('isolated worker health responds', client.get('http://127.0.0.1:18865/health').json()['status'] == 'ok')
            record('isolated worker rejects anonymous business request', client.get('http://127.0.0.1:18865/api/multispectral/sources').status_code == 401)
        record('PowerShell 5 repeated worker start reuses verified process', powershell('scripts/deploy/start-worker.ps1', '-Port', '18865').returncode == 0)
        gateway_setup = powershell('setup-gateway.ps1')
        record('PowerShell 5 gateway setup generates private config', gateway_setup.returncode == 0)
        config_path = fixture / 'cluster.json'
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        config['nodes'][0]['base_url'] = 'http://127.0.0.1:18865'
        config['nodes'][0]['optical_sources'] = ['arcgis']
        config_path.write_text(json.dumps(config), encoding='utf-8')
        started_gateway = powershell('start-gateway.ps1', '-Port', '18866')
        record('PowerShell 5 gateway starts on isolated port', started_gateway.returncode == 0)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
            record('isolated gateway rejects anonymous business request', client.get('http://127.0.0.1:18866/api/nodes').status_code == 401)
            reply = client.get('http://127.0.0.1:18866/api/multispectral/sources',
                               headers={'Authorization': 'Bearer ' + config['clients'][0]['api_key']})
            record('isolated gateway authenticates source catalogue', reply.status_code == 200 and bool(reply.json().get('sources')))
    except Exception:
        checks.append({'name': 'acceptance completed without execution errors', 'passed': False})
        raise
    finally:
        # Never stop a production service; these scripts read this fixture's records only.
        for relative in ('stop-gateway.ps1', 'scripts/deploy/stop-worker.ps1'):
            if (fixture / relative).exists():
                stopped = powershell(relative)
                checks.append({'name': relative + ' isolated cleanup', 'passed': stopped.returncode == 0})
        report = {'tested_at': datetime.now(timezone.utc).isoformat(),
                  'scope': 'Real Windows PowerShell 5 plus embedded Python, synthetic isolated configuration, no remote or ngrok test',
                  'passed': bool(checks) and all(c['passed'] for c in checks), 'checks': checks,
                  'tested_source_sha256': {n: hashlib.sha256((fixture/n).read_bytes()).hexdigest() for n in REPLACEMENTS}}
        (OUT / 'powershell5-portable-fix-validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('POWERSHELL5_ACCEPTANCE ' + ('PASS' if report['passed'] else 'FAIL'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
