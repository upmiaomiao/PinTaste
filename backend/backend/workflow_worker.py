"""Single-owner durable work/PPT worker. Does not run HTTP or database migrations."""
import argparse
import json
from pathlib import Path
import tempfile
import os
import signal
import threading
import time

from workflow_execution import (execution_mode,heartbeat_ready,state_directory,
                                 worker_owner,write_heartbeat)


def load_app():
    from app import app
    return app


def terminal_count(service):
    value = service.terminal_status()
    count = value.get('pending_terminal')
    if type(count) is not int or count < 0:
        raise RuntimeError('Workflow terminal observation is invalid')
    return count


def terminal_attempt_count(service):
    count = service.terminal_status().get('active_terminal_attempts')
    if type(count) is not int or count < 0:
        raise RuntimeError('Workflow terminal attempt observation is invalid')
    return count


def active_jobs(service):
    with service.future_lock:
        return (any(not future.done() for future in service.futures.values())
                or bool(service.terminal_owner.occupied()))



def write_capacity_snapshot(service, directory):
    """Numeric observation from the live executor, without importing a second app."""
    app = getattr(service, 'app', None)
    workers = getattr(getattr(service, 'executor', None), '_max_workers', None)
    if app is None or type(workers) is not int:
        return False
    from services.wuyu_resource_limits import memory_admission, resources
    limits = resources(app)
    body = {'schema_version': 1, 'pid': os.getpid(), 'recorded_at': time.time(),
            'executor_workers': workers,
            'per_actor_jobs': int(app.config.get('WUYU_WORKFLOW_PER_ACTOR', 0)),
            'local_export_workers': int(app.config.get('WUYU_LOCAL_EXPORT_WORKERS', 0)),
            'page_resource_bytes': limits.budget.capacity if limits.budget else 0,
            'page_resource_used': limits.budget.used if limits.budget else 0,
            'memory_admission': memory_admission(app.config),
            'pending_terminal': terminal_count(service),
            'active_terminal_attempts': terminal_attempt_count(service)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.capacity-',
                                         dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(body, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(Path(directory) / 'workflow-capacity.json')
        return True
    except OSError:
        return False  # Optional observation must not interrupt durable jobs.
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def serve(factory=None,drain_seconds=30):
    if execution_mode({'WUYU_WORKFLOW_EXECUTION_MODE':os.environ.get('WUYU_WORKFLOW_EXECUTION_MODE','combined')})!='worker':
        raise RuntimeError('Standalone workflow command requires worker mode')
    if os.environ.get('WUYU_MIGRATION_PROCESS')=='1':
        raise RuntimeError('Workflow worker cannot run as a migration process')
    stopped=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,lambda *_:stopped.set())
    directory=state_directory()
    # Lock before importing app: its factory performs workflow recovery.
    with worker_owner(directory):
        write_heartbeat('starting',directory)
        service=None
        exit_code=1
        try:
            app=(factory or load_app)()
            service=app.extensions['wuyu_workflow']
            service.start_worker()
            if service.execution_mode!='worker' or not service.scheduler or not service.scheduler.is_alive():
                raise RuntimeError('Workflow scheduler did not start in worker mode')
            while not stopped.is_set():
                if not service.scheduler.is_alive():
                    raise RuntimeError('Workflow scheduler stopped unexpectedly')
                write_heartbeat('ready',directory,pending_terminal=terminal_count(service),active_terminal_attempts=terminal_attempt_count(service))
                write_capacity_snapshot(service,directory)
                stopped.wait(2)
            exit_code=0
        finally:
            try:
                if service is not None:
                    service.shutdown()
                    deadline=time.monotonic()+drain_seconds
                    while active_jobs(service) and time.monotonic()<deadline:
                        write_heartbeat('stopping',directory,pending_terminal=terminal_count(service),active_terminal_attempts=terminal_attempt_count(service))
                        with app.app_context():
                            service.terminal_owner.retry_due()
                        time.sleep(.1)
                    # The provider drain deadline is not permission to kill a started DB/charge txn.
                    while terminal_attempt_count(service):
                        write_heartbeat('stopping',directory,pending_terminal=terminal_count(service),active_terminal_attempts=terminal_attempt_count(service))
                        time.sleep(.1)
            finally:
                try:
                    write_heartbeat('stopped',directory,pending_terminal=terminal_count(service) if service is not None else 0,active_terminal_attempts=terminal_attempt_count(service) if service is not None else 0)
                finally:
                    if service is not None and active_jobs(service):
                        # Never release the lock while an uncancellable provider thread survives.
                        # The next owner marks these jobs interrupted; it does not replay them.
                        os._exit(exit_code)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-health',action='store_true')
    args=parser.parse_args()
    if args.check_health:raise SystemExit(0 if heartbeat_ready() else 1)
    serve()


if __name__=='__main__':
    main()
