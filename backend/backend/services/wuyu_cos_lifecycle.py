"""Private per-artifact lifecycle bookkeeping. No autonomous cloud consumer or eviction.

Callers must retain existing project authorization. All state shares ArtifactStore's
cross-process lock; queue consumption requires an explicit cloud-write opt-in.
"""
from contextlib import contextmanager
import json
import math
import os
import time
import uuid

from services.wuyu_cos_artifacts import StorageError


class ArtifactLifecycle:
    def __init__(self, store):
        self.store = store

    def _load(self, catalog, item):
        path = catalog / (item['id'] + '.state.json')
        if not path.exists() and not path.is_symlink():
            return {'version': 1, 'sha256': item['sha256'], 'accesses': 0, 'last_access': None,
                'pinned': False, 'leases': {}, 'deleted': False, 'queue': 'none',
                'attempts': 0, 'claim': None, 'claim_until': 0, 'retry_at': 0}
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            expected = {'version', 'sha256', 'accesses', 'last_access', 'pinned', 'leases',
                'deleted', 'queue', 'attempts', 'claim', 'claim_until', 'retry_at'}
            if (not isinstance(value, dict) or set(value) != expected or type(value['version']) is not int or value['version'] != 1
                    or value['sha256'] != item['sha256'] or type(value['accesses']) is not int
                    or value['accesses'] < 0 or type(value['pinned']) is not bool
                    or type(value['deleted']) is not bool or not isinstance(value['leases'], dict)
                    or len(value['leases']) > 64 or value['queue'] not in
                    {'none', 'pending', 'running', 'verified', 'failed', 'cancelled'}
                    or type(value['attempts']) is not int or not 0 <= value['attempts'] <= 3
                    or value['claim'] is not None and (not isinstance(value['claim'], str)
                        or len(value['claim']) != 32 or any(c not in '0123456789abcdef' for c in value['claim']))):
                raise ValueError()
            for number in [value['claim_until'], value['retry_at'], *value['leases'].values()]:
                if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                    raise ValueError()
            if (value['queue'] == 'running' and (value['claim'] is None or value['attempts'] < 1)
                    or value['queue'] != 'running' and value['claim'] is not None
                    or value['deleted'] and value['queue'] != 'cancelled'):
                raise ValueError()
            if value['last_access'] is not None and (type(value['last_access']) not in (int, float)
                    or not math.isfinite(value['last_access']) or value['last_access'] < 0):
                raise ValueError()
            if any(not isinstance(k, str) or len(k) != 32 or any(c not in '0123456789abcdef' for c in k)
                    for k in value['leases']):
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise StorageError('COS_STATE_INVALID') from None

    def _save(self, catalog, item, state):
        if not catalog.parent.is_dir() or catalog.parent.is_symlink():
            raise StorageError('COS_PATH_INVALID')
        with self.store._temporary(catalog) as pending:
            with pending.open('w', encoding='utf-8') as out:
                json.dump(state, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(pending, catalog / (item['id'] + '.state.json'))
        fd = os.open(catalog, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _state(self, project, item, *, allow_deleted=False):
        _, catalog, _ = self.store._asset(project, item)
        with self.store._lock(catalog, item['id']):
            state = self._load(catalog, item)
            if state['deleted'] and not allow_deleted:
                raise StorageError('COS_ASSET_DELETED')
            yield catalog, state

    def assert_live_locked(self, catalog, item):
        """For mirror/restore while their artifact lock is already held."""
        if self._load(catalog, item)['deleted']:
            raise StorageError('COS_ASSET_DELETED')

    def accessed(self, project, item):
        """Authorized successful file resolution, not an inferred filesystem atime."""
        with self._state(project, item) as (catalog, state):
            state['accesses'] += 1
            state['last_access'] = time.time()
            if state['queue'] == 'none':
                _, _, descriptor = self.store._asset(project, item)
                state['queue'] = 'verified' if self.store._read_catalog(catalog, item) == descriptor else 'pending'
            self._save(catalog, item, state)

    def pin(self, project, item, pinned):
        if type(pinned) is not bool:
            raise StorageError('COS_STATE_INVALID')
        with self._state(project, item) as (catalog, state):
            state['pinned'] = pinned
            self._save(catalog, item, state)

    def enqueue(self, project, item):
        """Generation discovery is not a user access; do not inflate hotness."""
        with self._state(project, item) as (catalog, state):
            if state['queue'] == 'none':
                _, _, descriptor = self.store._asset(project, item)
                state['queue'] = 'verified' if self.store._read_catalog(catalog, item) == descriptor else 'pending'
                self._save(catalog, item, state)

    @contextmanager
    def using(self, project, item, seconds=60):
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise StorageError('COS_LEASE_INVALID')
        token = uuid.uuid4().hex
        with self._state(project, item) as (catalog, state):
            now = time.time()
            state['leases'] = {k: v for k, v in state['leases'].items() if v > now}
            if len(state['leases']) >= 64:
                raise StorageError('COS_ASSET_BUSY')
            state['leases'][token] = now + seconds
            self._save(catalog, item, state)
        try:
            yield token
        finally:
            with self._state(project, item, allow_deleted=True) as (catalog, state):
                state['leases'].pop(token, None)
                self._save(catalog, item, state)

    def tombstone(self, project, item):
        """Cancel future copies/restores; not a claim that cloud deletion has finished."""
        with self._state(project, item, allow_deleted=True) as (catalog, state):
            state.update(deleted=True, queue='cancelled', claim=None, claim_until=0)
            self._save(catalog, item, state)

    def inspect(self, project, item):
        with self._state(project, item, allow_deleted=True) as (_, state):
            return dict(state, leases=dict(state['leases']))

    def process_one(self, project, item, *, allow_cloud=False, before_attempt=None):
        """One bounded queue attempt; production wiring intentionally has no caller."""
        if allow_cloud is not True:
            return False
        token = uuid.uuid4().hex
        with self._state(project, item) as (catalog, state):
            now = time.time()
            if state['queue'] == 'running' and state['claim_until'] <= now and state['attempts'] >= 3:
                state.update(queue='failed', claim=None, claim_until=0)
                self._save(catalog, item, state)
                return False
            if (state['queue'] not in {'pending', 'running'} or state['attempts'] >= 3
                    or state['retry_at'] > now or state['claim_until'] > now
                    or any(v > now for v in state['leases'].values())):
                return False
            if before_attempt is not None and not before_attempt(item['bytes']):
                return False
            state.update(queue='running', claim=token, claim_until=now + 120,
                attempts=state['attempts'] + 1)
            self._save(catalog, item, state)
        try:
            self.store.mirror(project, item)
        except (StorageError, OSError):
            with self._state(project, item, allow_deleted=True) as (catalog, state):
                if state['claim'] == token and not state['deleted']:
                    state.update(queue='failed' if state['attempts'] >= 3 else 'pending',
                        claim=None, claim_until=0, retry_at=time.time() + 30 * 2 ** (state['attempts'] - 1))
                    self._save(catalog, item, state)
            return False
        with self._state(project, item, allow_deleted=True) as (catalog, state):
            if state['claim'] != token or state['deleted']:
                return False
            state.update(queue='verified', claim=None, claim_until=0, retry_at=0)
            self._save(catalog, item, state)
        return True
