"""LLM trade opinions and phantom trades

Revision ID: 0002_opinions
Revises: 0001_live
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0002_opinions"
down_revision = "0001_live"
branch_labels = None
depends_on = None

APPEND_ONLY = ["trade_opinions", "phantom_trades"]


def upgrade() -> None:
    op.create_table('trade_opinions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('trade_intent_id', sa.String(length=36), nullable=True),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('decision', sa.String(length=8), nullable=True),
    sa.Column('lots', sa.Integer(), nullable=True),
    sa.Column('rules_lots', sa.Integer(), nullable=False),
    sa.Column('final_lots', sa.Integer(), nullable=False),
    sa.Column('applied', sa.Boolean(), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('risk_flags', sa.JSON(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('prompt_sha256', sa.String(length=64), nullable=False),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.Column('lesson_review_ids', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_trade_opinions_deployment_id_deployments')),
    sa.ForeignKeyConstraint(['trade_intent_id'], ['trade_intents.id'], name=op.f('fk_trade_opinions_trade_intent_id_trade_intents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trade_opinions'))
    )
    with op.batch_alter_table('trade_opinions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_opinions_deployment_id'), ['deployment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_opinions_trade_intent_id'), ['trade_intent_id'], unique=False)

    op.create_table('phantom_trades',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('trade_intent_id', sa.String(length=36), nullable=False),
    sa.Column('trade_opinion_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('lots', sa.Integer(), nullable=False),
    sa.Column('legs', sa.JSON(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('exit_reason', sa.String(length=64), nullable=False),
    sa.Column('gross', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('est_costs', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('net', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_phantom_trades_deployment_id_deployments')),
    sa.ForeignKeyConstraint(['trade_intent_id'], ['trade_intents.id'], name=op.f('fk_phantom_trades_trade_intent_id_trade_intents')),
    sa.ForeignKeyConstraint(['trade_opinion_id'], ['trade_opinions.id'], name=op.f('fk_phantom_trades_trade_opinion_id_trade_opinions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_phantom_trades')),
    sa.UniqueConstraint('trade_intent_id', name=op.f('uq_phantom_trades_trade_intent_id'))
    )
    with op.batch_alter_table('phantom_trades', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_phantom_trades_deployment_id'), ['deployment_id'], unique=False)

    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
    with op.batch_alter_table('phantom_trades', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_phantom_trades_deployment_id'))

    op.drop_table('phantom_trades')
    with op.batch_alter_table('trade_opinions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trade_opinions_trade_intent_id'))
        batch_op.drop_index(batch_op.f('ix_trade_opinions_deployment_id'))

    op.drop_table('trade_opinions')
