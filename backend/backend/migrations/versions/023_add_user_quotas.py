"""Durable daily/bonus product quotas and the initiating user of workflow jobs."""
from alembic import op
import sqlalchemy as sa

revision = '023_add_user_quotas'
down_revision = '022_add_image_mode_to_projects'
branch_labels = None
depends_on = None


def _balances():
    return [sa.Column(resource + '_' + field, sa.BigInteger(), nullable=False, server_default='0')
            for resource in ['pages', 'points'] for field in ['granted', 'available', 'reserved', 'spent']]


def upgrade():
    op.create_table('wuyu_quota_accounts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, unique=True),
        sa.Column('created_at', sa.String(32), nullable=False), *_balances(),
        sa.CheckConstraint('pages_available >= 0 AND points_available >= 0 AND pages_reserved >= 0 AND points_reserved >= 0', name='ck_quota_account_nonnegative'))
    op.create_table('wuyu_quota_days',
        sa.Column('account_id', sa.String(36), sa.ForeignKey('wuyu_quota_accounts.id'), primary_key=True),
        sa.Column('day', sa.String(10), primary_key=True),
        sa.Column('created_at', sa.String(32), nullable=False), *_balances(),
        sa.CheckConstraint('pages_available >= 0 AND points_available >= 0 AND pages_reserved >= 0 AND points_reserved >= 0', name='ck_quota_day_nonnegative'))
    op.create_table('wuyu_quota_reservations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('wuyu_quota_accounts.id'), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('operation_key', sa.String(160), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False), sa.Column('status', sa.String(12), nullable=False),
        sa.Column('day', sa.String(10), nullable=False),
        *[sa.Column(name, sa.BigInteger(), nullable=False) for name in ['held_pages', 'held_points', 'free_pages', 'free_points', 'bonus_pages', 'bonus_points']],
        sa.Column('actual_pages', sa.BigInteger(), nullable=True), sa.Column('actual_points', sa.BigInteger(), nullable=True),
        sa.Column('project_id', sa.String(36), nullable=True), sa.Column('job_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.String(32), nullable=False), sa.Column('completed_at', sa.String(32), nullable=True),
        sa.UniqueConstraint('user_id', 'operation_key', name='uq_quota_user_operation'),
        sa.CheckConstraint('held_pages >= 0 AND held_points >= 1', name='ck_quota_hold_nonnegative'))
    op.create_index('ix_wuyu_quota_reservations_account_id', 'wuyu_quota_reservations', ['account_id'])
    op.create_table('wuyu_quota_ledger',
        sa.Column('sequence', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('id', sa.String(36), nullable=False, unique=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('wuyu_quota_accounts.id'), nullable=False),
        sa.Column('request_key', sa.String(190), nullable=False), sa.Column('fingerprint', sa.String(64), nullable=True),
        sa.Column('reservation_id', sa.String(36), sa.ForeignKey('wuyu_quota_reservations.id'), nullable=True),
        sa.Column('actor_user_id', sa.String(36), nullable=True), sa.Column('event', sa.String(16), nullable=False),
        sa.Column('kind', sa.String(12), nullable=True), sa.Column('day', sa.String(10), nullable=False),
        *[sa.Column(name, sa.BigInteger(), nullable=False, server_default='0') for name in ['pages', 'points', 'charged_pages', 'charged_points']],
        sa.Column('reason', sa.String(200), nullable=False, server_default=''), sa.Column('created_at', sa.String(32), nullable=False),
        sa.UniqueConstraint('account_id', 'request_key', name='uq_quota_account_event'))
    op.create_index('ix_wuyu_quota_ledger_account_id', 'wuyu_quota_ledger', ['account_id'])
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('wuyu_workflow_jobs'):
        if 'actor_user_id' not in {column['name'] for column in inspector.get_columns('wuyu_workflow_jobs')}:
            with op.batch_alter_table('wuyu_workflow_jobs') as batch:
                batch.add_column(sa.Column('actor_user_id', sa.String(36), nullable=True))
                batch.create_foreign_key('fk_wuyu_job_actor_user', 'users', ['actor_user_id'], ['id'], ondelete='SET NULL')
            op.create_index('ix_wuyu_workflow_jobs_actor_user_id', 'wuyu_workflow_jobs', ['actor_user_id'])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('wuyu_workflow_jobs') and 'actor_user_id' in {c['name'] for c in inspector.get_columns('wuyu_workflow_jobs')}:
        convention = {'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}
        with op.batch_alter_table('wuyu_workflow_jobs', naming_convention=convention) as batch:
            for constraint in inspector.get_foreign_keys('wuyu_workflow_jobs'):
                if constraint['constrained_columns'] == ['actor_user_id']:
                    batch.drop_constraint(constraint['name'] or 'fk_wuyu_workflow_jobs_actor_user_id_users', type_='foreignkey')
            if any(index['name'] == 'ix_wuyu_workflow_jobs_actor_user_id' for index in inspector.get_indexes('wuyu_workflow_jobs')):
                batch.drop_index('ix_wuyu_workflow_jobs_actor_user_id')
            batch.drop_column('actor_user_id')
    for table in ['wuyu_quota_ledger', 'wuyu_quota_reservations', 'wuyu_quota_days', 'wuyu_quota_accounts']:
        op.drop_table(table)
