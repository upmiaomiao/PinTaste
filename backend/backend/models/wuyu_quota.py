"""Durable product credits. Ledger rows survive account and project deletion."""
from . import db


class _Balances:
    pages_granted = db.Column(db.BigInteger, nullable=False, default=0)
    pages_available = db.Column(db.BigInteger, nullable=False, default=0)
    pages_reserved = db.Column(db.BigInteger, nullable=False, default=0)
    pages_spent = db.Column(db.BigInteger, nullable=False, default=0)
    points_granted = db.Column(db.BigInteger, nullable=False, default=0)
    points_available = db.Column(db.BigInteger, nullable=False, default=0)
    points_reserved = db.Column(db.BigInteger, nullable=False, default=0)
    points_spent = db.Column(db.BigInteger, nullable=False, default=0)


class QuotaAccount(_Balances, db.Model):
    __tablename__ = 'wuyu_quota_accounts'
    __table_args__ = (db.CheckConstraint('pages_available >= 0 AND points_available >= 0 AND pages_reserved >= 0 AND points_reserved >= 0', name='ck_quota_account_nonnegative'),)
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, unique=True)
    created_at = db.Column(db.String(32), nullable=False)
    spending_frozen = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    daily_points_override = db.Column(db.BigInteger, nullable=True)
    control_revision = db.Column(db.Integer, nullable=False, default=0, server_default='0')


class QuotaDay(_Balances, db.Model):
    __tablename__ = 'wuyu_quota_days'
    __table_args__ = (db.CheckConstraint('pages_available >= 0 AND points_available >= 0 AND pages_reserved >= 0 AND points_reserved >= 0', name='ck_quota_day_nonnegative'),)
    account_id = db.Column(db.String(36), db.ForeignKey('wuyu_quota_accounts.id'), primary_key=True)
    day = db.Column(db.String(10), primary_key=True)
    created_at = db.Column(db.String(32), nullable=False)


class QuotaReservation(db.Model):
    __tablename__ = 'wuyu_quota_reservations'
    __table_args__ = (db.UniqueConstraint('user_id', 'operation_key', name='uq_quota_user_operation'),
        db.CheckConstraint('held_pages >= 0 AND held_points >= 1', name='ck_quota_hold_nonnegative'))
    id = db.Column(db.String(36), primary_key=True)
    account_id = db.Column(db.String(36), db.ForeignKey('wuyu_quota_accounts.id'), nullable=False, index=True)
    user_id = db.Column(db.String(36), nullable=False)
    operation_key = db.Column(db.String(160), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    kind = db.Column(db.String(12), nullable=False)
    status = db.Column(db.String(12), nullable=False)
    day = db.Column(db.String(10), nullable=False)
    held_pages = db.Column(db.BigInteger, nullable=False)
    held_points = db.Column(db.BigInteger, nullable=False)
    free_pages = db.Column(db.BigInteger, nullable=False)
    free_points = db.Column(db.BigInteger, nullable=False)
    bonus_pages = db.Column(db.BigInteger, nullable=False)
    bonus_points = db.Column(db.BigInteger, nullable=False)
    actual_pages = db.Column(db.BigInteger, nullable=True)
    actual_points = db.Column(db.BigInteger, nullable=True)
    project_id = db.Column(db.String(36), nullable=True)
    job_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.String(32), nullable=False)
    completed_at = db.Column(db.String(32), nullable=True)
    tariff_json = db.Column(db.Text, nullable=True)


class QuotaLedger(db.Model):
    __tablename__ = 'wuyu_quota_ledger'
    __table_args__ = (db.UniqueConstraint('account_id', 'request_key', name='uq_quota_account_event'),)
    sequence = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id = db.Column(db.String(36), nullable=False, unique=True)
    account_id = db.Column(db.String(36), db.ForeignKey('wuyu_quota_accounts.id'), nullable=False, index=True)
    request_key = db.Column(db.String(190), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=True)
    reservation_id = db.Column(db.String(36), db.ForeignKey('wuyu_quota_reservations.id'), nullable=True)
    actor_user_id = db.Column(db.String(36), nullable=True)
    event = db.Column(db.String(16), nullable=False)
    kind = db.Column(db.String(12), nullable=True)
    day = db.Column(db.String(10), nullable=False)
    pages = db.Column(db.BigInteger, nullable=False, default=0)
    points = db.Column(db.BigInteger, nullable=False, default=0)
    charged_pages = db.Column(db.BigInteger, nullable=False, default=0)
    charged_points = db.Column(db.BigInteger, nullable=False, default=0)
    reason = db.Column(db.String(200), nullable=False, default='')
    created_at = db.Column(db.String(32), nullable=False)


class QuotaPolicy(db.Model):
    __tablename__ = 'wuyu_quota_policy'
    id = db.Column(db.Integer, primary_key=True)
    revision = db.Column(db.Integer, nullable=False)
    settings_json = db.Column(db.Text, nullable=False)


class QuotaAudit(db.Model):
    __tablename__ = 'wuyu_quota_audit'
    sequence = db.Column(db.Integer, primary_key=True, autoincrement=True)
    request_key = db.Column(db.String(190), nullable=False, unique=True)
    fingerprint = db.Column(db.String(64), nullable=False)
    actor_user_id = db.Column(db.String(36), nullable=False)
    subject = db.Column(db.String(64), nullable=False)
    action = db.Column(db.String(24), nullable=False)
    before_json = db.Column(db.Text, nullable=False)
    after_json = db.Column(db.Text, nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.String(32), nullable=False)


class QuotaCode(db.Model):
    """A one-use redemption code. Only the SHA-256 of the code is stored."""
    __tablename__ = 'wuyu_quota_codes'
    __table_args__ = (db.CheckConstraint('points >= 1', name='ck_quota_code_points'),)
    code_hash = db.Column(db.String(64), primary_key=True)
    batch_id = db.Column(db.String(36), nullable=False, index=True)
    points = db.Column(db.BigInteger, nullable=False)
    expires_at = db.Column(db.String(32), nullable=True)
    note = db.Column(db.String(200), nullable=False, default='', server_default='')
    created_by = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.String(32), nullable=False)
    redeemed_by = db.Column(db.String(36), nullable=True)
    redeemed_at = db.Column(db.String(32), nullable=True)
    ledger_id = db.Column(db.String(36), nullable=True)
    revoked_at = db.Column(db.String(32), nullable=True)


class QuotaCodeAttempt(db.Model):
    """Failed redemption attempts, to slow down code guessing."""
    __tablename__ = 'wuyu_quota_code_attempts'
    sequence = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(36), nullable=False, index=True)
    created_at = db.Column(db.String(32), nullable=False)
