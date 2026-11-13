"""Manual paper orders placed from the markets screen

Revision ID: 0006_paper_orders
Revises: 0005_sim_v3
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0006_paper_orders"
down_revision = "0005_sim_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("underlying", sa.String(length=32), nullable=False),
        sa.Column("instrument", sa.String(length=8), nullable=False),
        sa.Column("strike", sa.Float(), nullable=True),
        sa.Column("expiry", sa.String(length=10), nullable=True),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("charges", sa.JSON(), nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], name=op.f("fk_paper_orders_strategy_id_strategies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_orders")),
    )
    for stmt in append_only_sql(op.get_bind().dialect.name, "paper_orders"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_triggers_sql(op.get_bind().dialect.name, "paper_orders"):
        op.execute(stmt)
    op.drop_table("paper_orders")
