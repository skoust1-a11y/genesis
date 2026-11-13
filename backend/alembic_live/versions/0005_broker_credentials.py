"""Encrypted broker credentials and Trade Crew heartbeats

Revision ID: 0005_broker_credentials
Revises: 0004_partial_trades
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0005_broker_credentials"
down_revision = "0004_partial_trades"
branch_labels = None
depends_on = None

APPEND_ONLY = ["broker_credentials", "crew_heartbeats"]


def upgrade() -> None:
    op.create_table('broker_credentials',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('broker_account_id', sa.String(length=36), nullable=False),
    sa.Column('field', sa.String(length=64), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.String(length=16), nullable=True),
    sa.Column('revoked', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['broker_accounts.id'], name=op.f('fk_broker_credentials_broker_account_id_broker_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_credentials'))
    )
    with op.batch_alter_table('broker_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_broker_credentials_broker_account_id'), ['broker_account_id'], unique=False)

    op.create_table('crew_heartbeats',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('deployment_id', sa.String(length=36), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('feed', sa.String(length=16), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=True),
    sa.Column('pid', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], name=op.f('fk_crew_heartbeats_deployment_id_deployments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_crew_heartbeats'))
    )
    with op.batch_alter_table('crew_heartbeats', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_crew_heartbeats_deployment_id'), ['deployment_id'], unique=False)

    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
    with op.batch_alter_table('crew_heartbeats', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_crew_heartbeats_deployment_id'))

    op.drop_table('crew_heartbeats')
    with op.batch_alter_table('broker_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_broker_credentials_broker_account_id'))

    op.drop_table('broker_credentials')
