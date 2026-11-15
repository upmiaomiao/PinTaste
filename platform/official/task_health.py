"""Read-only task execution diagnostics; no user content or credentials."""
import json
import math
import sqlite3
import time

from . import store as db
from .chat_worker import heartbeat_status
from .runtime_config import settings as runtime


def snapshot(now=None):
    now=time.time() if now is None else now
    worker=(heartbeat_status(now) if runtime.chat_execution_mode != 'combined' else
            {'ready':None, 'status':'not_applicable', 'age_seconds':None})
    alerts=[]
    if worker['ready'] is False:
        alerts.append('worker_unavailable')
    queue=None
    try:
        # Follow the selected store without initializing schema, creating a
        # missing database or writing monitoring state. PG is read-only too.
        with db.connect(readonly=True, timeout=2) as conn:
            queued,running,oldest=conn.execute(
                "SELECT COUNT(CASE WHEN status='queued' THEN 1 END),"
                " COUNT(CASE WHEN status='running' THEN 1 END),"
                " MIN(CASE WHEN status='queued' THEN created END)"
                " FROM jobs WHERE status IN ('queued','running')").fetchone()
        age=0.0 if not queued else max(0.0,now-float(oldest))
        if not math.isfinite(age) or (queued and not math.isfinite(float(oldest))):
            raise ValueError('Invalid queue timestamp')
        queue={'queued':queued,'running':running,'oldest_queued_age_seconds':round(age,3)}
        if queued and age >= runtime.task_queue_warn_seconds:
            alerts.append('queue_wait_high')
    except (sqlite3.Error,OSError,ValueError,TypeError):
        alerts.append('queue_unavailable')
    return {'status':'degraded' if alerts else 'ok','checked_at':now,
            'execution_mode':runtime.chat_execution_mode,'worker':worker,'queue':queue,
            'queue_warn_seconds':runtime.task_queue_warn_seconds,'alerts':alerts}


def main():
    value=snapshot()
    print(json.dumps(value,allow_nan=False))
    raise SystemExit(1 if value['alerts'] else 0)


if __name__=='__main__':
    main()
