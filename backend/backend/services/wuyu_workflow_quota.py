"""Reserve paid workflow work on its frozen initiating account, outside model threads."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
import json
import uuid

from flask import has_request_context, request

from .wuyu_quota import get_quota_service, text_points, QuotaError
from .wuyu_errors import PipelineError
from .wuyu_planner_context import DEEPSEEK_MODELS, positive_int
from .wuyu_providers import MODELS


current_workflow_actor = ContextVar('wuyu_workflow_actor', default=None)
current_workflow_charge = ContextVar('wuyu_workflow_charge', default=None)


def initiating_user():
    """Use verified request identity or an existing worker's frozen identity."""
    if has_request_context():
        user = getattr(request, 'current_user', None)
        return user.id if user is not None else None
    return current_workflow_actor.get()


class WorkflowCharge:
    """One pre-dispatch hold; accepted outputs settle and rejected outputs refund."""

    def __init__(self, app, job, kind, *, text_input_tokens=None, text_output_tokens=None):
        self.app, self.job, self.kind = app, job, kind
        self.reservation = None
        self.points_upper = None
        self.accepted = False
        self.actual_points = None
        self.text_input_tokens, self.text_output_tokens = text_input_tokens, text_output_tokens

    def reserve(self):
        from services.wuyu_identity import authorize_paid, IdentityFailure
        runtime=getattr(self.job,'_runtime_config',self.app.config)
        try:authorize_paid(getattr(self.job,'actor_user_id',None),self.kind,runtime.get('WUYU_IMAGE_MODEL'))
        except IdentityFailure as error:raise PipelineError(error.code,'当前账号的功能权限已变更，未发起新的模型调用。')from None
        if not self.app.config.get('WUYU_QUOTA_ENABLED', True):
            return self
        if not self.job.actor_user_id:
            raise PipelineError('quota_actor_missing', '当前任务缺少发起账户，请重新发起该步骤；已有文件保留。')
        kwargs = {}
        if self.kind == 'text':
            runtime = getattr(self.job, '_runtime_config', self.app.config)
            planner = runtime.get('WUYU_PLANNER_MODEL') or runtime.get('WUYU_SHENSI_MODEL') or MODELS['planner']
            context = positive_int(runtime, ('WUYU_PLANNER_CONTEXT_TOKENS', 'WUYU_SHENSI_CONTEXT_TOKENS'),
                                   1_000_000 if planner in DEEPSEEK_MODELS else 32768)
            output = positive_int(runtime, ('WUYU_PLANNER_OUTPUT_TOKENS', 'WUYU_SHENSI_OUTPUT_TOKENS'), 8192, 32768)
            if self.text_input_tokens is not None or self.text_output_tokens is not None:
                if (type(self.text_input_tokens) is not int or type(self.text_output_tokens) is not int
                        or not 0 <= self.text_input_tokens <= context
                        or not 0 <= self.text_output_tokens <= output
                        or self.text_input_tokens + self.text_output_tokens > context):
                    raise PipelineError('reading_budget_invalid', '阅读任务预占超过已配置上下文边界。')
                context, output = self.text_input_tokens, self.text_output_tokens
            kwargs.update(input_tokens=context, output_tokens=output)
        self.reservation = get_quota_service(self.app).reserve(
            self.job.actor_user_id, f'job:{self.job.id}:{self.kind}:{uuid.uuid4().hex}', self.kind,
            project_id=self.job.project_id, job_id=self.job.id, **kwargs)
        self.points_upper = self.reservation['held_points']
        return self

    def expected_points(self, pipeline=None, output=None):
        if self.reservation is None:
            return None
        if self.kind == 'trace' and isinstance(output, dict):
            report = output.get('model_report') or (output.get('editable_manifest') or {}).get('model_report') or {}
            if report.get('provider_called') is False:
                return 0
        if self.kind == 'text' and isinstance(output, dict) and output.get('reading_result') is True:
            report = output.get('model_report') or {}
            if report.get('provider_called') is False:return 0
            usage = report.get('usage') or {}
            incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
                return text_points(incoming, outgoing, self.reservation.get('tariff'))
        if self.kind != 'text' or pipeline is None:
            return self.reservation['held_points']
        diagnostic = getattr(pipeline, '_planner_diagnostic', {})
        if diagnostic.get('response_reused') is True and diagnostic.get('provider_called') is False:
            return 0
        gateway = getattr(pipeline, 'gateway', None)
        usage = getattr(gateway, 'last_response_metadata', {}).get('usage', {}) if gateway else {}
        incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
            return text_points(incoming, outgoing, self.reservation.get('tariff'))
        return self.reservation['held_points']

    @contextmanager
    def accepting(self, *, pipeline=None, output=None):
        self.actual_points = self.expected_points(pipeline, output)
        if self.reservation is not None and self.actual_points is not None and self.actual_points > self.reservation['held_points']:
            raise QuotaError('QUOTA_USAGE_EXCEEDS_RESERVATION', '模型实际用量超过预占上限，结果未发布，请调整预估后重试。')
        token = current_workflow_charge.set(self)
        try:
            yield
        finally:
            current_workflow_charge.reset(token)

    def mark_accepted(self, job):
        """Add a receipt in the same transaction that publishes the output."""
        if self.reservation is None:
            return
        progress = json.loads(job.progress or '{}')
        progress.setdefault('quota_receipts', {})[self.reservation['id']] = {'actual_points': self.actual_points}
        job.progress = json.dumps(progress, ensure_ascii=False)
        self.accepted = True

    def settle(self, *, pipeline=None):
        if self.reservation is None:
            return
        actual = self.actual_points if self.actual_points is not None else self.expected_points(pipeline)
        service = get_quota_service(self.app)
        if actual == 0:
            service.refund(self.reservation['id'], reason='cached_response_reused')
        else:
            service.settle(self.reservation['id'], actual_points=actual)

    def refund(self, reason='generation_failed'):
        if self.reservation is not None and not self.accepted:
            get_quota_service(self.app).refund(self.reservation['id'], reason=reason)


def recover_workflow_charges(app):
    """Resolve holds only after their durable job is confirmed terminal."""
    if not app.config.get('WUYU_QUOTA_ENABLED', True):
        return
    from models import db
    from models.wuyu_quota import QuotaReservation
    from models.wuyu_workflow import WuyuWorkflowJob
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    if not inspector.has_table(QuotaReservation.__tablename__) or not inspector.has_table(WuyuWorkflowJob.__tablename__):
        # Startup/migration may precede 023; live reserve still fails closed.
        return
    reservations = QuotaReservation.query.filter(QuotaReservation.status == 'reserved', QuotaReservation.job_id.isnot(None)).all()
    decisions = []
    for reservation in reservations:
        job = db.session.get(WuyuWorkflowJob, reservation.job_id)
        if job is None or job.status not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            continue
        receipt = json.loads(job.progress or '{}').get('quota_receipts', {}).get(reservation.id)
        decisions.append((reservation.id, receipt))
    db.session.rollback()
    service = get_quota_service(app)
    for ident, receipt in decisions:
        if receipt is not None and receipt.get('actual_points') != 0:
            service.settle(ident, actual_points=receipt.get('actual_points'))
        else:
            service.refund(ident, reason='confirmed_worker_recovery')
