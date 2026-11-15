"""Standalone durable workflow ownership; no imports that initialize the app/DB."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import time

_owner=None
HEARTBEAT_MAX_AGE=15


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
