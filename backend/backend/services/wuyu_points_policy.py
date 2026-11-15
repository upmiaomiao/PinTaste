"""Versioned point rules and audited admin controls; no provider calls under locks."""
import json
from sqlalchemy import select, update, func
from models import User
from models.wuyu_quota import QuotaPolicy, QuotaAudit, QuotaAccount, QuotaDay, QuotaReservation

DEFAULT_POLICY = {
    'daily_points': 1200, 'signup_points': 600, 'standard_page_points': 100,
    'image_points': 60, 'trace_points': 30,
    'input_tokens_per_point': 10000, 'output_tokens_per_point': 2000,
    'search_points': 1, 'fetch_points': 1, 'scholar_points': 1,
    'wechat_search_points': 1, 'wechat_fetch_points': 1, 'voice_points': 2,
}


def tariffs(settings):
    values = {kind: {'pages': 0, 'points': settings[kind + '_points']}
              for kind in ('image', 'trace', 'search', 'fetch', 'scholar', 'wechat_search', 'wechat_fetch', 'voice')}
    values['text'] = {'pages': 0, 'minimum_points': 1,
        'input_tokens_per_point': settings['input_tokens_per_point'],
        'output_tokens_per_point': settings['output_tokens_per_point']}
    return values


class PointsPolicyMixin:
    def _policy(self, conn):
        row = conn.execute(select(QuotaPolicy.__table__).where(QuotaPolicy.id == 1)).mappings().first()
        if row is None:
            conn.execute(QuotaPolicy.__table__.insert().values(id=1, revision=1, settings_json=json.dumps(DEFAULT_POLICY)))
            return {'revision': 1, 'settings': dict(DEFAULT_POLICY)}
        return {'revision': row['revision'], 'settings': json.loads(row['settings_json'])}

    def policy(self):
        with self._transaction() as conn:
            return self._policy(conn)

    def _authorize_admin(self, actor_id):
        from .wuyu_identity import enabled, lookup, IdentityFailure
        from .wuyu_quota import QuotaError
        if enabled():
            try:
                identity = lookup(work_user_id=actor_id)
                allowed = identity['permissions'].get('work_quota_admin') is True and identity['permissions'].get('work') is True
            except IdentityFailure as error:
                raise QuotaError(error.code, '统一账号权限校验未通过。', error.status) from None
        else:
            with self.app.app_context():
                from models import db
                allowed = db.session.execute(select(User.role).where(User.id == actor_id)).scalar_one_or_none() == 'admin'
                db.session.rollback()
        if not allowed:
            raise QuotaError('FORBIDDEN', '当前账号没有积分管理权限。', 403)

    def admin_change(self, actor_id, *, action, settings, expected_revision, reason, idempotency_key, user_id=None):
        from .wuyu_quota import QuotaError, _integer, _key, _fingerprint, _iso
        self._authorize_admin(actor_id)
        _key(idempotency_key, 128); _integer(expected_revision)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200 or not isinstance(settings, dict):
            raise QuotaError('QUOTA_REQUEST_INVALID', '请填写变更原因和有效规则。')
        if action == 'policy':
            if set(settings) != set(DEFAULT_POLICY):
                raise QuotaError('QUOTA_REQUEST_INVALID', '积分规则字段不完整。')
            for key, value in settings.items():
                _integer(value, 0 if key in {'daily_points', 'signup_points'} else 1, 10_000_000)
            subject = 'policy'
        elif action == 'control' and user_id:
            if set(settings) != {'spending_frozen', 'daily_points_override'} or type(settings['spending_frozen']) is not bool:
                raise QuotaError('QUOTA_REQUEST_INVALID', '用户积分控制字段无效。')
            if settings['daily_points_override'] is not None:
                _integer(settings['daily_points_override'], -1)
            subject = user_id
        else:
            raise QuotaError('QUOTA_REQUEST_INVALID', '未知的积分管理操作。')
        signature = _fingerprint(dict(actor=actor_id, action=action, subject=subject, settings=settings,
                                      expected_revision=expected_revision, reason=reason))
        with self._transaction() as conn:
            previous = conn.execute(select(QuotaAudit.__table__).where(QuotaAudit.request_key == idempotency_key)).mappings().first()
            if previous:
                if previous['fingerprint'] != signature:
                    raise QuotaError('QUOTA_IDEMPOTENCY_CONFLICT', '此请求标识已用于其他变更。', 409)
                return json.loads(previous['after_json'])
            if action == 'policy':
                before = self._policy(conn)
                revision = before['revision']
            else:
                account, _, _ = self._ensure(conn, user_id)
                revision = account['control_revision']
                before = {'revision': revision, 'settings': {key: account[key] for key in settings}}
            if revision != expected_revision:
                raise QuotaError('QUOTA_REVISION_CONFLICT', '规则已被其他管理员更新，请刷新后重新确认。', 409)
            after = {'revision': revision + 1, 'settings': settings}
            if action == 'policy':
                conn.execute(update(QuotaPolicy).where(QuotaPolicy.id == 1).values(revision=revision + 1, settings_json=json.dumps(settings)))
            else:
                conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(control_revision=revision + 1, **settings))
            conn.execute(QuotaAudit.__table__.insert().values(request_key=idempotency_key, fingerprint=signature,
                actor_user_id=actor_id, subject=subject, action=action, before_json=json.dumps(before),
                after_json=json.dumps(after), reason=reason.strip(), created_at=_iso(self.clock())))
            return after

    def admin_overview(self, actor_id):
        self._authorize_admin(actor_id)
        from zoneinfo import ZoneInfo
        day = self.clock().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self._transaction() as conn:
            policy = self._policy(conn)
            rows = conn.execute(select(QuotaAudit.__table__).order_by(QuotaAudit.sequence.desc()).limit(50)).mappings().all()
            audits = [{k: row[k] for k in ('sequence', 'actor_user_id', 'subject', 'action', 'reason', 'created_at')} |
                      {'before': json.loads(row['before_json']), 'after': json.loads(row['after_json'])} for row in rows]
            holds = conn.execute(select(func.count(), func.coalesce(func.sum(QuotaReservation.held_points), 0))
                .select_from(QuotaReservation).where(QuotaReservation.status == 'reserved')).one()
            usage = conn.execute(select(QuotaReservation.kind, func.count(), func.sum(QuotaReservation.actual_points))
                .where(QuotaReservation.day == day, QuotaReservation.status == 'settled').group_by(QuotaReservation.kind)).all()
            # op177: PostgreSQL SUM() returns NUMERIC (Decimal -> JSON string); keep integers.
            return {'policy': policy, 'audit': audits, 'day': day, 'pending_reservations': int(holds[0] or 0), 'reserved_points': int(holds[1] or 0),
                    'usage': [{'kind': kind, 'calls': int(count or 0), 'points': int(points or 0)} for kind, count, points in usage]}
