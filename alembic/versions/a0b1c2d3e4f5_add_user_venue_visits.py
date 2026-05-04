"""add_user_venue_visits

Revision ID: a0b1c2d3e4f5
Revises: f8a9b0c1d2e3
Create Date: 2026-05-04 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a0b1c2d3e4f5'
down_revision: Union[str, None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_venue_visits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('venue_name', sa.String(length=255), nullable=False),
        sa.Column('venue_type', sa.String(length=100), nullable=True),
        sa.Column('venue_city', sa.String(length=100), nullable=True),
        sa.Column('scheduled_date', sa.String(length=50), nullable=False),
        sa.Column('scheduled_time', sa.String(length=10), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_venue_visits_id'), 'user_venue_visits', ['id'], unique=False)
    op.create_index(op.f('ix_user_venue_visits_user_id'), 'user_venue_visits', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_user_venue_visits_user_id'), table_name='user_venue_visits')
    op.drop_index(op.f('ix_user_venue_visits_id'), table_name='user_venue_visits')
    op.drop_table('user_venue_visits')
