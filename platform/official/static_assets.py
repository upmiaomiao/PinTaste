"""Cache only verified immutable public build outputs; all other responses stay private."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

NO_STORE = 'no-store'
IMMUTABLE = 'public, max-age=31536000, immutable'
ASSET_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,}\.(?:js|css|png|webp|svg|jpg|jpeg|gif|ico|avif|woff|woff2|ttf|otf|wasm)')


def _identity(path):
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('PUBLIC_ASSET_NOT_REGULAR')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class PublicAssetCache:
    """Verify a release manifest once; refuse caching after any filesystem drift.

    A missing manifest is compatible with old/development distributions: it simply
    leaves caching disabled. No database, network, cookie or user state is read.
    """
    def __init__(self, dist):
        self.root = Path(dist) / 'assets'
        self._approved = {}
        self._root_identity = None
        try:
            manifest = Path(dist) / 'static-assets.json'
            if manifest.is_symlink() or self.root.is_symlink() or manifest.stat().st_size > 2_000_000:
                return
            value = json.loads(manifest.read_bytes())
            if (set(value) != {'version', 'files'} or value['version'] != 1
                    or not isinstance(value['files'], dict) or len(value['files']) > 1000):
                return
            approved = {}
            for name, record in value['files'].items():
                if (not isinstance(name, str) or not ASSET_NAME.fullmatch(name)
                        or not isinstance(record, dict) or set(record) != {'sha256', 'bytes'}
                        or not isinstance(record['sha256'], str)
                        or not re.fullmatch('[a-f0-9]{64}', record['sha256'])
                        or type(record['bytes']) is not int or record['bytes'] < 0):
                    return
                path = self.root / name
                identity = _identity(path)
                if identity[2] != record['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                    return
                approved['/assets/' + name] = identity
            directory = self.root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(directory.st_mode):
                return
            self._root_identity = (directory.st_dev, directory.st_ino)
            self._approved = approved
        except (OSError, ValueError, TypeError):
            pass  # Fail closed; existing distributions still serve with no-store.

    def cache_control(self, request, response):
        if request.method not in {'GET', 'HEAD'} or response.status_code not in {200, 304}:
            return NO_STORE
        path = request.url.path
        identity = self._approved.get(path)
        if identity is None or 'set-cookie' in response.headers:
            return NO_STORE
        vary = {part.strip().lower() for part in response.headers.get('vary', '').split(',')}
        if vary & {'*', 'cookie', 'authorization'} or 'content-disposition' in response.headers:
            return NO_STORE
        try:
            directory = self.root.stat(follow_symlinks=False)
            if (not stat.S_ISDIR(directory.st_mode)
                    or (directory.st_dev, directory.st_ino) != self._root_identity
                    or _identity(self.root / path.removeprefix('/assets/')) != identity):
                return NO_STORE
        except (OSError, ValueError):
            return NO_STORE
        return IMMUTABLE
