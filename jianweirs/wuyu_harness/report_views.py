"""Versioned report views from saved evidence, without changing audited outputs.

The caller must validate the run's ownership and completed state first. This
module reads only the authorized run directory, never a global asset store,
remote service, model, or user-supplied filesystem path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from threading import Lock

from .consumer_conclusion import package_consumer_conclusion


REPORT_NAMES = frozenset({'report.html', 'report.md', 'report.docx'})
_BUILD_LOCK = Lock()


class _SavedRunImages:
    def __init__(self, root: Path, output: Path, package: dict):
        self.root, self.output = root, output
        self.assets = {row['id']: row for row in package.get('assets', []) if isinstance(row, dict) and row.get('id')}
        self.paths = {}

    def run_dir(self, _run_id):
        return self.output

    def get(self, identity, kind):
        if kind != 'image' or identity not in self.assets or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity):
            raise ValueError('报告影像标识无效')
        for suffix in ('.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'):
            path = self.root / ('source-' + identity + suffix)
            if path.is_file() and not path.is_symlink() and path.resolve().parent == self.root:
                self.paths[identity] = path
                return {**self.assets[identity], 'file': identity + suffix}
        raise ValueError('已保存的报告原图缺失，无法重新排版')

    def asset_path(self, record):
        return self.paths[record['id']]


def _revision():
    # A code change invalidates views automatically, without migrating evidence.
    digest = hashlib.sha256(b'jianwei-report-view-v12')
    for name in ('report.py', 'docx_export.py', 'consumer_conclusion.py', 'task_results.py', 'task_outputs.py', 'task_modes.py', 'answer_layout.py', 'living_report.py', 'report_views.py'):
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def _child(parent: Path, name: str):
    path = parent / name
    if path.is_symlink() or path.resolve().parent != parent:
        raise ValueError('报告派生目录无效')
    return path


def current_report_view(run_dir: Path, filename: str) -> Path:
    """Return a cached current-format report while retaining original artifacts."""
    if filename not in REPORT_NAMES:
        raise ValueError('不支持的报告格式')
    root = Path(run_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('报告目录不存在')
    evidence = _child(root, 'finding-package.json')
    raw = evidence.read_bytes()
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('报告证据包超过排版大小上限')
    package = json.loads(raw)
    if not isinstance(package, dict):
        raise ValueError('报告证据包无效')
    version = hashlib.sha256(raw + _revision().encode()).hexdigest()
    base = _child(root, '.report-views')
    base.mkdir(exist_ok=True)
    target = _child(base, version)
    with _BUILD_LOCK:
        marker = target / 'view-manifest.json'
        if marker.is_file():
            manifest = json.loads(marker.read_text(encoding='utf-8'))
            result = _child(target, filename)
            if (manifest.get('evidence_sha256') == hashlib.sha256(raw).hexdigest()
                    and result.is_file() and hashlib.sha256(result.read_bytes()).hexdigest() == manifest.get('files', {}).get(filename)):
                return result
            raise ValueError('已缓存报告校验失败，请联系管理员检查')
        if target.exists():
            raise ValueError('派生报告目录尚未完成，请稍后重试')
        temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=base)).resolve()
        try:
            from .report import render_report
            from .docx_export import export_report_docx
            images = _SavedRunImages(root, temporary, package)
            render_report(package, images, root.name, tolerate_missing_saved_images=True)
            export_report_docx(package, temporary)
            # HTML links still resolve through the original owner-checked
            # artifact URLs; Word embeds its own images. Do not retain a second
            # image set or full evidence package in this presentation cache.
            for child in temporary.iterdir():
                if child.name not in REPORT_NAMES and child.is_file() and not child.is_symlink():
                    child.unlink()
            manifest = {'schema': 'jianwei-report-view-v12', 'evidence_sha256': hashlib.sha256(raw).hexdigest(),
                        'renderer_revision': _revision(), 'conclusion': package_consumer_conclusion(package),
                        'files': {name: hashlib.sha256((temporary / name).read_bytes()).hexdigest() for name in REPORT_NAMES},
                        'models_called': False, 'imagery_reacquired': False, 'original_artifacts_replaced': False}
            (temporary / 'view-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.rename(target)
        finally:
            if temporary.exists() and temporary.parent == base and temporary.name.startswith('.building-'):
                shutil.rmtree(temporary)
    return _child(target, filename)
