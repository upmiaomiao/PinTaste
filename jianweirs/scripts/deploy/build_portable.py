"""Build an allowlisted Windows x64 bundle with an application-local runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = '3.13.16'
URL = f'https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip'
ROOT_FILES = ['DEPLOY-PUBLIC.cmd', 'STOP-PUBLIC.cmd', 'deploy-public.ps1', 'stop-public.ps1',
              'DEPLOY-CLOUDFLARE.cmd', 'start-quick-tunnel.ps1', 'stop-quick-tunnel.ps1', 'install-cloudflared.ps1',
              'RECONFIGURE-NGROK.cmd', 'reconfigure-ngrok.ps1',
              'PORTABLE_README.md', 'PUBLIC_API_QUICKSTART.md', 'CLOUD_DEPLOYMENT.md', 'requirements.txt', 'cluster.example.json',
              'start-api.ps1', 'stop-api.ps1', 'setup-gateway.ps1', 'start-gateway.ps1',
              'stop-gateway.ps1', 'install-ngrok.ps1', 'setup-ngrok.ps1',
              'start-ngrok.ps1', 'stop-ngrok.ps1', 'FOUR_APIS.md']
DEPLOY_FILES = ['worker-common.ps1', 'setup-worker.ps1', 'start-worker.ps1', 'stop-worker.ps1',
                'configure_sources.py', 'verify_public_api.py', 'deployment_admin.py', 'probe_runtime.py', 'wait_public_gateway.py']
CLOUDFLARE_FILES = {'DEPLOY-CLOUDFLARE.cmd', 'start-quick-tunnel.ps1', 'stop-quick-tunnel.ps1', 'install-cloudflared.ps1'}
NGROK_ROOT_FILES = ['UPDATE-NGROK.cmd', 'NGROK_WINDOWS_README.md', 'NGROK_API_EXAMPLES.md']
CURRENT_DEPLOY_FILES = ['expand_sources.py', 'verify_source_alignment.py', '.worker.env.example', 'source-map-types.example.json']


def package_files(tunnel_provider: str) -> tuple[list[str], list[str]]:
    roots = [name for name in ROOT_FILES if tunnel_provider != 'ngrok' or name not in CLOUDFLARE_FILES]
    if tunnel_provider == 'ngrok':
        roots += NGROK_ROOT_FILES
    return roots, list(dict.fromkeys(DEPLOY_FILES + CURRENT_DEPLOY_FILES))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signature(path: Path, publisher: str) -> None:
    # Paths go in an environment variable, never in executable PowerShell text.
    import os
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    env['OMAP_SIGNATURE_FILE'] = str(path)
    # A PowerShell 7 parent can pass module paths incompatible with Windows PowerShell 5.
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                             "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:OMAP_SIGNATURE_FILE; "
                             '$s.Status.ToString(); $s.SignerCertificate.Subject'],
                            env=env, text=True, capture_output=True, check=True)
    lines = result.stdout.strip().splitlines()
    if not lines or lines[0] != 'Valid' or publisher.lower() not in '\n'.join(lines[1:]).lower():
        raise RuntimeError(f'Official publisher signature check failed: {path.name}')


def build_runtime(cache: Path) -> Path:
    import struct
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 13) or struct.calcsize('P') != 8:
        raise RuntimeError('Build requires Windows x64 Python 3.13 for matching binary wheels')
    requirements_text = '\n'.join(line for line in (ROOT / 'requirements.txt').read_text().splitlines()
                                  if line and not line.startswith('pytest')) + '\n'
    requirement_hash = hashlib.sha256(requirements_text.encode()).hexdigest()
    runtime = cache / f'python-{VERSION}-runtime-{requirement_hash[:12]}'
    if (runtime / 'runtime-ready.json').exists():
        subprocess.run([str(runtime / 'python.exe'), '-c', 'import fastapi,uvicorn,websockets,PIL,dotenv,httpx,rasterio,numpy,cv2'], check=True)
        return runtime
    archive = cache / f'python-{VERSION}-embed-amd64.zip'
    if archive.exists():
        try:
            with zipfile.ZipFile(archive) as existing:
                if existing.testzip() is not None:
                    raise zipfile.BadZipFile()
        except zipfile.BadZipFile:
            archive.rename(archive.with_name(archive.name + '.invalid-' + datetime.now().strftime('%Y%m%d%H%M%S')))
    if not archive.exists():
        print('Downloading official Python runtime...', flush=True)
        partial = archive.with_suffix('.part')
        with urllib.request.urlopen(URL, timeout=90) as response, partial.open('wb') as target:
            shutil.copyfileobj(response, target)
        with zipfile.ZipFile(partial) as downloaded:
            if downloaded.testzip() is not None:
                raise RuntimeError('Incomplete Python download; retry build')
        partial.replace(archive)
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            target = (runtime / item.filename).resolve()
            if not target.is_relative_to(runtime.resolve()):
                raise RuntimeError('Unsafe runtime archive entry')
        package.extractall(runtime)
    signature(runtime / 'python.exe', 'Python Software Foundation')
    (runtime / 'python313._pth').write_text('python313.zip\n.\nsite-packages\n..\nimport site\n', encoding='ascii')
    requirements = cache / 'runtime-requirements.txt'
    requirements.write_text(requirements_text, encoding='utf-8')
    wheels = cache / 'wheels'
    wheels.mkdir(exist_ok=True)
    print('Downloading binary dependencies...', flush=True)
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check',
                    '--only-binary=:all:', '-r', str(requirements), '-d', str(wheels)], check=True)
    print('Installing application-local dependencies...', flush=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--no-index', '--find-links', str(wheels), '--only-binary=:all:', '--no-compile',
                    '--target', str(runtime / 'site-packages'), '-r', str(requirements)], check=True)
    subprocess.run([str(runtime / 'python.exe'), '-c',
                    'import fastapi,uvicorn,websockets,PIL,dotenv,httpx,rasterio,numpy,cv2; print("Runtime import check passed")'], check=True)
    versions = subprocess.check_output([str(runtime / 'python.exe'), '-c',
                    'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'], text=True)
    (runtime / 'runtime-ready.json').write_text(json.dumps({
        'python': VERSION, 'abi': 'cp313-win_amd64', 'requirements_sha256': requirement_hash,
        'python_download_url': URL, 'python_archive_sha256': sha(archive),
        'packages': json.loads(versions),
        'wheels': {p.name: sha(p) for p in sorted(wheels.glob('*.whl'))}}, indent=2), encoding='utf-8')
    return runtime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--tunnel-provider', choices=['ngrok', 'all'], default='all')
    parser.add_argument('--release', default='current')
    args = parser.parse_args()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    destination = (args.destination or ROOT / 'outputs' / 'deployment' / f'omap-public-portable-{stamp}.zip').resolve()
    if destination.exists() or destination.suffix.lower() != '.zip':
        raise SystemExit('Destination must be a new ZIP path')
    cache = ROOT / 'outputs' / 'deployment-cache'
    cache.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(cache)
    signature(runtime / 'python.exe', 'Python Software Foundation')
    stage = destination.parent / (destination.stem + '-folder')
    stage.mkdir(parents=True, exist_ok=False)
    roots, deployment_files = package_files(args.tunnel_provider)
    sources = [(ROOT / name, stage / name) for name in roots]
    sources += [(ROOT / 'scripts' / 'deploy' / name, stage / 'scripts' / 'deploy' / name) for name in deployment_files]
    sources += [(p, stage / p.relative_to(ROOT)) for p in sorted((ROOT / 'omap_api').glob('*.py'))]
    sources += [(p, stage / p.relative_to(ROOT)) for p in sorted((ROOT / 'omap_api' / 'resources').glob('*.json'))]
    for source, target in sources:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.ps1':
            target.write_text(source.read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
        else:
            shutil.copy2(source, target)
    if args.tunnel_provider == 'ngrok':
        (stage / '先读我-部署说明.txt').write_text((ROOT / 'NGROK_WINDOWS_README.md').read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
    shutil.copytree(runtime, stage / 'runtime', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    ngrok = ROOT / 'tools' / 'ngrok' / 'ngrok.exe'
    signature(ngrok, 'ngrok')
    target_ngrok = stage / 'tools' / 'ngrok' / 'ngrok.exe'
    target_ngrok.parent.mkdir(parents=True)
    shutil.copy2(ngrok, target_ngrok)
    import os
    probe_env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('CONDA', 'PYTHON', 'GDAL', 'PROJ'))}
    probe_env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    subprocess.run([str(stage / 'runtime' / 'python.exe'), '-B', str(stage / 'scripts' / 'deploy' / 'probe_runtime.py')],
                   cwd=stage, env=probe_env, check=True)
    # Actual private values are read only for a leakage check and never printed.
    from dotenv import dotenv_values
    private_values = []
    for name in ['.env', '.worker.env', '.env.wuyu']:
        private_values.extend(v.encode() for k, v in dotenv_values(ROOT / name).items()
                              if v and len(v) >= 12 and any(s in k.upper() for s in ['KEY', 'TOKEN', 'PSK', 'SECRET', 'PASSWORD']))
    try:
        cluster = json.loads((ROOT / 'cluster.json').read_text(encoding='utf-8-sig'))
        private_values.extend(x['api_key'].encode() for group in ['nodes', 'clients'] for x in cluster[group]
                              if not x['api_key'].startswith('REPLACE_'))
    except FileNotFoundError:
        pass
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'package_type': 'Windows x64 portable gateway and worker',
                'release': args.release, 'tunnel_provider': args.tunnel_provider,
                'primary_api_groups': ['optical_latest_2026_sources', 'multispectral', 'sar'],
                'secrets_included': False, 'files': {}}
    for path in sorted(stage.rglob('*')):
        if not path.is_file():
            continue
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        relative = path.relative_to(stage).as_posix()
        if any(part in {'.env', '.worker.env', 'cluster.json', 'ngrok.yml', 'outputs', '.venv'} for part in path.relative_to(stage).parts):
            raise RuntimeError('Forbidden private file in bundle')
        data = path.read_bytes()
        if any(value in data for value in private_values):
            raise RuntimeError('Private configuration value detected in bundle')
        manifest['files'][relative] = hashlib.sha256(data).hexdigest()
    (stage / 'deployment-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                package.write(path, path.relative_to(stage).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity check failed')
        if set(archive.namelist()) != set(manifest['files']) | {'deployment-manifest.json'}:
            raise RuntimeError('Archive manifest has missing or unlisted files')
        if not all(hashlib.sha256(archive.read(name)).hexdigest() == value for name, value in manifest['files'].items()):
            raise RuntimeError('Archive manifest hash mismatch')
    report = {'zip': str(destination), 'folder': str(stage), 'bytes': destination.stat().st_size,
              'sha256': sha(destination), 'files': len(manifest['files']) + 1,
              'private_value_scan': 'passed', 'portable_imports': 'passed',
              'archive_integrity': 'passed', 'all_file_hashes_verified': True,
              'tunnel_provider': args.tunnel_provider, 'release': args.release}
    destination.with_suffix('.build.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
