"""One-use redemption codes for product points (hash-only storage)."""
from alembic import op
import sqlalchemy as sa

revision = '025_quota_codes'
down_revision = '024_points_policy'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('wuyu_quota_codes',
        sa.Column('code_hash', sa.String(64), primary_key=True),
        sa.Column('batch_id', sa.String(36), nullable=False, index=True),
        sa.Column('points', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.String(32), nullable=True),
        sa.Column('note', sa.String(200), nullable=False, server_default=''),
        sa.Column('created_by', sa.String(36), nullable=False),
        sa.Column('created_at', sa.String(32), nullable=False),
        sa.Column('redeemed_by', sa.String(36), nullable=True),
        sa.Column('redeemed_at', sa.String(32), nullable=True),
        sa.Column('ledger_id', sa.String(36), nullable=True),
        sa.Column('revoked_at', sa.String(32), nullable=True),
        sa.CheckConstraint('points >= 1', name='ck_quota_code_points'))
    op.create_table('wuyu_quota_code_attempts',
        sa.Column('sequence', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(36), nullable=False, index=True),
        sa.Column('created_at', sa.String(32), nullable=False))


def downgrade():
    # Redeemed codes are part of the financial history; keep them on a binary rollback.
    raise RuntimeError('兑换码表不可破坏性回退；请回滚应用代码并保留账本。')
