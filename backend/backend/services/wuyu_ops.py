"""Operational visibility: is the service healthy, how loaded is it, what is failing.

Two outputs, both read-only and free of user content (no prompts, filenames, emails or keys):
  /health/ready          public, tiny: 200 when the database answers and workers are not stuck, else 503.
                         For load balancers and uptime checks.
  /api/admin/ops/summary admin only: queue depth, oldest wait, how long jobs really take, what failed and
                         why in the last 24 hours, jobs whose worker stopped reporting.
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify
from sqlalchemy import func, text

from middleware.auth import admin_required
from models import db
from models.wuyu_workflow import WuyuWorkflowJob

ops_bp = Blueprint('wuyu_ops', __name__)

# A running job that has not reported for this long has lost its worker (the heartbeat is 20 s).
STALE_SECONDS = 180


def _percentile(sorted_values, share):
    if not sorted_values:
        return None
    return round(sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * share))], 1)


def readiness(now=None):
    """(ok, details). ok is False only for things that mean requests will fail or hang."""
    now = now or datetime.utcnow()
    details = {'database': 'ok', 'stuck_jobs': 0}
    ok = True
    try:
        db.session.execute(text('SELECT 1'))
    except Exception:
        return False, {'database': 'unavailable', 'stuck_jobs': None}
    try:
        cutoff = now - timedelta(seconds=STALE_SECONDS)
        stuck = WuyuWorkflowJob.query.filter(WuyuWorkflowJob.status == 'running',
                                             WuyuWorkflowJob.heartbeat_at < cutoff).count()
        details['stuck_jobs'] = stuck
        if stuck:
            details['note'] = 'some running jobs stopped reporting; they are recovered on restart'
    except Exception:
        details['stuck_jobs'] = None
    return ok, details


def ops_summary(now=None):
    now = now or datetime.utcnow()
    day = now - timedelta(hours=24)
    by_status = dict(db.session.query(WuyuWorkflowJob.status, func.count()).group_by(WuyuWorkflowJob.status).all())
    queued = WuyuWorkflowJob.query.filter_by(status='queued').order_by(WuyuWorkflowJob.created_at).all()
    oldest = round((now - queued[0].created_at).total_seconds()) if queued else 0
    stale_cutoff = now - timedelta(seconds=STALE_SECONDS)
    stale = WuyuWorkflowJob.query.filter(WuyuWorkflowJob.status == 'running',
                                         WuyuWorkflowJob.heartbeat_at < stale_cutoff).count()
    recent = WuyuWorkflowJob.query.filter(WuyuWorkflowJob.created_at >= day).all()
    durations, failures, outcomes = {}, {}, {}
    waiting = []
    for job in recent:
        outcomes[job.status] = outcomes.get(job.status, 0) + 1
        if job.status == 'failed':
            code = job.error_code or 'unknown'
            failures[code] = failures.get(code, 0) + 1
        if job.started_at and job.completed_at and job.status == 'completed':
            durations.setdefault(job.action, []).append((job.completed_at - job.started_at).total_seconds())
        if job.started_at:
            waiting.append((job.started_at - job.created_at).total_seconds())
    timings = {action: {'jobs': len(values), 'p50_s': _percentile(sorted(values), .5), 'p90_s': _percentile(sorted(values), .9)}
               for action, values in sorted(durations.items())}
    waiting.sort()
    users_waiting = len({job.actor_user_id for job in queued})
    return {'generated_at': now.isoformat() + 'Z',
            'queue': {'queued': len(queued), 'running': by_status.get('running', 0), 'users_waiting': users_waiting,
                      'oldest_wait_s': oldest, 'stuck_running': stale},
            'last_24h': {'jobs': len(recent), 'by_status': outcomes,
                         'failures_by_code': dict(sorted(failures.items(), key=lambda item: -item[1])[:15]),
                         'time_in_queue_s': {'p50': _percentile(waiting, .5), 'p90': _percentile(waiting, .9)},
                         'run_time_by_action': timings}}


@ops_bp.get('/health/ready')
def health_ready():
    ok, details = readiness()
    return jsonify({'status': 'ok' if ok else 'unavailable', **details}), (200 if ok else 503)


@ops_bp.get('/api/admin/ops/summary')
@admin_required
def admin_ops_summary():
    return jsonify({'success': True, 'data': ops_summary()})


# One current database statement, independent of the API ORM Session or any
# filesystem SQLite path. Unknown and NULL states always prevent retirement.
_IDLE_SQL = """
SELECT NOT (
    EXISTS (SELECT 1 FROM wuyu_workflow_jobs
        WHERE status IS NULL OR LOWER(status) NOT IN ('completed','failed','cancelled','interrupted'))
    OR EXISTS (SELECT 1 FROM tasks
        WHERE status IS NULL OR LOWER(status) NOT IN ('completed','failed','partial','cancelled','interrupted'))
    OR EXISTS (SELECT 1 FROM wuyu_design_compile_runs
        WHERE status IS NULL OR LOWER(status) NOT IN ('completed','failed'))
) AS idle
"""


def workload_idle():
    """Use a short, independent read transaction; release it before responding.

    WSGI does not reliably notify a handler when its HTTP caller disconnects.
    PostgreSQL therefore bounds the statement and lock wait itself. The context
    managers roll back/close on errors or cancellation, and return the connection
    before Flask writes a response. No transaction is kept for the HTTP lifetime.
    """
    with db.engine.connect() as connection:
        with connection.begin():
            if connection.dialect.name == 'postgresql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                connection.exec_driver_sql("SET LOCAL statement_timeout = '750ms'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '750ms'")
            elif connection.dialect.name != 'sqlite':
                raise RuntimeError('WORKLOAD_DATABASE_UNSUPPORTED')
            value = connection.execute(text(_IDLE_SQL)).scalar_one()
            if type(value) not in (bool, int) or value not in (False, True):
                raise RuntimeError('WORKLOAD_RESULT_INVALID')
            return bool(value)


@ops_bp.get('/health/idle')
def health_idle():
    try:
        idle = workload_idle()
    except Exception:
        # Never include driver text, SQL parameters, identifiers or task values.
        return jsonify({'schema_version': 1, 'idle': False,
                        'error_code': 'WORKLOAD_STATUS_UNAVAILABLE'}), 503
    return jsonify({'schema_version': 1, 'idle': idle}), 200
