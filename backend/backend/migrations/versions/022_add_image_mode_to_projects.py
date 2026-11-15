"""add image_mode to project

Revision ID: 022_add_image_mode_to_projects
Revises: 021_add_user_id_to_user_assets
Create Date: 2026-09-08 00:00:00.000000

Per-project image generation mode (stable|creative|best). NULL means the
global IMAGE_MODEL / IMAGE_MODE / IMAGE_REFINE_MODEL settings still apply.
"""
from alembic import op
import sqlalchemy as sa


revision = '022_add_image_mode_to_projects'
down_revision = '021_add_user_id_to_user_assets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('image_mode', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_column('image_mode')
