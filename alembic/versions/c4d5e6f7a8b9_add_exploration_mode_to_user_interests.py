"""add exploration_mode to user_interests

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-03-20 01:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop old unique constraint (category was NOT NULL)
    op.drop_constraint('uq_user_interest', 'user_interests', type_='unique')

    # Make category nullable so exploration-mode-only rows can exist without a category
    op.alter_column('user_interests', 'category', nullable=True)

    # Add exploration_mode column
    op.add_column('user_interests', sa.Column('exploration_mode', sa.String(50), nullable=True))

    # Re-create unique constraint using NULLS NOT DISTINCT (PostgreSQL 15+)
    # so (user_id, NULL, NULL, "Espontaneo") is treated as a unique combination
    op.execute(
        'CREATE UNIQUE INDEX uq_user_interest '
        'ON user_interests (user_id, category, subtype, exploration_mode) '
        'NULLS NOT DISTINCT'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_user_interest')
    op.drop_column('user_interests', 'exploration_mode')
    op.alter_column('user_interests', 'category', nullable=False)
    op.create_unique_constraint(
        'uq_user_interest', 'user_interests', ['user_id', 'category', 'subtype']
    )
