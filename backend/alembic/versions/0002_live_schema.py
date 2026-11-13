"""Live trading schema (no broker connection)

Revision ID: 0002_live_schema
Revises: 0001_core_sim
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql, no_delete_sql

revision = "0002_live_schema"
down_revision = '0001_core_sim'
branch_labels = None
depends_on = None

APPEND_ONLY = ['broker_accounts', 'deployments', 'deployment_events', 'trade_intents', 'orders', 'order_events', 'fills', 'live_trades', 'broker_charges', 'position_snapshots']
NO_DELETE = []


def upgrade() -> None:
    op.create_table('broker_accounts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('broker', sa.String(length=32), nullable=False),
    sa.Column('account_ref_masked', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('default_algo_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_accounts'))
    )
    op.create_table('position_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('broker_account_id', sa.String(length=36), nullable=False),
    sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('positions', sa.JSON(), nullable=False),
    sa.Column('mtm', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['broker_accounts.id'], name=op.f('fk_position_snapshots_broker_account_id_broker_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_position_snapshots'))
    )
    with op.batch_alter_table('position_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_position_snapshots_broker_account_id'), ['broker_account_id'], unique=False)

    op.create_table('deployments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('strategy_id', sa.String(length=36), nullable=False),
    sa.Column('strategy_version_id', sa.Integer(), nullable=False),
    sa.Column('broker_account_id', sa.String(length=36), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('sebi_algo_id', sa.String(length=64), nullable=True),
    sa.Column('risk_limits', sa.JSON(), nullable=False),
    sa.Column('approved_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['broker_accounts.id'], name=op.f('fk_deployments_broker_account_id_broker_accounts')),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_deployments_strategy_id_strategies')),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], name=op.f('fk_deployments_strategy_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments'))
    )
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deployments_strategy_id'), ['strategy_id'], unique=False)

    op.create_table('deployment_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_deployment_events_deployment_id_deployments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployment_events'))
    )
    with op.batch_alter_table('deployment_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deployment_events_deployment_id'), ['deployment_id'], unique=False)

    op.create_table('trade_intents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('strategy_version_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('legs', sa.JSON(), nullable=False),
    sa.Column('entry_rule', sa.Text(), nullable=True),
    sa.Column('entry_conditions', sa.JSON(), nullable=False),
    sa.Column('reason_text', sa.Text(), nullable=False),
    sa.Column('market_snapshot', sa.JSON(), nullable=False),
    sa.Column('thesis_ref', sa.String(length=128), nullable=True),
    sa.Column('target_pnl', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('stop_loss_pnl', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('max_loss', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('risk_reward', sa.Float(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('risk_checks', sa.JSON(), nullable=False),
    sa.Column('decided_by', sa.String(length=128), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('approval', sa.String(length=16), nullable=False),
    sa.Column('approved_by', sa.String(length=128), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_trade_intents_deployment_id_deployments')),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], name=op.f('fk_trade_intents_strategy_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trade_intents'))
    )
    with op.batch_alter_table('trade_intents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_intents_deployment_id'), ['deployment_id'], unique=False)

    op.create_table('live_trades',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('open_intent_id', sa.String(length=36), nullable=False),
    sa.Column('close_intent_id', sa.String(length=36), nullable=True),
    sa.Column('strategy_version_id', sa.Integer(), nullable=False),
    sa.Column('legs', sa.JSON(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('holding_minutes', sa.Integer(), nullable=False),
    sa.Column('exit_rule', sa.Text(), nullable=True),
    sa.Column('exit_reason', sa.String(length=64), nullable=False),
    sa.Column('gross', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('est_costs', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('actual_costs', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('net', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('slippage_vs_expected', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('pnl_vs_target', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('max_adverse_excursion', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('max_favorable_excursion', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('outcome_label', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['close_intent_id'], ['trade_intents.id'], name=op.f('fk_live_trades_close_intent_id_trade_intents')),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_live_trades_deployment_id_deployments')),
    sa.ForeignKeyConstraint(['open_intent_id'], ['trade_intents.id'], name=op.f('fk_live_trades_open_intent_id_trade_intents')),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], name=op.f('fk_live_trades_strategy_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_live_trades'))
    )
    with op.batch_alter_table('live_trades', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_live_trades_deployment_id'), ['deployment_id'], unique=False)

    op.create_table('orders',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('trade_intent_id', sa.String(length=36), nullable=False),
    sa.Column('client_order_id', sa.String(length=64), nullable=False),
    sa.Column('broker_order_id', sa.String(length=64), nullable=True),
    sa.Column('exchange_order_id', sa.String(length=64), nullable=True),
    sa.Column('sebi_algo_id', sa.String(length=64), nullable=True),
    sa.Column('exchange', sa.String(length=16), nullable=False),
    sa.Column('tradingsymbol', sa.String(length=64), nullable=False),
    sa.Column('expiry', sa.String(length=10), nullable=True),
    sa.Column('strike', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('option_type', sa.String(length=4), nullable=True),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('order_type', sa.String(length=16), nullable=False),
    sa.Column('product', sa.String(length=16), nullable=False),
    sa.Column('limit_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('trigger_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['trade_intent_id'], ['trade_intents.id'], name=op.f('fk_orders_trade_intent_id_trade_intents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('client_order_id', name=op.f('uq_orders_client_order_id'))
    )
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_orders_trade_intent_id'), ['trade_intent_id'], unique=False)

    op.create_table('broker_charges',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('broker_account_id', sa.String(length=36), nullable=False),
    sa.Column('trade_date', sa.String(length=10), nullable=False),
    sa.Column('order_id', sa.String(length=36), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('breakdown', sa.JSON(), nullable=False),
    sa.Column('total', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['broker_accounts.id'], name=op.f('fk_broker_charges_broker_account_id_broker_accounts')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_broker_charges_order_id_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_charges'))
    )
    with op.batch_alter_table('broker_charges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_broker_charges_broker_account_id'), ['broker_account_id'], unique=False)

    op.create_table('fills',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.String(length=36), nullable=False),
    sa.Column('broker_trade_id', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('est_cost_breakdown', sa.JSON(), nullable=False),
    sa.Column('filled_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_fills_order_id_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fills')),
    sa.UniqueConstraint('order_id', 'broker_trade_id', name=op.f('uq_fills_order_id_broker_trade_id'))
    )
    with op.batch_alter_table('fills', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fills_order_id'), ['order_id'], unique=False)

    op.create_table('order_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('filled_qty', sa.Integer(), nullable=False),
    sa.Column('pending_qty', sa.Integer(), nullable=False),
    sa.Column('avg_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_events_order_id_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_events'))
    )
    with op.batch_alter_table('order_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_order_events_order_id'), ['order_id'], unique=False)

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
    op.drop_table("order_events")
    op.drop_table("fills")
    op.drop_table("broker_charges")
    op.drop_table("orders")
    op.drop_table("live_trades")
    op.drop_table("trade_intents")
    op.drop_table("deployment_events")
    op.drop_table("deployments")
    op.drop_table("position_snapshots")
    op.drop_table("broker_accounts")
