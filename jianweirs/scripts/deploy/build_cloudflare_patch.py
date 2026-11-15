"""Add a verified Cloudflare client and launchers to the existing portable release."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/deployment'
BASE = OUT / 'omap-public-windows-v1.1-20261018.zip'
PATCH = OUT / 'omap-cloudflare-entry-20261018.zip'
FULL = OUT / 'omap-public-windows-v1.2-cloudflare-20261018.zip'
FILES = ['DEPLOY-CLOUDFLARE.cmd', 'deploy-public.ps1', 'stop-public.ps1', 'STOP-PUBLIC.cmd',
         'install-cloudflared.ps1', 'start-quick-tunnel.ps1', 'stop-quick-tunnel.ps1',
         'start-ngrok.ps1', 'setup-ngrok.ps1', 'RECONFIGURE-NGROK.cmd', 'reconfigure-ngrok.ps1',
         'scripts/deploy/setup-worker.ps1', 'scripts/deploy/probe_runtime.py',
         'scripts/deploy/wait_public_gateway.py', 'scripts/deploy/verify_public_api.py',
         'scripts/deploy/deployment_admin.py', 'CLOUDFLARE_README.md', 'PORTABLE_README.md',
         'tools/cloudflared/cloudflared.exe', 'tools/cloudflared/cloudflared-install.json']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--replace-unpublished', action='store_true')
    args = parser.parse_args()
    original = json.loads(BASE.with_suffix('.build.json').read_text())
    if hashlib.sha256(BASE.read_bytes()).hexdigest() != original['full_sha256']:
        raise RuntimeError('Base package hash mismatch')
    if (PATCH.exists() or FULL.exists()) and not args.replace_unpublished:
        raise RuntimeError('Release files already exist')
    replacements = {}
    for name in FILES:
        source = ROOT / name
        if source.suffix == '.ps1':
            replacements[name] = source.read_text(encoding='utf-8-sig').encode('utf-8-sig')
        else:
            replacements[name] = source.read_bytes()
    metadata = json.loads(replacements['tools/cloudflared/cloudflared-install.json'])
    expected = '4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6'
    if hashlib.sha256(replacements['tools/cloudflared/cloudflared.exe']).hexdigest() != expected:
        raise RuntimeError('Official cloudflared digest mismatch')
    with zipfile.ZipFile(BASE) as old:
        manifest = json.loads(old.read('deployment-manifest.json'))
        manifest['release'] = '1.2-cloudflare'
        manifest['patched_at'] = datetime.now(timezone.utc).isoformat()
        manifest['public_tunnel_options'] = ['ngrok', 'cloudflare_quick']
        for name, data in replacements.items():
            manifest['files'][name] = hashlib.sha256(data).hexdigest()
        replacements['deployment-manifest.json'] = json.dumps(manifest, indent=2).encode()
        with zipfile.ZipFile(PATCH, 'w' if args.replace_unpublished else 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as patch:
            for name, data in replacements.items():
                patch.writestr(name, data)
        with zipfile.ZipFile(FULL, 'w' if args.replace_unpublished else 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as full:
            for entry in old.infolist():
                full.writestr(entry, replacements.pop(entry.filename) if entry.filename in replacements else old.read(entry.filename))
            for name, data in replacements.items():
                full.writestr(name, data)
    for archive in (PATCH, FULL):
        with zipfile.ZipFile(archive) as package:
            if package.testzip() is not None:
                raise RuntimeError('Archive integrity check failed')
            if any(part in {'.env', '.worker.env', 'cluster.json', 'ngrok.yml', 'outputs', '__pycache__'}
                   for name in package.namelist() for part in name.split('/')):
                raise RuntimeError('Private configuration in release')
    with zipfile.ZipFile(FULL) as package:
        manifest = json.loads(package.read('deployment-manifest.json'))
        if set(package.namelist()) != set(manifest['files']) | {'deployment-manifest.json'} or not all(
                hashlib.sha256(package.read(name)).hexdigest() == value for name, value in manifest['files'].items()):
            raise RuntimeError('Manifest validation failed')
    report = {'patch': str(PATCH), 'patch_bytes': PATCH.stat().st_size, 'full_package': str(FULL),
              'full_bytes': FULL.stat().st_size, 'full_sha256': hashlib.sha256(FULL.read_bytes()).hexdigest(),
              'private_configuration_copied': False, 'all_file_hashes_verified': True,
              'cloudflared_sha256': expected, 'cloudflared_metadata_included': bool(metadata)}
    FULL.with_suffix('.build.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
