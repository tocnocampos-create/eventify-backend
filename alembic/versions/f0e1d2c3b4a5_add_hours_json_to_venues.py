"""add hours_json to venues

Revision ID: f0e1d2c3b4a5
Revises: de165a4f12a5
Create Date: 2026-05-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'f0e1d2c3b4a5'
down_revision = 'de165a4f12a5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'venues',
        sa.Column('hours_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('venues', 'hours_json')
