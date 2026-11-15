"""Build a small source/alignment overlay; never package machine configuration."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'outputs/deployment'
FILES = ['UPDATE-SOURCES.cmd','图源与偏移更新说明.txt','PUBLIC_API_QUICKSTART.md',
    'omap_api/optical.py','omap_api/optical_coordinates.py','omap_api/registration.py',
    'omap_api/image_quality.py','omap_api/gateway.py','omap_api/spectral.py',
    'omap_api/sar.py','omap_api/spatial_metadata.py',
    'scripts/deploy/update-sources.ps1','scripts/deploy/expand_sources.py',
    'scripts/deploy/verify_source_alignment.py','scripts/deploy/verify_public_api.py',
    'scripts/deploy/setup-worker.ps1','scripts/deploy/worker-common.ps1']


def main():
    base = OUT/'omap-public-windows-v1.2-cloudflare-20261018.zip'
    info = json.loads(base.with_suffix('.build.json').read_text())
    if hashlib.sha256(base.read_bytes()).hexdigest() != info['full_sha256']:
        raise ValueError('Base package hash mismatch')
    with zipfile.ZipFile(base) as z:
        manifest = json.loads(z.read('deployment-manifest.json'))
    files = {}
    for name in FILES:
        p = ROOT/name
        data = p.read_text(encoding='utf-8-sig').encode('utf-8-sig') if p.suffix=='.ps1' else p.read_bytes()
        files[name] = data
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    manifest.update(release='1.3-sources-alignment',patched_at=datetime.now(timezone.utc).isoformat())
    files['deployment-manifest.json'] = json.dumps(manifest,indent=2).encode()
    target = OUT/'omap-sources-alignment-20261022.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():
            z.writestr(name,data)
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        assert set(z.namelist())==set(files)
        for name in z.namelist():
            assert not {'.env','.worker.env','cluster.json','ngrok.yml','__pycache__','outputs'}.intersection(name.split('/'))
    report={'patch':str(target),'bytes':target.stat().st_size,
        'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'base_release':'1.2-cloudflare','remote_applied':False,
        'private_configuration_included':False,'files':FILES}
    target.with_suffix('.build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':
    main()
