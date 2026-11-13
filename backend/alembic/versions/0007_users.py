"""Users and login sessions

Revision ID: 0007_users
Revises: 0006_paper_orders
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_users"
down_revision = "0006_paper_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("username", name=op.f("pk_users")),
    )
    op.create_table(
        "user_sessions",
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["username"], ["users.username"], name=op.f("fk_user_sessions_username_users")),
        sa.PrimaryKeyConstraint("token_sha256", name=op.f("pk_user_sessions")),
    )
    op.create_index(op.f("ix_user_sessions_username"), "user_sessions", ["username"])


def downgrade() -> None:
    op.drop_index(op.f("ix_user_sessions_username"), table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("users")
