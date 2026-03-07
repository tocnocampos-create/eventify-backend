"""add event_products and event_community_links tables

Revision ID: 7bfb9979eb26
Revises: 5c04b14d85bc
Create Date: 2026-02-28 14:01:43.743210
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7bfb9979eb26'
down_revision: Union[str, None] = '5c04b14d85bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('event_community_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=100), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_event_community_links_event_id'), 'event_community_links', ['event_id'], unique=False)
    op.create_index(op.f('ix_event_community_links_id'), 'event_community_links', ['id'], unique=False)
    op.create_table('event_products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('price', sa.String(length=50), nullable=True),
    sa.Column('image_url', sa.String(length=500), nullable=True),
    sa.Column('purchase_url', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_event_products_event_id'), 'event_products', ['event_id'], unique=False)
    op.create_index(op.f('ix_event_products_id'), 'event_products', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_event_products_id'), table_name='event_products')
    op.drop_index(op.f('ix_event_products_event_id'), table_name='event_products')
    op.drop_table('event_products')
    op.drop_index(op.f('ix_event_community_links_id'), table_name='event_community_links')
    op.drop_index(op.f('ix_event_community_links_event_id'), table_name='event_community_links')
    op.drop_table('event_community_links')
