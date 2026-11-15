"""add user_id to user_templates / user_style_templates / materials / reference_files

Revision ID: 021_add_user_id_to_user_assets
Revises: 020_add_image_refine_to_settings
Create Date: 2026-06-24 22:10:00.000000

User data isolation: each row now records its owner. Existing rows keep
user_id NULL, which the controllers treat as "legacy / globally visible"
(same convention as Project.user_id, see project_controller._ownership_filter).
"""
from alembic import op
import sqlalchemy as sa


revision = '021_add_user_id_to_user_assets'
down_revision = '020_add_image_refine_to_settings'
branch_labels = None
depends_on = None


_TABLES = ('user_templates', 'user_style_templates', 'materials', 'reference_files')


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column('user_id', sa.String(length=36), nullable=True))
            batch_op.create_index(f'ix_{table}_user_id', ['user_id'])
            batch_op.create_foreign_key(f'fk_{table}_user_id_users', 'users', ['user_id'], ['id'])


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f'fk_{table}_user_id_users', type_='foreignkey')
            batch_op.drop_index(f'ix_{table}_user_id')
            batch_op.drop_column('user_id')
