"""Standalone durable workflow ownership; no imports that initialize the app/DB."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import time

_owner=None
HEARTBEAT_MAX_AGE=15
ADMISSION_SCOPE_FILENAME = 'workflow-admission-scope.json'
ADMISSION_SCOPE_MAX_BYTES = 65536
ADMISSION_SCOPE_MAX_SECONDS = 86400


@dataclass(frozen=True)
class WorkerAdmissionScope:
    """Operational admission only; it never changes persisted job state."""
    status: str
    observed_at: float
    allowed_job_ids: frozenset = field(default_factory=frozenset, repr=False)
    issued_at: float | None = None
    valid_until: float | None = None
    authorization_reference_sha256: str | None = field(default=None, repr=False)

    def allows(self, job_id):
        return self.status == 'unrestricted' or (
            self.status == 'restricted' and job_id in self.allowed_job_ids)

    def public_snapshot(self):
        # Do not expose a job identifier, allow-list, or private approval reference.
        return {'status': self.status, 'observed_at': self.observed_at}


def _scope_private_file(metadata):
    return (stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_uid == 0 and metadata.st_nlink == 1
            and 0 < metadata.st_size <= ADMISSION_SCOPE_MAX_BYTES)


def _scope_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate admission scope field')
        value[key] = item
    return value


def load_worker_admission_scope(directory=None, now=None):
    """Read the optional root-owned maintenance scope, failing closed on doubt.

    Absence preserves normal scheduling. Once present, expiry, unsafe metadata,
    unreadable content or an invalid schema hold new claims only. Active work and
    terminal persistence do not consult this gate and continue draining.
    """
    now = time.time() if now is None else now
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        return WorkerAdmissionScope('hold', 0.0)
    directory = state_directory() if directory is None else Path(directory)
    path = directory / ADMISSION_SCOPE_FILENAME
    held = WorkerAdmissionScope('hold', now)
    descriptor = None
    try:
        before = os.lstat(path)
        if not _scope_private_file(before):
            return held
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        descriptor = os.open(path, flags)
        current = os.fstat(descriptor)
        if (not _scope_private_file(current)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            return held
        data = os.read(descriptor, ADMISSION_SCOPE_MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (len(data) != current.st_size or len(data) > ADMISSION_SCOPE_MAX_BYTES
                or not _scope_private_file(after)
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return held
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_scope_object)
        fields = {'schema_version', 'allowed_job_ids', 'issued_at', 'valid_until', 'authorization_reference_sha256'}
        if not isinstance(value, dict) or set(value) != fields:
            return held
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            return held
        identifiers = value['allowed_job_ids']
        pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        if (not isinstance(identifiers, list) or len(identifiers) > 256
                or any(not isinstance(ident, str) or re.fullmatch(pattern, ident) is None for ident in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            return held
        issued, expires = value['issued_at'], value['valid_until']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (issued, expires)):
            return held
        if not (0 <= issued <= now < expires and 0 < expires - issued <= ADMISSION_SCOPE_MAX_SECONDS):
            return held
        reference = value['authorization_reference_sha256']
        if not isinstance(reference, str) or re.fullmatch(r'[0-9a-f]{64}', reference) is None:
            return held
        return WorkerAdmissionScope('restricted', now, frozenset(identifiers), issued, expires, reference)
    except OSError as error:
        return WorkerAdmissionScope('unrestricted', now) if error.errno == errno.ENOENT else held
    except (ValueError, TypeError, UnicodeError):
        return held
    finally:
        if descriptor is not None:
            os.close(descriptor)


def worker_admission_block_reason(job_id, directory=None, now=None):
    scope = load_worker_admission_scope(directory, now)
    return None if scope.allows(job_id) else 'maintenance_hold'


def public_worker_admission_scope(directory=None, now=None):
    return load_worker_admission_scope(directory, now).public_snapshot()


def execution_mode(config):
    mode=config.get('WUYU_WORKFLOW_EXECUTION_MODE','combined')
    if mode not in {'combined','api','worker'}:
        raise ValueError('WUYU_WORKFLOW_EXECUTION_MODE must be combined, api or worker')
    if mode!='combined' and config.get('WUYU_WORKFLOW_INLINE'):
        raise ValueError('Standalone API/worker modes cannot execute workflow jobs inline')
    return mode


def state_directory():
    return Path(os.environ.get('WUYU_WORKFLOW_STATE_DIR',Path(__file__).resolve().parent/'instance')).resolve()


def owner_lock_held():
    return _owner is not None and _owner[0]==os.getpid()


@contextmanager
def worker_owner(directory=None):
    import fcntl  # Production worker runs on Linux; do not rely on container PIDs.
    global _owner
    if owner_lock_held():
        raise RuntimeError('Workflow worker already owns the execution lock')
    directory=state_directory() if directory is None else Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    handle=(directory/'workflow-worker.lock').open('a+b')
    try:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another workflow worker owns the execution lock') from None
        _owner=(os.getpid(),handle)
        yield
    finally:
        if _owner is not None and _owner[1] is handle:
            _owner=None
        handle.close()


def write_heartbeat(status,directory=None,*,pending_terminal=0,active_terminal_attempts=0):
    if (type(pending_terminal) is not int or pending_terminal < 0
            or type(active_terminal_attempts) is not int or active_terminal_attempts < 0):
        raise ValueError("Invalid pending terminal observation")
    directory=state_directory() if directory is None else Path(directory)
    path=directory/'workflow-worker.json'
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'status':status,'updated':time.time(),'pid':os.getpid(),'pending_terminal':pending_terminal,'active_terminal_attempts':active_terminal_attempts}))
    temporary.chmod(0o600)
    os.replace(temporary,path)




def worker_queue_block_reason(directory=None, now=None):
    """Public admission code from the live worker, never from the API's memory.

    The shared heartbeat and capacity observation must both be current and
    belong to the same process. This is a read-only projection; unknown data
    never proves that a queued task is executing or that memory is available.
    """
    directory = state_directory() if directory is None else Path(directory)
    now = time.time() if now is None else now
    try:
        heartbeat = json.loads((directory / 'workflow-worker.json').read_text())
        capacity = json.loads((directory / 'workflow-capacity.json').read_text())
        if not isinstance(heartbeat, dict) or not isinstance(capacity, dict):
            return 'worker_state_unknown'
        if heartbeat.get('status') != 'ready':
            return 'worker_state_unknown'
        if type(heartbeat.get('pid')) is not int or heartbeat['pid'] <= 0:
            return 'worker_state_unknown'
        if type(capacity.get('pid')) is not int or capacity['pid'] != heartbeat['pid']:
            return 'worker_state_unknown'
        if type(capacity.get('schema_version')) is not int or capacity['schema_version'] != 1:
            return 'worker_state_unknown'
        for value in (heartbeat.get('updated'), capacity.get('recorded_at')):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return 'worker_state_unknown'
            if not 0 <= now - value <= HEARTBEAT_MAX_AGE:
                return 'worker_state_unknown'
        if capacity['recorded_at'] < heartbeat['updated']:
            return 'worker_state_unknown'
        for key in ('pending_terminal', 'active_terminal_attempts'):
            if type(heartbeat.get(key)) is not int or heartbeat[key] < 0:
                return 'worker_state_unknown'
        admission = capacity.get('memory_admission')
        if not isinstance(admission, dict) or type(admission.get('allowed')) is not bool:
            return 'worker_state_unknown'
        if admission['allowed'] and admission.get('reason') in {'available', 'disabled'}:
            return None
        if not admission['allowed'] and admission.get('reason') == 'memory_wait':
            return 'memory_wait'
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 'worker_state_unknown'


def heartbeat_ready(directory=None,now=None):
    directory=state_directory() if directory is None else Path(directory)
    now=time.time() if now is None else now
    try:
        value=json.loads((directory/'workflow-worker.json').read_text())
        if not isinstance(value,dict):return False
        pending=value.get('pending_terminal')
        if type(pending) is not int or pending < 0:return False
        active=value.get('active_terminal_attempts')
        if type(active) is not int or active < 0:return False
        updated=value.get('updated')
        if isinstance(updated,bool) or not isinstance(updated,(int,float)) or not math.isfinite(updated):return False
        age=now-updated
        return value.get('status')=='ready' and math.isfinite(age) and 0<=age<=HEARTBEAT_MAX_AGE
    except (OSError,ValueError,TypeError,KeyError):
        return False
