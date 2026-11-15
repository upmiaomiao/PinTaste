"""add users table and project.user_id

Revision ID: 019_add_users_and_project_user_id
Revises: 018_add_project_title
Create Date: 2026-05-11 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '019_add_users_and_project_user_id'
down_revision = '018_add_project_title'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='employee'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('username', name='uq_users_username'),
    )
    op.create_index('ix_users_username', 'users', ['username'], unique=True)

    # Add user_id FK to projects
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_projects_user_id', ['user_id'])
        batch_op.create_foreign_key(
            'fk_projects_user_id_users',
            'users',
            ['user_id'],
            ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_constraint('fk_projects_user_id_users', type_='foreignkey')
        batch_op.drop_index('ix_projects_user_id')
        batch_op.drop_column('user_id')

    op.drop_index('ix_users_username', table_name='users')
    op.drop_table('users')
