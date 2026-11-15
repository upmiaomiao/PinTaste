"""add image refine fields to settings

Revision ID: 020_add_image_refine_to_settings
Revises: 019_add_users_and_project_user_id
Create Date: 2026-06-24 21:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '020_add_image_refine_to_settings'
down_revision = '019_add_users_and_project_user_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.add_column(sa.Column('image_mode', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('image_refine_model', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('image_refine_model_source', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('image_refine_api_key', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('image_refine_api_base_url', sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.drop_column('image_refine_api_base_url')
        batch_op.drop_column('image_refine_api_key')
        batch_op.drop_column('image_refine_model_source')
        batch_op.drop_column('image_refine_model')
        batch_op.drop_column('image_mode')
