"""One-use redemption codes: an administrator creates a batch, each user redeems one code once.

Codes are shown in plain text only when they are created; the database keeps their SHA-256.
A redemption adds long-term (bonus) points through the same ledger as an administrator grant,
so balances, history and audit stay in one place (wuyu_quota.QuotaService).
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
import uuid

from sqlalchemy import case, select, update, func

from models.wuyu_quota import QuotaAccount, QuotaCode, QuotaCodeAttempt

# No 0/O/1/I/L: codes are read out loud and typed by hand.
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
MAX_BATCH, MAX_CODE_POINTS = 500, 100_000
ATTEMPT_WINDOW, ATTEMPT_LIMIT = timedelta(minutes=10), 10


def normalize(code):
    """'wy-abcd efgh-jkmn' -> 'WY-ABCD-EFGH-JKMN'. Spaces, dashes and case are ignored."""
    raw = re.sub(r'[\s\-]', '', str(code or '')).upper()
    if raw.startswith('WY'):
        raw = raw[2:]
    if len(raw) != 12 or any(c not in ALPHABET for c in raw):
        return None
    return 'WY-' + '-'.join(raw[i:i + 4] for i in range(0, 12, 4))


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _new_code():
    body = ''.join(secrets.choice(ALPHABET) for _ in range(12))
    return 'WY-' + '-'.join(body[i:i + 4] for i in range(0, 12, 4))


class QuotaCodesMixin:
    """Mixed into QuotaService: shares its transaction, account and ledger helpers."""

    def codes_create(self, actor_id, count, points, expires_at=None, note=''):
        from .wuyu_quota import QuotaError, _integer, _iso
        self._authorize_admin(actor_id)
        _integer(count, 1, MAX_BATCH)
        _integer(points, 1, MAX_CODE_POINTS)
        note = (note or '').strip()
        if len(note) > 200:
            raise QuotaError('QUOTA_REQUEST_INVALID', '备注不超过200字。')
        expiry = None
        if expires_at:
            try:
                parsed = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            except ValueError:
                raise QuotaError('QUOTA_REQUEST_INVALID', '失效时间格式无效。') from None
            if parsed.tzinfo is None:
                # A date picked in the console means the end of that day in Beijing time.
                from zoneinfo import ZoneInfo
                parsed = parsed.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo('Asia/Shanghai'))
            if parsed <= self.clock():
                raise QuotaError('QUOTA_REQUEST_INVALID', '失效时间必须晚于现在。')
            expiry = _iso(parsed)
        batch = str(uuid.uuid4())
        now = _iso(self.clock())
        codes = []
        with self._transaction() as conn:
            while len(codes) < count:
                code = _new_code()
                digest = _hash(code)
                if conn.execute(select(QuotaCode.code_hash).where(QuotaCode.code_hash == digest)).first():
                    continue
                conn.execute(QuotaCode.__table__.insert().values(code_hash=digest, batch_id=batch, points=points,
                    expires_at=expiry, note=note, created_by=actor_id, created_at=now))
                codes.append(code)
        return {'batch_id': batch, 'points': points, 'expires_at': expiry, 'note': note, 'codes': codes}

    def codes_list(self, actor_id, limit=50):
        from .wuyu_quota import _integer, _iso
        self._authorize_admin(actor_id)
        _integer(limit, 1, 200)
        now = _iso(self.clock())
        table = QuotaCode.__table__
        with self._transaction() as conn:
            def count(condition):
                return func.sum(case((condition, 1), else_=0))
            unused_now = table.c.redeemed_at.is_(None) & table.c.revoked_at.is_(None)
            rows = conn.execute(select(
                table.c.batch_id, func.min(table.c.points), func.min(table.c.note), func.min(table.c.created_at),
                func.min(table.c.created_by), func.min(table.c.expires_at), func.count(),
                count(table.c.redeemed_at.isnot(None)),
                count(table.c.revoked_at.isnot(None) & table.c.redeemed_at.is_(None)),
                count(unused_now & table.c.expires_at.isnot(None) & (table.c.expires_at <= now)),
            ).group_by(table.c.batch_id).order_by(func.min(table.c.created_at).desc()).limit(limit)).all()
        batches = []
        for batch, points, note, created, creator, expires, total, used, revoked, expired in rows:
            used, revoked, expired = int(used or 0), int(revoked or 0), int(expired or 0)
            batches.append({'batch_id': batch, 'points': points, 'note': note, 'created_at': created, 'created_by': creator,
                            'expires_at': expires, 'total': total, 'redeemed': used, 'revoked': revoked, 'expired': expired,
                            'unused': total - used - revoked - expired})
        return {'batches': batches}

    def codes_revoke(self, actor_id, batch_id):
        from .wuyu_quota import QuotaError, _key, _iso
        self._authorize_admin(actor_id)
        _key(batch_id, 36)
        with self._transaction() as conn:
            exists = conn.execute(select(QuotaCode.code_hash).where(QuotaCode.batch_id == batch_id)).first()
            if not exists:
                raise QuotaError('QUOTA_CODE_BATCH_NOT_FOUND', '兑换码批次不存在。', 404)
            changed = conn.execute(update(QuotaCode).where(QuotaCode.batch_id == batch_id, QuotaCode.redeemed_at.is_(None),
                QuotaCode.revoked_at.is_(None)).values(revoked_at=_iso(self.clock()))).rowcount
        return {'batch_id': batch_id, 'revoked': changed}

    def redeem(self, user_id, code):
        from .wuyu_quota import QuotaError, _iso
        now_dt = self.clock()
        now = _iso(now_dt)
        with self._transaction() as conn:
            window = _iso(now_dt - ATTEMPT_WINDOW)
            failures = conn.execute(select(func.count()).select_from(QuotaCodeAttempt).where(
                QuotaCodeAttempt.user_id == user_id, QuotaCodeAttempt.created_at > window)).scalar_one()
            if failures >= ATTEMPT_LIMIT:
                raise QuotaError('QUOTA_CODE_TOO_MANY_ATTEMPTS', '兑换码输错次数过多，请10分钟后再试。', 429)
            normalized = normalize(code)
            row = conn.execute(select(QuotaCode.__table__).where(QuotaCode.code_hash == _hash(normalized))).mappings().first() if normalized else None
            problem = (('QUOTA_CODE_INVALID', '兑换码不存在，请核对后重新输入。', 404) if row is None
                else ('QUOTA_CODE_USED', '这个兑换码已经被使用过了。', 409) if row['redeemed_at']
                else ('QUOTA_CODE_REVOKED', '这个兑换码已作废。', 409) if row['revoked_at']
                else ('QUOTA_CODE_EXPIRED', '这个兑换码已过期。', 409) if row['expires_at'] and row['expires_at'] <= now
                else None)
            if problem:
                if problem[0] == 'QUOTA_CODE_INVALID':
                    conn.execute(QuotaCodeAttempt.__table__.insert().values(user_id=user_id, created_at=now))
                    conn.commit()
                    # The persisted failed attempt ended its transaction and lock.
                    self._begin_transaction(conn)
                raise QuotaError(*problem)
            account, day, _ = self._ensure(conn, user_id)
            claimed = conn.execute(update(QuotaCode).where(QuotaCode.code_hash == row['code_hash'],
                QuotaCode.redeemed_at.is_(None), QuotaCode.revoked_at.is_(None)).values(
                redeemed_by=user_id, redeemed_at=now)).rowcount
            if claimed != 1:
                raise QuotaError('QUOTA_CODE_USED', '这个兑换码已经被使用过了。', 409)
            points = row['points']
            conn.execute(update(QuotaAccount).where(QuotaAccount.id == account['id']).values(
                points_granted=QuotaAccount.points_granted + points, points_available=QuotaAccount.points_available + points))
            reason = ('兑换码' + ('：' + row['note'] if row['note'] else ''))[:200]
            ledger = self._entry(conn, account['id'], day['day'], 'redeem', 'redeem:' + row['code_hash'], now_dt,
                                 points=points, actor_user_id=row['created_by'], reason=reason)
            conn.execute(update(QuotaCode).where(QuotaCode.code_hash == row['code_hash']).values(ledger_id=ledger))
        return {'points': points, 'quota': self.summary(user_id)}
