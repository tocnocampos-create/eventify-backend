"""add scraper and profile fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Events table
    op.add_column('events', sa.Column('source_url', sa.String(500), nullable=True))
    op.add_column('events', sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('events', sa.Column('scraped_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('events', sa.Column('kids_friendly', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('events', sa.Column('age_restriction', sa.Integer(), nullable=True))

    # Venues table
    op.add_column('venues', sa.Column('source_url', sa.String(500), nullable=True))
    op.add_column('venues', sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('venues', sa.Column('scraped_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('venues', sa.Column('accessibility_features', sa.Text(), nullable=True))

    # Users table
    op.add_column('users', sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Users table
    op.drop_column('users', 'last_active_at')

    # Venues table
    op.drop_column('venues', 'accessibility_features')
    op.drop_column('venues', 'scraped_at')
    op.drop_column('venues', 'is_verified')
    op.drop_column('venues', 'source_url')

    # Events table
    op.drop_column('events', 'age_restriction')
    op.drop_column('events', 'kids_friendly')
    op.drop_column('events', 'scraped_at')
    op.drop_column('events', 'is_verified')
    op.drop_column('events', 'source_url')
