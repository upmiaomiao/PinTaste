"""Points-only policy, frozen reservation tariffs and administrator audit."""
from alembic import op
import sqlalchemy as sa

revision = '024_points_policy'
down_revision = '023_add_user_quotas'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wuyu_quota_accounts', sa.Column('spending_frozen', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('wuyu_quota_accounts', sa.Column('daily_points_override', sa.BigInteger(), nullable=True))
    op.add_column('wuyu_quota_accounts', sa.Column('control_revision', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('wuyu_quota_reservations', sa.Column('tariff_json', sa.Text(), nullable=True))
    op.create_table('wuyu_quota_policy', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('revision', sa.Integer(), nullable=False), sa.Column('settings_json', sa.Text(), nullable=False))
    op.create_table('wuyu_quota_audit',
        sa.Column('sequence', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('request_key', sa.String(190), nullable=False, unique=True),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('actor_user_id', sa.String(36), nullable=False),
        sa.Column('subject', sa.String(64), nullable=False), sa.Column('action', sa.String(24), nullable=False),
        sa.Column('before_json', sa.Text(), nullable=False), sa.Column('after_json', sa.Text(), nullable=False),
        sa.Column('reason', sa.String(200), nullable=False), sa.Column('created_at', sa.String(32), nullable=False))


def downgrade():
    # Financial history is deliberately not destroyed by a binary rollback.
    raise RuntimeError('积分审计迁移不可破坏性回退；请回滚应用代码并保留账本。')
