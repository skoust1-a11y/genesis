"""Position snapshots per deployment, capital events and drawdown events

Revision ID: 0003_drawdown
Revises: 0002_opinions
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0003_drawdown"
down_revision = "0002_opinions"
branch_labels = None
depends_on = None

APPEND_ONLY = ["capital_events", "drawdown_events"]


def upgrade() -> None:
    # A plain ADD COLUMN: rebuilding the table (batch mode) would drop its append-only triggers.
    op.add_column('position_snapshots', sa.Column('deployment_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_position_snapshots_deployment_id'), 'position_snapshots', ['deployment_id'], unique=False)

    op.create_table('capital_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('amount_inr', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_capital_events_deployment_id_deployments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_capital_events'))
    )
    with op.batch_alter_table('capital_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_capital_events_deployment_id'), ['deployment_id'], unique=False)

    op.create_table('drawdown_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=24), nullable=False),
    sa.Column('at_pct', sa.Float(), nullable=True),
    sa.Column('drawdown_pct', sa.Float(), nullable=False),
    sa.Column('drawdown_inr', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('equity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('peak', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_drawdown_events_deployment_id_deployments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_drawdown_events'))
    )
    with op.batch_alter_table('drawdown_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_drawdown_events_deployment_id'), ['deployment_id'], unique=False)

    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
    with op.batch_alter_table('drawdown_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_drawdown_events_deployment_id'))

    op.drop_table('drawdown_events')
    with op.batch_alter_table('capital_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_capital_events_deployment_id'))

    op.drop_table('capital_events')
    op.drop_index(op.f('ix_position_snapshots_deployment_id'), table_name='position_snapshots')
    # SQLite 3.35+ drops a column in place, so the append-only triggers survive.
    op.drop_column('position_snapshots', 'deployment_id')
