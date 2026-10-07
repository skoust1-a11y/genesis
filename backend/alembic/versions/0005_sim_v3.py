"""Simulator v3: multi-day positions, legs, mark-to-market drawdown, data source

All new columns are nullable; rows written before this migration keep NULLs and keep hashing
with the legacy field list (see app.reports.store.trade_hash_fields).

Revision ID: 0005_sim_v3
Revises: 0004_split_live
"""
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0005_sim_v3"
down_revision = "0004_split_live"
branch_labels = None
depends_on = None

RUN_COLUMNS = [
    sa.Column("data_source", sa.String(length=32), nullable=True),
    sa.Column("equity_curve", sa.JSON(), nullable=True),
    sa.Column("max_drawdown_mtm", sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column("max_drawdown_days", sa.Integer(), nullable=True),
    sa.Column("by_year", sa.JSON(), nullable=True),
    sa.Column("skipped", sa.JSON(), nullable=True),
]
TRADE_COLUMNS = [
    sa.Column("legs", sa.JSON(), nullable=True),
    sa.Column("expiry", sa.String(length=10), nullable=True),
    sa.Column("exit_date", sa.String(length=10), nullable=True),
]


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    # ADD COLUMN keeps the append-only triggers and the trade_journal view in place.
    for column in RUN_COLUMNS:
        op.add_column("simulation_runs", column)
    for column in TRADE_COLUMNS:
        op.add_column("sim_trades", column)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.execute("DROP VIEW IF EXISTS trade_journal")
    for table, columns in (("sim_trades", TRADE_COLUMNS), ("simulation_runs", RUN_COLUMNS)):
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column in reversed(columns):
                batch_op.drop_column(column.name)
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)
    op.execute(_sibling("0004_split_live.py").SIM_JOURNAL_VIEW)
