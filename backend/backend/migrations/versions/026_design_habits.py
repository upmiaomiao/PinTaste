"""Per-user design habits, their revisions, the design event log and injection log."""
from alembic import op
import sqlalchemy as sa

revision = '026_design_habits'
down_revision = '025_quota_codes'
branch_labels = None
depends_on = None

TABLES = ('wuyu_design_events', 'wuyu_design_habits', 'wuyu_design_habit_revisions',
          'wuyu_design_injections', 'wuyu_design_habit_prefs')


def _create(inspector, name, *columns, indexes=()):
    # SQLite deployments create these tables at boot; never fail on an existing one.
    if inspector.has_table(name):
        return
    op.create_table(name, *columns)
    for column in indexes:
        op.create_index('ix_' + name + '_' + column, name, [column])


def upgrade():
    inspector = sa.inspect(op.get_bind())
    _create(inspector, 'wuyu_design_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('page_number', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('context', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('request_key', sa.String(160), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        indexes=('owner_id', 'project_id'))
    _create(inspector, 'wuyu_design_habits',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('layer', sa.String(10), nullable=False, server_default='personal'),
        sa.Column('owner_id', sa.String(36), nullable=True),
        sa.Column('category', sa.String(12), nullable=False),
        sa.Column('slot', sa.String(60), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('value', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('rule', sa.Text(), nullable=False),
        sa.Column('origin', sa.String(12), nullable=False, server_default='explicit'),
        sa.Column('evidence', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('project_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('helpful', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('harmful', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(10), nullable=False, server_default='candidate'),
        sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('valid_from', sa.DateTime(), nullable=False),
        sa.Column('valid_to', sa.DateTime(), nullable=True),
        sa.Column('superseded_by', sa.String(36), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        indexes=('owner_id',))
    _create(inspector, 'wuyu_design_habit_revisions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('habit_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('actor', sa.String(10), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('before', sa.Text(), nullable=True),
        sa.Column('after', sa.Text(), nullable=True),
        sa.Column('reason', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        indexes=('habit_id', 'owner_id'))
    _create(inspector, 'wuyu_design_injections',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('request_key', sa.String(160), nullable=True),
        sa.Column('habit_ids', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('rule_ids', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        indexes=('owner_id', 'project_id'))
    _create(inspector, 'wuyu_design_habit_prefs',
        sa.Column('owner_id', sa.String(36), primary_key=True),
        sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.DateTime(), nullable=False))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    for name in reversed(TABLES):
        if inspector.has_table(name):
            op.drop_table(name)
