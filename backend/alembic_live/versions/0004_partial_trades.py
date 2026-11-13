"""Partial closes: live_trades.partial

Revision ID: 0004_partial_trades
Revises: 0003_drawdown
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_partial_trades"
down_revision = "0003_drawdown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN: rebuilding the table (batch mode) would drop its append-only triggers.
    op.add_column('live_trades', sa.Column('partial', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('live_trades', 'partial')
