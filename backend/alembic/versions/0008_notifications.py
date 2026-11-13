"""Notifications table for in-app reassessment and audit

Revision ID: 0008_notifications
Revises: 0007_users
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import drop_triggers_sql, no_delete_sql

revision = "0008_notifications"
down_revision = "0007_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="alert"),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(op.f("ix_notifications_created_at"), "notifications", ["created_at"])
    op.create_index(op.f("ix_notifications_deployment_id"), "notifications", ["deployment_id"])

    for stmt in no_delete_sql(op.get_bind().dialect.name, "notifications"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_triggers_sql(op.get_bind().dialect.name, "notifications"):
        op.execute(stmt)
    op.drop_index(op.f("ix_notifications_deployment_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_created_at"), table_name="notifications")
    op.drop_table("notifications")
