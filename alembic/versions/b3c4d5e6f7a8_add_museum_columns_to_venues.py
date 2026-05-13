"""add museum columns to venues

Revision ID: b3c4d5e6f7a8
Revises: a0b1c2d3e4f5
Create Date: 2026-05-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3c4d5e6f7a8'
down_revision = 'a0b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('opening_hours',        sa.Text(),         nullable=True))
    op.add_column('venues', sa.Column('permanent_collection', sa.Text(),         nullable=True))
    op.add_column('venues', sa.Column('ticket_url',           sa.String(500),    nullable=True))
    op.add_column('venues', sa.Column('instagram_url',        sa.String(500),    nullable=True))
    op.add_column('venues', sa.Column('admission_info',       sa.String(255),    nullable=True))


def downgrade() -> None:
    op.drop_column('venues', 'admission_info')
    op.drop_column('venues', 'instagram_url')
    op.drop_column('venues', 'ticket_url')
    op.drop_column('venues', 'permanent_collection')
    op.drop_column('venues', 'opening_hours')
