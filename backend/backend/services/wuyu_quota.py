"""Atomic product quota accounting, independent of model/network calls."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select, update, func, text
from sqlalchemy.exc import SQLAlchemyError
from models import db, User
from models.wuyu_quota import QuotaAccount, QuotaDay, QuotaReservation, QuotaLedger

from .wuyu_points_policy import PointsPolicyMixin, DEFAULT_POLICY, tariffs
from .wuyu_quota_codes import QuotaCodesMixin

DAILY_PAGES, DAILY_POINTS = 0, 1200
MAX_POINTS, MAX_ADJUSTMENT = 1_000_000, 10_000_000
TARIFFS = tariffs(DEFAULT_POLICY)

# One database-wide quota domain, matching SQLite's existing serialized writes.
# Transaction-scoped locks are released by either commit, rollback or disconnect.
PG_QUOTA_LOCK_NAMESPACE = 0x57555955
PG_QUOTA_LOCK_KEY = 0x51554F54
PG_QUOTA_LOCK_TIMEOUT_MS = 10_000


class QuotaError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _integer(value, minimum=0, maximum=MAX_ADJUSTMENT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise QuotaError('QUOTA_REQUEST_INVALID', '额度参数必须是允许范围内的整数。')
    return value


def text_points(input_tokens=0, output_tokens=0, tariff=None):
    _integer(input_tokens, maximum=100_000_000)
    _integer(output_tokens, maximum=100_000_000)
    rate = tariff or TARIFFS["text"]
    incoming, outgoing = rate["input_tokens_per_point"], rate["output_tokens_per_point"]
    return max(1, (input_tokens + incoming - 1) // incoming + (output_tokens + outgoing - 1) // outgoing)


def _key(value, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise QuotaError('QUOTA_REQUEST_INVALID', '请提供有效的幂等请求标识。')
    return value


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class QuotaService(PointsPolicyMixin, QuotaCodesMixin):
    def __init__(self, app, clock=None):
        self.app, self.clock = app, clock or (lambda: datetime.now(timezone.utc))

    def _begin_transaction(self, conn):
        """Begin one short, serialized accounting unit; never performs provider IO."""
        if conn.dialect.name == 'sqlite':
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            return
        if conn.dialect.name != 'postgresql':
            raise QuotaError('QUOTA_STORAGE_UNAVAILABLE', '额度存储尚未配置受支持的原子事务。', 503)
        if conn.in_transaction():
            raise QuotaError('QUOTA_STORAGE_UNAVAILABLE', '额度存储尚未配置受支持的原子事务。', 503)
        timeout_ms = self.app.config.get('WUYU_QUOTA_PG_LOCK_TIMEOUT_MS', PG_QUOTA_LOCK_TIMEOUT_MS)
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30_000:
            raise QuotaError('QUOTA_STORAGE_UNAVAILABLE', '额度存储尚未配置受支持的原子事务。', 503)
        # READ COMMITTED takes a fresh snapshot for business queries after the
        # advisory-lock wait. REPEATABLE READ could keep a pre-wait snapshot.
        # This is called only on a fresh connection or after the explicit commit
        # which persists an invalid-code attempt below; no transaction is active.
        conn.execution_options(isolation_level='READ COMMITTED')
        conn.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                     {'timeout': str(timeout_ms) + 'ms'})
        conn.execute(text('SELECT pg_advisory_xact_lock(:namespace, :key)'),
                     {'namespace': PG_QUOTA_LOCK_NAMESPACE, 'key': PG_QUOTA_LOCK_KEY})

    @contextmanager
    def _transaction(self):
        # A database write lock spans only local accounting, never a provider call.
        with self.app.app_context():
            try:
                with db.engine.connect() as conn:
                    self._begin_transaction(conn)
                    try:
                        yield conn
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
            except SQLAlchemyError:
                raise QuotaError('QUOTA_STORAGE_UNAVAILABLE', '额度账本暂不可用，请稍后重试。', 503) from None

    def _ensure(self, conn, user_id):
        if not conn.execute(select(User.id).where(User.id == user_id)).first():
            raise QuotaError('QUOTA_USER_NOT_FOUND', '账号不存在。', 404)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        day = now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        account = conn.execute(select(QuotaAccount.__table__).where(QuotaAccount.user_id == user_id)).mappings().first()
        if account is None:
            account_id = str(uuid.uuid4())
            conn.execute(QuotaAccount.__table__.insert().values(id=account_id, user_id=user_id, created_at=_iso(now)))
            account = conn.execute(select(QuotaAccount.__table__).where(QuotaAccount.id == account_id)).mappings().one()
        policy = self._policy(conn)['settings']
        # Migration is recorded once per wallet. Paired legacy page+point grants
        # describe the same allowance, so only an uncovered page value is added.
        migrated = conn.execute(select(QuotaLedger.id).where(QuotaLedger.account_id == account['id'],
            QuotaLedger.request_key == 'points-migration:v1')).first()
        if not migrated:
            converted = max(0, account['pages_available'] * 100 - account['points_available'])
            conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(
                pages_available=0, points_available=QuotaAccount.points_available + converted,
                points_granted=QuotaAccount.points_granted + converted))
            self._entry(conn, account['id'], day, 'migration', 'points-migration:v1', now,
                pages=-account['pages_available'], points=converted, reason='统一积分；旧页数与配套积分不重复折算')
        signup = conn.execute(select(QuotaLedger.id).where(QuotaLedger.account_id == account['id'],
            QuotaLedger.request_key == 'signup:v1')).first()
        if not signup:
            conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(
                points_available=QuotaAccount.points_available + policy['signup_points'],
                points_granted=QuotaAccount.points_granted + policy['signup_points']))
            self._entry(conn, account['id'], day, 'signup', 'signup:v1', now,
                points=policy['signup_points'], reason='注册赠送积分（现有内测账号补发一次）')
        account = conn.execute(select(QuotaAccount.__table__).where(QuotaAccount.id == account['id'])).mappings().one()
        # -1 is an explicit per-account unlimited allowance, not a negative grant.
        daily = policy['daily_points'] if account['daily_points_override'] is None else max(0, account['daily_points_override'])
        day_row = conn.execute(select(QuotaDay.__table__).where(QuotaDay.account_id == account['id'], QuotaDay.day == day)).mappings().first()
        if day_row is None:
            conn.execute(QuotaDay.__table__.insert().values(account_id=account['id'], day=day, created_at=_iso(now),
                pages_granted=0, pages_available=0, points_granted=daily, points_available=daily))
            self._entry(conn, account['id'], day, 'grant', 'grant:' + day, now, points=daily, reason='每日赠送积分；当日有效，不累加')
        elif not migrated:
            # Preserve already spent/reserved points; only unused daily allowance
            # is reset on the one-time product migration. Never touch old days.
            target = max(daily, day_row['points_spent'] + day_row['points_reserved'])
            delta = target - day_row['points_granted']
            conn.execute(update(QuotaDay).where(QuotaDay.account_id == account['id'], QuotaDay.day == day).values(
                points_granted=target, points_available=target-day_row['points_spent']-day_row['points_reserved'], pages_available=0))
            self._entry(conn, account['id'], day, 'migration', 'daily-points-migration:v1', now,
                points=delta, pages=-day_row['pages_available'], reason='每日额度统一为积分；保留已用与预占')
        day_row = conn.execute(select(QuotaDay.__table__).where(QuotaDay.account_id == account['id'], QuotaDay.day == day)).mappings().one()
        return account, day_row, now

    def refresh_daily_grants(self):
        """Operator release hook: uplift every existing user atomically and idempotently."""
        with self._transaction() as conn:
            users = conn.execute(select(User.id).order_by(User.id)).scalars().all()
            before = conn.execute(select(func.coalesce(func.sum(QuotaDay.pages_granted), 0),
                                         func.coalesce(func.sum(QuotaDay.points_granted), 0))).one()
            for user_id in users:
                self._ensure(conn, user_id)
            after = conn.execute(select(func.coalesce(func.sum(QuotaDay.pages_granted), 0),
                                        func.coalesce(func.sum(QuotaDay.points_granted), 0))).one()
            return {'users': len(users), 'added_pages': after[0] - before[0],
                    'added_points': after[1] - before[1], 'daily_pages': 0, 'daily_points': self._policy(conn)['settings']['daily_points']}

    def _entry(self, conn, account_id, day, event, request_key, now, **values):
        ident = str(uuid.uuid4())
        conn.execute(QuotaLedger.__table__.insert().values(id=ident, account_id=account_id, day=day,
            event=event, request_key=request_key, created_at=_iso(now), **values))
        return ident

    @staticmethod
    def _public_reservation(row, created=False):
        keys = ['id', 'user_id', 'operation_key', 'kind', 'status', 'day', 'held_pages', 'held_points',
                'actual_pages', 'actual_points', 'project_id', 'job_id']
        return {**{key: row[key] for key in keys}, 'created': created,
                'tariff': json.loads(row['tariff_json']) if row['tariff_json'] else TARIFFS.get(row['kind'], {})}

    def summary(self, user_id):
        with self._transaction() as conn:
            account, day, now = self._ensure(conn, user_id)
            policy = self._policy(conn)
            balances = lambda row: {resource: {name: row[resource + '_' + name] for name in
                        ['granted', 'available', 'reserved', 'spent']} for resource in ['pages', 'points']}
            held = conn.execute(select(func.coalesce(func.sum(QuotaReservation.held_pages), 0),
                func.coalesce(func.sum(QuotaReservation.held_points), 0)).where(
                    QuotaReservation.account_id == account['id'], QuotaReservation.status == 'reserved')).one()
            local = now.astimezone(ZoneInfo('Asia/Shanghai'))
            reset = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return {'unlimited': account['daily_points_override'] == -1, 'user_id': user_id, 'day': day['day'], 'timezone': 'Asia/Shanghai',
                'reset_at': reset.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                'daily': balances(day), 'bonus': balances(account),
                'available': {r: day[r + '_available'] + account[r + '_available'] for r in ['pages', 'points']},
                'reserved': {'pages': int(held[0] or 0), 'points': int(held[1] or 0)}, 'tariffs': tariffs(policy['settings']),
                'unit': 'points', 'policy': policy, 'control': {'revision': account['control_revision'],
                    'spending_frozen': account['spending_frozen'], 'daily_points_override': account['daily_points_override']},
                'estimated_standard_pages': (day['points_available']+account['points_available']) // policy['settings']['standard_page_points']}

    @staticmethod
    def _require_entitlement(account, day):
        if account['spending_frozen']:
            raise QuotaError('QUOTA_ACCOUNT_FROZEN', '当前账户的积分使用已暂停，请联系管理员；已完成的文件仍可下载。', 403)
        if account['daily_points_override'] != -1 and sum(row['points_'+kind] for row in (day, account) for kind in ('available', 'reserved')) <= 0:
            raise QuotaError('QUOTA_EXCEEDED', '积分已用完，后续付费操作已停止。已完成内容保留，请等待每日刷新或联系管理员。', 429)

    def assert_work_allowed(self,user_id):
        with self._transaction()as conn:
            account,day,_=self._ensure(conn,user_id)
            self._require_entitlement(account,day)

    def reserve(self, user_id, operation_key, kind, *, input_tokens=0, output_tokens=0,
                points_upper=None, project_id=None, job_id=None):
        _key(operation_key)
        if kind not in TARIFFS:
            raise QuotaError('QUOTA_REQUEST_INVALID', '未知的额度计费类型。')
        _integer(input_tokens, maximum=100_000_000); _integer(output_tokens, maximum=100_000_000)
        if points_upper is not None:
            _integer(points_upper, 1, MAX_POINTS)
        for value in [project_id, job_id]:
            if value is not None:
                _key(value, 36)
        signature = _fingerprint({'kind': kind, 'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'points_upper': points_upper, 'project_id': project_id, 'job_id': job_id})
        with self._transaction() as conn:
            account, day, now = self._ensure(conn, user_id)
            rate = tariffs(self._policy(conn)['settings'])[kind]
            estimated = text_points(input_tokens, output_tokens, rate) if kind == 'text' else rate['points']
            points = estimated if points_upper is None else points_upper
            pages = 0
            previous = conn.execute(select(QuotaReservation.__table__).where(
                QuotaReservation.user_id == user_id, QuotaReservation.operation_key == operation_key)).mappings().first()
            if previous is not None:
                legacy = _fingerprint({'kind': kind, 'pages': previous['held_pages'], 'points': points, 'project_id': project_id, 'job_id': job_id})
                if previous['fingerprint'] != signature and (previous['tariff_json'] or previous['fingerprint'] != legacy):
                    raise QuotaError('QUOTA_IDEMPOTENCY_CONFLICT', '请求标识已用于另一笔额度操作。', 409)
                return self._public_reservation(previous)
            if points < estimated or (kind != 'text' and points != estimated):
                raise QuotaError('QUOTA_REQUEST_INVALID', '预占积分不符合计费规则。')
            self._require_entitlement(account,day)
            # Fund only this hold's shortfall. Existing allocation, settlement,
            # refunds and usage accounting stay atomic and unchanged.
            shortfall = max(0, points - day['points_available'] - account['points_available'])
            if account['daily_points_override'] == -1 and shortfall:
                conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(
                    points_available=QuotaAccount.points_available + shortfall,
                    points_granted=QuotaAccount.points_granted + shortfall))
                self._entry(conn, account['id'], day['day'], 'adjustment',
                    'unlimited:' + _fingerprint({'user': user_id, 'operation': operation_key}), now,
                    points=shortfall, reason='不限额度账号：自动补足本次预占')
                account = conn.execute(select(QuotaAccount.__table__).where(QuotaAccount.id == account['id'])).mappings().one()
            if pages > day['pages_available'] + account['pages_available'] or points > day['points_available'] + account['points_available']:
                raise QuotaError('QUOTA_EXCEEDED', '可用积分不足，未发起付费请求；请等待每日刷新或联系管理员。', 429)
            amounts = {'pages': pages, 'points': points}
            allocation = {}
            for resource, amount in amounts.items():
                free = min(amount, day[resource + '_available'])
                allocation['free_' + resource], allocation['bonus_' + resource] = free, amount - free
            for table, scope, prefix in [(QuotaDay.__table__, (QuotaDay.account_id == account['id']) & (QuotaDay.day == day['day']), 'free_'),
                                         (QuotaAccount.__table__, QuotaAccount.id == account['id'], 'bonus_')]:
                updates, predicate = {}, scope
                for resource in amounts:
                    amount = allocation[prefix + resource]
                    predicate &= table.c[resource + '_available'] >= amount
                    updates[resource + '_available'] = table.c[resource + '_available'] - amount
                    updates[resource + '_reserved'] = table.c[resource + '_reserved'] + amount
                if conn.execute(update(table).where(predicate).values(**updates)).rowcount != 1:
                    raise QuotaError('QUOTA_EXCEEDED', '可用额度不足。', 429)
            ident = str(uuid.uuid4())
            conn.execute(QuotaReservation.__table__.insert().values(id=ident, account_id=account['id'], user_id=user_id,
                operation_key=operation_key, fingerprint=signature, kind=kind, status='reserved', day=day['day'],
                held_pages=pages, held_points=points, project_id=project_id, job_id=job_id, created_at=_iso(now), tariff_json=json.dumps(rate), **allocation))
            self._entry(conn, account['id'], day['day'], 'reserve', 'reserve:' + ident, now,
                        kind=kind, reservation_id=ident, pages=-pages, points=-points)
            row = conn.execute(select(QuotaReservation.__table__).where(QuotaReservation.id == ident)).mappings().one()
            return self._public_reservation(row, True)

    def _finish(self, ident, actual_points=None, refund=False, reason=''):
        _key(ident, 36)
        with self._transaction() as conn:
            row = conn.execute(select(QuotaReservation.__table__).where(QuotaReservation.id == ident)).mappings().first()
            if row is None:
                raise QuotaError('QUOTA_RESERVATION_NOT_FOUND', '额度预占记录不存在。', 404)
            if row['status'] != 'reserved':
                if not refund and row['status'] == 'settled' and actual_points is not None and actual_points != row['actual_points']:
                    raise QuotaError('QUOTA_IDEMPOTENCY_CONFLICT', '此预占已按另一结算金额完成。', 409)
                return self._public_reservation(row)
            if refund:
                actual, pages = 0, 0
            else:
                actual = row['held_points'] if actual_points is None else _integer(actual_points, 1, row['held_points'])
                if row['kind'] != 'text' and actual != row['held_points']:
                    raise QuotaError('QUOTA_REQUEST_INVALID', '此类型的积分结算必须使用固定规则。')
                pages = row['held_pages']
            for table, scope, prefix in [(QuotaDay.__table__, (QuotaDay.account_id == row['account_id']) & (QuotaDay.day == row['day']), 'free_'),
                                         (QuotaAccount.__table__, QuotaAccount.id == row['account_id'], 'bonus_')]:
                changes = {}
                for resource, charge in [('pages', pages), ('points', actual)]:
                    spent = min(charge, row['free_' + resource]) if prefix == 'free_' else max(0, charge - row['free_' + resource])
                    held = row[prefix + resource]
                    changes[resource + '_reserved'] = table.c[resource + '_reserved'] - held
                    changes[resource + '_available'] = table.c[resource + '_available'] + held - spent
                    changes[resource + '_spent'] = table.c[resource + '_spent'] + spent
                conn.execute(update(table).where(scope).values(**changes))
            now = self.clock()
            status, event = ('refunded', 'refund') if refund else ('settled', 'settle')
            conn.execute(update(QuotaReservation).where(QuotaReservation.id == ident).values(
                status=status, actual_pages=pages, actual_points=actual, completed_at=_iso(now)))
            self._entry(conn, row['account_id'], row['day'], event, event + ':' + ident, now,
                        kind=row['kind'], reservation_id=ident, pages=row['held_pages'] - pages,
                        points=row['held_points'] - actual, charged_pages=pages, charged_points=actual,
                        reason=str(reason)[:200])
            current = conn.execute(select(QuotaReservation.__table__).where(QuotaReservation.id == ident)).mappings().one()
            return self._public_reservation(current, True)

    def settle(self, ident, actual_points=None):
        return self._finish(ident, actual_points=actual_points)

    def refund(self, ident, reason=''):
        return self._finish(ident, refund=True, reason=reason)

    def adjust(self, actor_id, user_id, pages, points, reason, idempotency_key):
        self._authorize_admin(actor_id)
        _integer(pages); _integer(points, -MAX_ADJUSTMENT, MAX_ADJUSTMENT); _key(idempotency_key)
        if pages:
            raise QuotaError('QUOTA_REQUEST_INVALID', '已统一为积分，请将页数设为0并填写积分。')
        if not points or not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise QuotaError('QUOTA_REQUEST_INVALID', '请填写有效积分数量和不超过200字的说明；负数仅扣回未使用的长期积分。')
        signature = _fingerprint({'actor': actor_id, 'pages': pages, 'points': points, 'reason': reason})
        with self._transaction() as conn:
            account, day, now = self._ensure(conn, user_id)
            previous = conn.execute(select(QuotaLedger.__table__).where(QuotaLedger.account_id == account['id'],
                QuotaLedger.request_key == 'adjust:' + idempotency_key)).mappings().first()
            if previous is not None:
                if previous['fingerprint'] != signature:
                    raise QuotaError('QUOTA_IDEMPOTENCY_CONFLICT', '同一请求标识不能用于不同的加额。', 409)
                ident, created = previous['id'], False
            else:
                if account['points_available'] + points < 0:
                    raise QuotaError('QUOTA_ADJUSTMENT_INSUFFICIENT', '可扣回的长期积分不足，不会动用每日积分或预占积分。', 409)
                conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(
                    pages_granted=QuotaAccount.pages_granted + pages, pages_available=QuotaAccount.pages_available + pages,
                    points_granted=QuotaAccount.points_granted + points, points_available=QuotaAccount.points_available + points))
                ident = self._entry(conn, account['id'], day['day'], 'adjustment', 'adjust:' + idempotency_key,
                    now, pages=pages, points=points, actor_user_id=actor_id, reason=reason, fingerprint=signature)
                created = True
        return {'quota': self.summary(user_id), 'adjustment': {'id': ident, 'created': created, 'pages': pages, 'points': points, 'reason': reason}}

    def ledger(self, user_id, limit=50, offset=0):
        _integer(limit, 1, 100); _integer(offset, 0, 1_000_000)
        with self._transaction() as conn:
            account, _day, _now = self._ensure(conn, user_id)
            scope = QuotaLedger.account_id == account['id']
            total = conn.execute(select(func.count()).select_from(QuotaLedger).where(scope)).scalar_one()
            rows = conn.execute(select(QuotaLedger.__table__, QuotaReservation.project_id, QuotaReservation.job_id, QuotaReservation.operation_key).outerjoin(QuotaReservation, QuotaReservation.id == QuotaLedger.reservation_id).where(scope).order_by(QuotaLedger.sequence.desc()).limit(limit).offset(offset)).mappings().all()
            keys = ['id', 'event', 'kind', 'day', 'pages', 'points', 'charged_pages', 'charged_points', 'reason', 'created_at', 'reservation_id', 'actor_user_id', 'project_id', 'job_id', 'operation_key']
            return {'entries': [{key: row[key] for key in keys} for row in rows], 'total': total, 'limit': limit, 'offset': offset}


def get_quota_service(app=None):
    app = app or current_app._get_current_object()
    if 'wuyu_quota' not in app.extensions:
        app.extensions['wuyu_quota'] = QuotaService(app)
    return app.extensions['wuyu_quota']
