"""Core tables, simulated trades and audit log

Revision ID: 0001_core_sim
Revises: 
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql, no_delete_sql

revision = "0001_core_sim"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY = ['ingest_runs', 'strategy_versions', 'reports', 'audit_events', 'simulation_runs', 'sim_trades']
NO_DELETE = ['strategies']


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('entity_type', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events')),
    sa.UniqueConstraint('hash', name=op.f('uq_audit_events_hash')),
    sa.UniqueConstraint('prev_hash', name=op.f('uq_audit_events_prev_hash'))
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index('ix_audit_events_entity', ['entity_type', 'entity_id'], unique=False)

    op.create_table('ingest_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('source_text', sa.Text(), nullable=True),
    sa.Column('source_sha256', sa.String(length=64), nullable=True),
    sa.Column('hints', sa.Text(), nullable=True),
    sa.Column('extractor', sa.String(length=32), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('raw_output', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingest_runs'))
    )
    op.create_table('strategies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategies'))
    )
    op.create_table('strategy_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('strategy_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('markdown', sa.Text(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=False),
    sa.Column('frontmatter', sa.JSON(), nullable=False),
    sa.Column('entry_rules', sa.JSON(), nullable=False),
    sa.Column('exit_rules', sa.JSON(), nullable=False),
    sa.Column('gaps', sa.JSON(), nullable=False),
    sa.Column('ingest_run_id', sa.String(length=36), nullable=True),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['ingest_run_id'], ['ingest_runs.id'], name=op.f('fk_strategy_versions_ingest_run_id_ingest_runs')),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_strategy_versions_strategy_id_strategies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategy_versions')),
    sa.UniqueConstraint('strategy_id', 'version', name=op.f('uq_strategy_versions_strategy_id_version'))
    )
    with op.batch_alter_table('strategy_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_strategy_versions_strategy_id'), ['strategy_id'], unique=False)

    op.create_table('reports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('strategy_id', sa.String(length=36), nullable=False),
    sa.Column('strategy_version_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('verdict_label', sa.String(length=32), nullable=True),
    sa.Column('scenarios_tested', sa.Integer(), nullable=True),
    sa.Column('scenarios_passing', sa.Integer(), nullable=True),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('payload_sha256', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_reports_strategy_id_strategies')),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], name=op.f('fk_reports_strategy_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports'))
    )
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.create_index('ix_reports_strategy_started', ['strategy_id', 'started_at'], unique=False)

    op.create_table('simulation_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('scenario_index', sa.Integer(), nullable=False),
    sa.Column('scenario', sa.JSON(), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('window_start', sa.String(length=10), nullable=True),
    sa.Column('window_end', sa.String(length=10), nullable=True),
    sa.Column('bars_in_window', sa.Integer(), nullable=False),
    sa.Column('trade_count', sa.Integer(), nullable=False),
    sa.Column('gross_pnl', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('total_costs', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('net_pnl', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('win_rate', sa.Float(), nullable=False),
    sa.Column('max_drawdown', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('pass_net', sa.Boolean(), nullable=False),
    sa.Column('trades_sha256', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_simulation_runs_report_id_reports')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_simulation_runs')),
    sa.UniqueConstraint('report_id', 'scenario_index', 'mode', name=op.f('uq_simulation_runs_report_id_scenario_index_mode'))
    )
    with op.batch_alter_table('simulation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_simulation_runs_report_id'), ['report_id'], unique=False)

    op.create_table('sim_trades',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('simulation_run_id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('strategy_version_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('trade_date', sa.String(length=10), nullable=False),
    sa.Column('entry_rule', sa.Text(), nullable=False),
    sa.Column('entry_conditions', sa.JSON(), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=False),
    sa.Column('edge', sa.Float(), nullable=False),
    sa.Column('market_snapshot', sa.JSON(), nullable=False),
    sa.Column('lots', sa.Integer(), nullable=False),
    sa.Column('credit', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('max_loss', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('notional', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('orders', sa.Integer(), nullable=False),
    sa.Column('cost_breakdown', sa.JSON(), nullable=False),
    sa.Column('exit_rule', sa.Text(), nullable=False),
    sa.Column('exit_reason', sa.String(length=64), nullable=False),
    sa.Column('holding_bars', sa.Integer(), nullable=False),
    sa.Column('target_pnl', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('stop_loss_pnl', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('risk_reward', sa.Float(), nullable=False),
    sa.Column('gross', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('costs', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('net', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('pnl_vs_target', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('cost_ratio', sa.Float(), nullable=True),
    sa.Column('outcome_label', sa.String(length=16), nullable=False),
    sa.Column('equity_after', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('drawdown_after', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_sim_trades_report_id_reports')),
    sa.ForeignKeyConstraint(['simulation_run_id'], ['simulation_runs.id'], name=op.f('fk_sim_trades_simulation_run_id_simulation_runs')),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], name=op.f('fk_sim_trades_strategy_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sim_trades')),
    sa.UniqueConstraint('simulation_run_id', 'seq', name=op.f('uq_sim_trades_simulation_run_id_seq'))
    )
    with op.batch_alter_table('sim_trades', schema=None) as batch_op:
        batch_op.create_index('ix_sim_trades_report_date', ['report_id', 'trade_date'], unique=False)

    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)
    for table in NO_DELETE:
        for stmt in no_delete_sql(dialect, table):
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY + NO_DELETE:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
    op.drop_table("sim_trades")
    op.drop_table("simulation_runs")
    op.drop_table("reports")
    op.drop_table("strategy_versions")
    op.drop_table("strategies")
    op.drop_table("ingest_runs")
    op.drop_table("audit_events")
