"""Real temporary public acceptance, using only isolated synthetic credentials."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/deployment'


def main():
    fixture = ROOT / 'outputs/deployment-tests' / ('Cloudflare 中文 ' + uuid.uuid4().hex[:10])
    fixture.mkdir(parents=True)
    checks = []
    public_origin = None
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'
           and not k.upper().startswith(('OMAP_', 'TUNNEL_', 'CONDA', 'GDAL', 'PROJ'))}
    env['PYTHONIOENCODING'] = 'utf-8'
    ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')

    def run_ps(relative, *args, timeout=90):
        name = '.live-' + uuid.uuid4().hex
        output, errors = fixture / (name + '.out'), fixture / (name + '.err')
        with output.open('wb') as stdout, errors.open('wb') as stderr:
            result = subprocess.run([ps, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(fixture / relative), *args], cwd=fixture, env=env,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=timeout)
        return result.returncode

    def record(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        if not passed:
            raise RuntimeError(name)

    def python(*args, timeout=180):
        return subprocess.run([str(fixture / 'runtime/python.exe'), *args], cwd=fixture, env=env,
                              stdin=subprocess.DEVNULL, timeout=timeout).returncode

    try:
        with zipfile.ZipFile(OUT / 'omap-public-windows-v1.1-20261018.zip') as package:
            package.extractall(fixture)
        with zipfile.ZipFile(OUT / 'omap-cloudflare-entry-20261018.zip') as patch:
            patch.extractall(fixture)
        for port in (19865, 19866, 49402):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        record('PowerShell 5 worker setup in isolated Chinese path', run_ps('scripts/deploy/setup-worker.ps1', '-SkipDependencies') == 0)
        record('isolated worker starts on 19865', run_ps('scripts/deploy/start-worker.ps1', '-Port', '19865') == 0)
        record('isolated gateway private configuration generated', run_ps('setup-gateway.ps1') == 0)
        config_path = fixture / 'cluster.json'
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        config['nodes'][0]['base_url'] = 'http://127.0.0.1:19865'
        config['nodes'][0]['optical_sources'] = ['arcgis']
        config_path.write_text(json.dumps(config), encoding='utf-8')
        record('isolated gateway starts on 19866', run_ps('start-gateway.ps1', '-Port', '19866') == 0)
        record('official cloudflared binary revalidated', run_ps('install-cloudflared.ps1') == 0)
        record('real temporary Cloudflare tunnel connects with owned metrics',
               run_ps('start-quick-tunnel.ps1', '-Port', '19866', '-MetricsPort', '49402', '-StartupTimeoutSeconds', '60') == 0)
        endpoint = json.loads((fixture / 'outputs/quick-tunnel-endpoint.json').read_text(encoding='utf-8-sig'))
        public_origin = endpoint['public_url']
        record('public HTTPS gateway ready with anonymous business 401',
               python(str(fixture / 'scripts/deploy/wait_public_gateway.py'), '--base-url', public_origin, '--timeout', '60', timeout=75) == 0)
        code = python(str(fixture / 'scripts/deploy/verify_public_api.py'), '--base-url', public_origin,
                      '--cluster-config', str(config_path), '--output', str(OUT / 'cloudflare-public-api-validation.json'),
                      '--optical-sources', 'arcgis', '--run-science-crops', timeout=900)
        record('real public optical plus multispectral and SAR file acceptance', code == 0)
    except Exception:
        checks.append({'name': 'public acceptance completed without execution errors', 'passed': False})
        raise
    finally:
        for name in ('stop-quick-tunnel.ps1', 'stop-gateway.ps1', 'scripts/deploy/stop-worker.ps1'):
            if (fixture / name).is_file():
                try:
                    code = run_ps(name, timeout=30)
                except subprocess.TimeoutExpired:
                    code = 1
                checks.append({'name': name + ' isolated cleanup', 'passed': code == 0})
        report = {'tested_at': datetime.now(timezone.utc).isoformat(),
                  'scope': 'This development machine issued requests through a real public Cloudflare URL to a synthetic isolated worker/gateway; user remote Windows NOT yet tested',
                  'passed': bool(checks) and all(c['passed'] for c in checks), 'checks': checks,
                  'temporary_test_origin': public_origin, 'temporary_test_tunnel_stopped': any(
                      c['name'] == 'stop-quick-tunnel.ps1 isolated cleanup' and c['passed'] for c in checks),
                  'linux_ngrok_modified': False}
        (OUT / 'cloudflare-live-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('CLOUDFLARE_LIVE_ACCEPTANCE ' + ('PASS' if report['passed'] else 'FAIL'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
