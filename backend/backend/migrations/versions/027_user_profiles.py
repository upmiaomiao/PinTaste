"""Per-user first-run profile (Work questionnaire answers, op201)."""
from alembic import op
import sqlalchemy as sa

revision = '027_user_profiles'
down_revision = '026_design_habits'
branch_labels = None
depends_on = None

TABLES = ('wuyu_user_profiles',)


def upgrade():
    # SQLite deployments create this table at boot; never fail on an existing one.
    if sa.inspect(op.get_bind()).has_table('wuyu_user_profiles'):
        return
    op.create_table('wuyu_user_profiles',
        sa.Column('owner_id', sa.String(36), primary_key=True),
        sa.Column('value', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('updated_at', sa.DateTime(), nullable=False))


def downgrade():
    if sa.inspect(op.get_bind()).has_table('wuyu_user_profiles'):
        op.drop_table('wuyu_user_profiles')
