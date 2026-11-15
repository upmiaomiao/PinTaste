"""Opt-in COS copies for completed exports; never evicts or deletes originals.

The JSON catalog stays inside the project on the existing uploads volume. No
database migration, public cloud URL, new RPC, or automatic historical upload.
Callers must authorize the project before invoking this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, unquote


BUCKET = 'wuyu-1324734433'
HOST = BUCKET + '.cos.ap-beijing.myqcloud.com'
CONFIG_PATH = '/run/wuyu-cos/settings.json'
KINDS = frozenset({'pptx', 'image_pptx', 'hybrid_pptx', 'pdf', 'images_zip', 'export_preview'})
NEW_EXPORT_KINDS = KINDS - {'export_preview'}
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')
_HASH = re.compile(r'[a-f0-9]{64}')


class StorageError(ValueError):
    """Only fixed diagnostic codes, never CLI output or signed request URLs."""


class ObjectMissing(StorageError):
    pass


@dataclass(frozen=True)
class Settings:
    projects: frozenset[str]
    prefix: str
    cli: str = '/run/wuyu-cos/coscli'
    credentials: str = '/run/wuyu-cos/cos.yaml'
    storage_class: str = 'MAZ_STANDARD'
    max_bytes: int = 64 * 1024
    reserve_bytes: int = 32 * 1024 * 1024
    deadline: float = 15.0
    lock_wait: float = 2.0
    not_before: str | None = None

    def __post_init__(self):
        if ((not self.projects and self.not_before is None) or len(self.projects) > 2
                or any(not isinstance(p, str) or not _UUID.fullmatch(p) for p in self.projects)
                or not re.fullmatch(r'wuyu-web/staging/ppt-assets/[a-f0-9]{32}', self.prefix)
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= (32 * 1024 * 1024 if self.not_before else 64 * 1024)
                or not 0 < self.deadline <= 20 or not 0 < self.lock_wait <= 3
                or self.reserve_bytes < 0):
            raise StorageError('COS_CONFIG_INVALID')
        if self.storage_class != 'MAZ_STANDARD':
            raise StorageError('COS_STORAGE_CLASS_INVALID')
        if self.not_before is not None and _timestamp(self.not_before) is None:
            raise StorageError('COS_CONFIG_INVALID')


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


class CosCliClient:
    """CLI upload has no retries/logs; GET has a size and wall-clock bound."""

    def __init__(self, settings):
        self.settings = settings

    def _run(self, args, timeout):
        try:
            result = subprocess.run(
                [self.settings.cli, '--config-path', self.settings.credentials,
                 '--disable-log', '--init-skip', '--protocol', 'https',
                 '--endpoint', 'cos.ap-beijing.myqcloud.com',
                 '--close_auto_switch_host', 'true', *args],
                capture_output=True, timeout=timeout, check=False,
                env={**os.environ, 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
                     'http_proxy': '', 'https_proxy': '', 'all_proxy': ''})
        except (OSError, subprocess.TimeoutExpired):
            raise StorageError('COS_CLIENT_UNAVAILABLE') from None
        if result.returncode:
            raise StorageError('COS_CLIENT_REJECTED')
        return result.stdout

    def upload(self, key, source):
        # This CLI otherwise emits only "Error 1 files" on a failed transfer.
        # Keep diagnostics in a 0700 temporary directory, classify fixed COS
        # codes only, and destroy the raw diagnostics before returning.
        with tempfile.TemporaryDirectory(prefix='wuyu-cos-private-') as diagnostic:
            try:
                self._run(['cp', str(source), 'cos://wuyu/' + key, '--forbid-overwrite',
                           '--acl', 'private', '--storage-class', self.settings.storage_class,
                           '--err-retry-num', '0', '--retry-num', '0',
                           '--process-log=false', '--fail-output=true', '--fail-output-path', diagnostic,
                           '--check-point=false', '--routines', '1', '--thread-num', '1'], timeout=20)
            except StorageError:
                codes = ('SAZOperationNotSupportOnMAZBucket', 'OAZOperationNotSupportOnMAZBucket',
                         'UserAccessDenied', 'SignatureDoesNotMatch', 'SignatureExpired',
                         'InvalidAccessKeyId', 'InvalidArgument', 'InvalidDigest', 'BadDigest',
                         'RequestTimeout', 'AccessDenied', 'NoSuchBucket', 'ForbidOverwrite')
                for path in Path(diagnostic).rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        with path.open('rb') as source_log:
                            text = source_log.read(64 * 1024).decode('utf-8', errors='replace')
                        for code in codes:
                            if code in text:
                                raise StorageError('COS_UPLOAD_' + code.upper()) from None
                raise

    def download(self, key, destination, expected_bytes, expected_sha):
        started = time.monotonic()
        raw = self._run(['signurl', 'cos://wuyu/' + key, '--time', '60', '--simple-output'], timeout=3)
        try:
            url = urlsplit(raw.decode('utf-8').strip())
            if (url.scheme != 'https' or url.netloc != HOST or url.fragment
                    or unquote(url.path) != '/' + key or not url.query
                    or '\r' in url.query or '\n' in url.query):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise StorageError('COS_SIGNING_INVALID') from None
        # http.client does not log request URLs; do not hand presigned URLs to
        # libraries whose INFO request log includes the signature query string.
        connection = http.client.HTTPSConnection(HOST, timeout=2, context=ssl.create_default_context())
        try:
            connection.request('GET', url.path + '?' + url.query, headers={'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status == 404:
                error = response.read(4096)
                if b'<Code>NoSuchKey</Code>' in error:
                    raise ObjectMissing('COS_OBJECT_MISSING')
                raise StorageError('COS_READ_REJECTED')
            if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
                raise StorageError('COS_READ_REJECTED')
            length = response.getheader('Content-Length')
            if length is not None and length != str(expected_bytes):
                raise StorageError('COS_SIZE_MISMATCH')
            count, digest = 0, hashlib.sha256()
            with Path(destination).open('wb') as out:
                while True:
                    if time.monotonic() - started > self.settings.deadline:
                        raise StorageError('COS_READ_TIMEOUT')
                    block = response.read1(64 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > expected_bytes:
                        raise StorageError('COS_SIZE_MISMATCH')
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if count != expected_bytes or digest.hexdigest() != expected_sha:
                raise StorageError('COS_INTEGRITY_FAILED')
        except StorageError:
            raise
        except (OSError, ValueError, http.client.HTTPException, socket.timeout):
            raise StorageError('COS_READ_UNAVAILABLE') from None
        finally:
            connection.close()


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(64 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class ArtifactStore:
    def __init__(self, root, settings, client=None):
        self.root = Path(root).resolve()
        self.settings = settings
        self.client = client or CosCliClient(settings)

    def lifecycle(self):
        from services.wuyu_cos_lifecycle import ArtifactLifecycle
        return ArtifactLifecycle(self)

    @classmethod
    def from_app(cls, app):
        # There is deliberately no generic environment switch: live credentials
        # and the tiny canary project allowlist are server-private deployment data.
        config = Path(app.config.get('WUYU_COS_STORAGE_CONFIG', CONFIG_PATH))
        if not config.exists():
            return None
        try:
            if config.is_symlink() or config.stat().st_size > 8192:
                raise ValueError()
            data = json.loads(config.read_text(encoding='utf-8'))
            if data.get('enabled') is not True:
                return None
            if set(data) not in ({'enabled', 'projects', 'prefix'}, {'enabled', 'projects', 'prefix', 'new_exports_after'}) or not isinstance(data['projects'], list):
                raise ValueError()
            cutoff = data.get('new_exports_after')
            if 'new_exports_after' in data and cutoff is None:
                raise ValueError()
            settings = Settings(frozenset(data['projects']), data['prefix'], not_before=cutoff,
                max_bytes=32 * 1024 * 1024 if cutoff else 64 * 1024)
            return cls(app.config['UPLOAD_FOLDER'], settings)
        except (OSError, ValueError, TypeError, KeyError):
            raise StorageError('COS_CONFIG_INVALID') from None

    def eligible(self, project, item):
        if not isinstance(project, str) or not _UUID.fullmatch(project) or not isinstance(item, dict):
            return False
        if project in self.settings.projects and type(item.get('bytes')) is int and item['bytes'] <= 64 * 1024:
            return item.get('kind') in KINDS
        created = _timestamp(item.get('created_at'))
        return (self.settings.not_before is not None and item.get('kind') in NEW_EXPORT_KINDS
            and created is not None and _timestamp(self.settings.not_before) <= created <= time.time() + 120
            and type(item.get('bytes')) is int and 1 <= item['bytes'] <= self.settings.max_bytes)

    def _asset(self, project, item):
        if (not self.eligible(project, item)
                or not isinstance(item, dict) or not _UUID.fullmatch(str(item.get('id', '')))
                or item.get('kind') not in KINDS or not _HASH.fullmatch(str(item.get('sha256', '')))
                or type(item.get('bytes')) is not int or not 1 <= item['bytes'] <= self.settings.max_bytes):
            raise StorageError('COS_ASSET_NOT_ELIGIBLE')
        relative = item.get('relative_path')
        if not isinstance(relative, str) or '\\' in relative:
            raise StorageError('COS_PATH_INVALID')
        parts = PurePosixPath(relative).parts
        if (len(parts) < 2 or parts[0] != project or any(p in {'.', '..'} for p in parts)
                or PurePosixPath(relative).is_absolute()):
            raise StorageError('COS_PATH_INVALID')
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise StorageError('COS_PATH_INVALID')
        project_root = self.root / project
        if not project_root.is_dir() or project_root.is_symlink() or not path.is_relative_to(project_root):
            raise StorageError('COS_PATH_INVALID')
        catalog = project_root / '.cos-assets'
        if catalog.is_symlink():
            raise StorageError('COS_PATH_INVALID')
        key = f'{self.settings.prefix}/{project}/{item["id"]}/{item["sha256"]}'
        descriptor = {'version': 1, 'project': project, 'artifact': item['id'],
                      'bytes': item['bytes'], 'sha256': item['sha256'], 'key': key,
                      'bucket': BUCKET, 'storage_class': self.settings.storage_class, 'state': 'verified'}
        return path, catalog, descriptor

    @contextmanager
    def _lock(self, catalog, artifact):
        import fcntl
        catalog.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(catalog / (artifact + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            until = time.monotonic() + self.settings.lock_wait
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise StorageError('COS_ASSET_BUSY')
                    time.sleep(0.02)
            yield
        finally:
            os.close(descriptor)

    @contextmanager
    def _temporary(self, catalog):
        fd, name = tempfile.mkstemp(prefix='.copy-', dir=catalog)
        os.close(fd)
        path = Path(name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def _matches(self, path, item):
        return path.is_file() and path.stat().st_size == item['bytes'] and _digest(path) == item['sha256']

    def _read_catalog(self, catalog, item):
        path = catalog / (item['id'] + '.json')
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 4096:
            raise StorageError('COS_CATALOG_INVALID')
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise StorageError('COS_CATALOG_INVALID') from None

    def _space(self, catalog, expected_bytes):
        # Two temporary copies can coexist during copy verification.
        if shutil.disk_usage(catalog).free < self.settings.reserve_bytes + expected_bytes * 2:
            raise StorageError('COS_CACHE_CAPACITY')

    def mirror(self, project, item):
        path, catalog, descriptor = self._asset(project, item)
        with self._lock(catalog, item['id']):
            self.lifecycle().assert_live_locked(catalog, item)
            existing = self._read_catalog(catalog, item)
            if existing == descriptor:
                return descriptor
            if existing is not None:
                raise StorageError('COS_CATALOG_INVALID')
            if not self._matches(path, item):
                raise StorageError('COS_LOCAL_INTEGRITY_FAILED')
            self._space(catalog, item['bytes'])
            with self._temporary(catalog) as snapshot, self._temporary(catalog) as verified:
                shutil.copyfile(path, snapshot)
                if not self._matches(snapshot, item):
                    raise StorageError('COS_LOCAL_INTEGRITY_FAILED')
                try:
                    # Resume after a crash between PUT and catalog commit without
                    # overwriting the immutable object or charging another PUT.
                    self.client.download(descriptor['key'], verified, item['bytes'], item['sha256'])
                except ObjectMissing:
                    self.client.upload(descriptor['key'], snapshot)
                    self.client.download(descriptor['key'], verified, item['bytes'], item['sha256'])
                if not self._matches(verified, item):
                    raise StorageError('COS_INTEGRITY_FAILED')
                # A removed project must not be recreated by background copy work.
                if not self._matches(path, item):
                    raise StorageError('COS_LOCAL_INTEGRITY_FAILED')
                with self._temporary(catalog) as pending:
                    with pending.open('w', encoding='utf-8') as out:
                        json.dump(descriptor, out, sort_keys=True)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(pending, catalog / (item['id'] + '.json'))
                    directory_fd = os.open(catalog, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            return descriptor

    def restore(self, project, item):
        path, catalog, descriptor = self._asset(project, item)
        with self._lock(catalog, item['id']):
            self.lifecycle().assert_live_locked(catalog, item)
            if path.is_file():
                return path
            if self._read_catalog(catalog, item) != descriptor:
                raise StorageError('COS_COPY_NOT_VERIFIED')
            if not path.parent.is_dir():
                # No mkdir for a deleted dependency/project.
                raise StorageError('COS_PATH_INVALID')
            self._space(catalog, item['bytes'])
            with self._temporary(catalog) as pending:
                self.client.download(descriptor['key'], pending, item['bytes'], item['sha256'])
                if not self._matches(pending, item):
                    raise StorageError('COS_INTEGRITY_FAILED')
                # Re-check symlinks and project scope after network I/O.
                checked_path, _, _ = self._asset(project, item)
                if checked_path != path or not path.parent.is_dir():
                    raise StorageError('COS_PATH_INVALID')
                if path.exists():
                    if self._matches(path, item):
                        return path
                    raise StorageError('COS_LOCAL_CONFLICT')
                os.replace(pending, path)
            return path
