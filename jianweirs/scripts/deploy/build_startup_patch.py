"""Patch the already-verified portable ZIP without copying any live configuration."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/deployment'
BASE = OUT / 'omap-public-windows-v1-20261018.zip'
PATCH = OUT / 'omap-startup-fix-20261018.zip'
FULL = OUT / 'omap-public-windows-v1.1-20261018.zip'
FILES = ['deploy-public.ps1', 'scripts/deploy/setup-worker.ps1', 'scripts/deploy/probe_runtime.py']


def main():
    original = json.loads(BASE.with_suffix('.build.json').read_text(encoding='utf-8'))
    if hashlib.sha256(BASE.read_bytes()).hexdigest() != original['sha256']:
        raise RuntimeError('Base package hash mismatch')
    if PATCH.exists() or FULL.exists():
        raise RuntimeError('Use a new release name; existing archives are never overwritten')
    replacements = {}
    for name in FILES:
        source = (ROOT / name).read_text(encoding='utf-8-sig')
        replacements[name] = source.encode('utf-8-sig' if name.endswith('.ps1') else 'utf-8')
    note = ('Windows PowerShell 5.1 启动修复\r\n\r\n'
            '将本补丁中的 deploy-public.ps1、scripts 文件夹和 deployment-manifest.json 复制到原部署目录，选择替换同名文件。\r\n'
            'scripts 文件夹需要合并到原 scripts，不要另建一层。\r\n'
            '然后重新双击原目录的 DEPLOY-PUBLIC.cmd。\r\n'
            '已有 .worker.env、cluster.json、ngrok.yml 和网站后端接入配置会保留。\r\n\r\n'
            '修复内容：把 Python -c 内联探针改为脚本文件，避免 Windows PowerShell 5.1 吞掉字符串引号。\r\n'
            '新增 -CheckOnly 参数用于无密钥、无网络发布的运行环境检查。\r\n')
    replacements['STARTUP-FIX.txt'] = note.encode('utf-8-sig')
    with zipfile.ZipFile(BASE) as old:
        manifest = json.loads(old.read('deployment-manifest.json'))
        manifest['release'] = '1.1'
        manifest['patched_at'] = datetime.now(timezone.utc).isoformat()
        manifest['fix'] = 'Windows PowerShell 5.1 native argument quoting'
        for name, data in replacements.items():
            manifest['files'][name] = hashlib.sha256(data).hexdigest()
        replacements['deployment-manifest.json'] = json.dumps(manifest, indent=2).encode('utf-8')
        with zipfile.ZipFile(PATCH, 'x', zipfile.ZIP_DEFLATED) as patch:
            for name, data in replacements.items():
                patch.writestr(name, data)
        with zipfile.ZipFile(FULL, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as full:
            for entry in old.infolist():
                full.writestr(entry, replacements.pop(entry.filename) if entry.filename in replacements else old.read(entry.filename))
            for name, data in replacements.items():
                full.writestr(name, data)
    with zipfile.ZipFile(FULL) as package:
        if package.testzip() is not None:
            raise RuntimeError('ZIP integrity failed')
        updated = json.loads(package.read('deployment-manifest.json'))
        if not all(hashlib.sha256(package.read(name)).hexdigest() == digest for name, digest in updated['files'].items()):
            raise RuntimeError('Updated file hash failed')
        if set(package.namelist()) != set(updated['files']) | {'deployment-manifest.json'}:
            raise RuntimeError('Unexpected ZIP entries')
    report = {'patch': str(PATCH), 'patch_bytes': PATCH.stat().st_size,
              'full_package': str(FULL), 'full_bytes': FULL.stat().st_size,
              'full_sha256': hashlib.sha256(FULL.read_bytes()).hexdigest(),
              'private_configuration_copied': False, 'updated_manifest_verified': True}
    FULL.with_suffix('.build.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
