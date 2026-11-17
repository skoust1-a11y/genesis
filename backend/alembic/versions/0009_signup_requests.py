"""Owner-reviewed signup requests; existing users and sessions are unchanged."""
import sqlalchemy as sa

from alembic import op

revision = "0009_signup_requests"
down_revision = "0008_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "signup_requests",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name=op.f("ck_signup_requests_status")),
    )


def downgrade():
    op.drop_table("signup_requests")
