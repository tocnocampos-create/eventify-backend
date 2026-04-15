"""add password_reset_codes table

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'password_reset_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('code', sa.String(6), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_password_reset_codes_id', 'password_reset_codes', ['id'])
    op.create_index('ix_password_reset_codes_email', 'password_reset_codes', ['email'])


def downgrade() -> None:
    op.drop_index('ix_password_reset_codes_email', table_name='password_reset_codes')
    op.drop_index('ix_password_reset_codes_id', table_name='password_reset_codes')
    op.drop_table('password_reset_codes')
