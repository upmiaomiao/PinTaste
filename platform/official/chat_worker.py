"""Single-owner chat execution process; API replicas never acquire this lock."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import time

from . import store as db
from .runtime_config import settings as runtime

HEARTBEAT_INTERVAL=2
HEARTBEAT_MAX_AGE=15


def heartbeat_status(now=None):
    now = time.time() if now is None else now
    try:
        value = json.loads((db.DATA/'chat-worker.json').read_text())
        if not isinstance(value, dict) or value.get('status') not in {'ready', 'stopped'}:
            raise ValueError('Invalid heartbeat status')
        updated = value.get('updated')
        if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
            raise ValueError('Invalid heartbeat timestamp')
        age = now-updated
        if age < 0 or not math.isfinite(age):
            raise ValueError('Invalid heartbeat age')
        status = value['status']
        if status == 'ready' and age > HEARTBEAT_MAX_AGE:
            status = 'stale'
        return {'ready': status == 'ready', 'status': status, 'age_seconds': round(age, 3)}
    except FileNotFoundError:
        return {'ready': False, 'status': 'missing', 'age_seconds': None}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ready': False, 'status': 'invalid', 'age_seconds': None}


def heartbeat_ready():
    return heartbeat_status()['ready']


def write_heartbeat(status, pending_terminal=0):
    if type(pending_terminal) is not int or pending_terminal < 0:
        raise ValueError('Invalid terminal persistence count')
    path=db.DATA/'chat-worker.json'
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'status':status,'updated':time.time(),'pid':os.getpid(),'pending_terminal':pending_terminal}))
    temporary.chmod(0o600)
    os.replace(temporary,path)


async def serve(runner=None):
    if runtime.chat_execution_mode!='worker':
        raise RuntimeError('Chat worker requires WUYU_CHAT_EXECUTION_MODE=worker')
    db.DATA.mkdir(parents=True,exist_ok=True)
    # Acquire ownership before initialization or recovery, including on second-worker startup.
    with db.SingleProcess():
        from .runtime_bootstrap import initialize_runtime
        db.start_async_work()
        try:
            await db.run_sync(initialize_runtime)
            from . import document_rag
            from .jobs import Scheduler
            if runner is None:
                from .chat_execution import runner
            scheduler=Scheduler(runner)
            stopping=asyncio.Event()
            loop=asyncio.get_running_loop()
            for signum in (signal.SIGTERM,signal.SIGINT):
                try:
                    loop.add_signal_handler(signum,stopping.set)
                except NotImplementedError:
                    signal.signal(signum,lambda *_:loop.call_soon_threadsafe(stopping.set))
            await scheduler.start()
            try:
                while not stopping.is_set():
                    if scheduler.loop.done():
                        await scheduler.loop
                        raise RuntimeError('Chat dispatcher stopped unexpectedly')
                    write_heartbeat('ready', scheduler.pending_terminal_count)
                    try:
                        await asyncio.wait_for(stopping.wait(),timeout=HEARTBEAT_INTERVAL)
                    except TimeoutError:
                        pass
            finally:
                try:
                    await scheduler.stop()
                    await document_rag.stop()
                finally:
                    write_heartbeat('stopped', scheduler.pending_terminal_count)
        finally:
            await db.close_async_work()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-health',action='store_true')
    args=parser.parse_args()
    if args.check_health:
        raise SystemExit(0 if heartbeat_ready() else 1)
    asyncio.run(serve())


if __name__=='__main__':
    main()
