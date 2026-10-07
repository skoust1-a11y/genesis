"""Trade reviews, strategy improvements and trade_journal view

Revision ID: 0003_journal
Revises: 0002_live_schema
"""
from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql, no_delete_sql

revision = "0003_journal"
down_revision = '0002_live_schema'
branch_labels = None
depends_on = None

APPEND_ONLY = ['trade_reviews', 'strategy_improvements', 'improvement_decisions']
NO_DELETE = []

LIVE_DATE = {
    "sqlite": "substr(lt.closed_at, 1, 10)",
    "postgresql": "to_char(lt.closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
}

JOURNAL_VIEW = """
CREATE VIEW trade_journal AS
SELECT
    'sim' AS kind,
    CAST(t.id AS VARCHAR(36)) AS trade_id,
    r.strategy_id AS strategy_id,
    t.strategy_version_id AS strategy_version_id,
    sv.version AS strategy_version,
    t.trade_date AS trade_date,
    sr.mode AS mode,
    t.entry_rule AS entry_rule,
    t.reason_text AS reason_text,
    t.target_pnl AS target_pnl,
    t.net AS net,
    t.pnl_vs_target AS pnl_vs_target,
    t.outcome_label AS outcome_label,
    t.report_id AS source_id,
    (SELECT tr.process_verdict FROM trade_reviews tr
      WHERE tr.sim_trade_id = t.id ORDER BY tr.id DESC LIMIT 1) AS latest_verdict,
    (SELECT tr.mistake_tags FROM trade_reviews tr
      WHERE tr.sim_trade_id = t.id ORDER BY tr.id DESC LIMIT 1) AS mistake_tags
FROM sim_trades t
JOIN simulation_runs sr ON sr.id = t.simulation_run_id
JOIN reports r ON r.id = t.report_id
JOIN strategy_versions sv ON sv.id = t.strategy_version_id
UNION ALL
SELECT
    'live',
    lt.id,
    d.strategy_id,
    lt.strategy_version_id,
    sv.version,
    {live_date},
    d.mode,
    ti.entry_rule,
    ti.reason_text,
    ti.target_pnl,
    lt.net,
    lt.pnl_vs_target,
    lt.outcome_label,
    lt.deployment_id,
    (SELECT tr.process_verdict FROM trade_reviews tr
      WHERE tr.live_trade_id = lt.id ORDER BY tr.id DESC LIMIT 1),
    (SELECT tr.mistake_tags FROM trade_reviews tr
      WHERE tr.live_trade_id = lt.id ORDER BY tr.id DESC LIMIT 1)
FROM live_trades lt
JOIN deployments d ON d.id = lt.deployment_id
JOIN strategy_versions sv ON sv.id = lt.strategy_version_id
JOIN trade_intents ti ON ti.id = lt.open_intent_id
"""


def _create_journal_view(dialect: str) -> None:
    op.execute(JOURNAL_VIEW.format(live_date=LIVE_DATE[dialect]))


def upgrade() -> None:
    op.create_table('strategy_improvements',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('strategy_id', sa.String(length=36), nullable=False),
    sa.Column('from_version_id', sa.Integer(), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('proposed_change', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('expected_impact', sa.Text(), nullable=True),
    sa.Column('proposed_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['from_version_id'], ['strategy_versions.id'], name=op.f('fk_strategy_improvements_from_version_id_strategy_versions')),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_strategy_improvements_report_id_reports')),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name=op.f('fk_strategy_improvements_strategy_id_strategies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategy_improvements'))
    )
    with op.batch_alter_table('strategy_improvements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_strategy_improvements_strategy_id'), ['strategy_id'], unique=False)

    op.create_table('improvement_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('improvement_id', sa.Integer(), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('resulting_version_id', sa.Integer(), nullable=True),
    sa.Column('decided_by', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['improvement_id'], ['strategy_improvements.id'], name=op.f('fk_improvement_decisions_improvement_id_strategy_improvements')),
    sa.ForeignKeyConstraint(['resulting_version_id'], ['strategy_versions.id'], name=op.f('fk_improvement_decisions_resulting_version_id_strategy_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_improvement_decisions'))
    )
    with op.batch_alter_table('improvement_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_improvement_decisions_improvement_id'), ['improvement_id'], unique=False)

    op.create_table('trade_reviews',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('trade_kind', sa.String(length=8), nullable=False),
    sa.Column('sim_trade_id', sa.Integer(), nullable=True),
    sa.Column('live_trade_id', sa.String(length=36), nullable=True),
    sa.Column('reviewer_type', sa.String(length=16), nullable=False),
    sa.Column('reviewer', sa.String(length=128), nullable=False),
    sa.Column('rules_version', sa.String(length=16), nullable=True),
    sa.Column('followed_plan', sa.Boolean(), nullable=True),
    sa.Column('process_verdict', sa.String(length=48), nullable=True),
    sa.Column('what_went_well', sa.Text(), nullable=True),
    sa.Column('what_went_wrong', sa.Text(), nullable=True),
    sa.Column('mistake_tags', sa.JSON(), nullable=False),
    sa.Column('lesson', sa.Text(), nullable=True),
    sa.Column('improvement_suggestion', sa.Text(), nullable=True),
    sa.Column('suggested_rule_change', sa.JSON(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('supersedes_review_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(trade_kind = 'sim' AND sim_trade_id IS NOT NULL AND live_trade_id IS NULL) OR (trade_kind = 'live' AND live_trade_id IS NOT NULL AND sim_trade_id IS NULL)", name=op.f('ck_trade_reviews_exactly_one_trade')),
    sa.ForeignKeyConstraint(['live_trade_id'], ['live_trades.id'], name=op.f('fk_trade_reviews_live_trade_id_live_trades')),
    sa.ForeignKeyConstraint(['sim_trade_id'], ['sim_trades.id'], name=op.f('fk_trade_reviews_sim_trade_id_sim_trades')),
    sa.ForeignKeyConstraint(['supersedes_review_id'], ['trade_reviews.id'], name=op.f('fk_trade_reviews_supersedes_review_id_trade_reviews')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trade_reviews'))
    )
    with op.batch_alter_table('trade_reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_reviews_live_trade_id'), ['live_trade_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_reviews_sim_trade_id'), ['sim_trade_id'], unique=False)

    dialect = op.get_bind().dialect.name
    for table in APPEND_ONLY:
        for stmt in append_only_sql(dialect, table):
            op.execute(stmt)
    for table in NO_DELETE:
        for stmt in no_delete_sql(dialect, table):
            op.execute(stmt)
    _create_journal_view(dialect)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.execute("DROP VIEW IF EXISTS trade_journal")
    for table in APPEND_ONLY + NO_DELETE:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
    op.drop_table("trade_reviews")
    op.drop_table("improvement_decisions")
    op.drop_table("strategy_improvements")
