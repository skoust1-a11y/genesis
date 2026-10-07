"""Move live trading to its own database

Live tables now live in LIVE_DATABASE_URL (see alembic_live/). This drops them from the
research database, restricts trade_reviews to simulated trades and rebuilds trade_journal
with simulated trades only. Refuses to run while any live row exists.

Revision ID: 0004_split_live
Revises: 0003_journal
"""
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa

from app.db.triggers import append_only_sql, drop_triggers_sql

revision = "0004_split_live"
down_revision = '0003_journal'
branch_labels = None
depends_on = None

# Children before parents.
LIVE_TABLES = ['order_events', 'fills', 'broker_charges', 'orders', 'live_trades', 'trade_intents', 'deployment_events', 'deployments', 'position_snapshots', 'broker_accounts']

SIM_JOURNAL_VIEW = """
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
"""


def _sibling(filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"_genesis_migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    nonempty = [t for t in LIVE_TABLES if bind.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar()]
    if bind.execute(sa.text("SELECT COUNT(*) FROM trade_reviews WHERE live_trade_id IS NOT NULL")).scalar():
        nonempty.append("trade_reviews (live)")
    if nonempty:
        raise RuntimeError(
            "Live tables in the research database still hold rows: "
            f"{', '.join(nonempty)}. Copy them to the live database before running 0004_split_live."
        )

    op.execute("DROP VIEW IF EXISTS trade_journal")
    for stmt in drop_triggers_sql(dialect, "trade_reviews"):
        op.execute(stmt)
    with op.batch_alter_table('trade_reviews', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trade_reviews_live_trade_id'))
        batch_op.drop_constraint(batch_op.f('fk_trade_reviews_live_trade_id_live_trades'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('ck_trade_reviews_exactly_one_trade'), type_='check')
        batch_op.drop_column('live_trade_id')
        batch_op.create_check_constraint('sim_trade_only', "trade_kind = 'sim' AND sim_trade_id IS NOT NULL")
    for stmt in append_only_sql(dialect, "trade_reviews"):
        op.execute(stmt)

    for table in LIVE_TABLES:
        for stmt in drop_triggers_sql(dialect, table):
            op.execute(stmt)
        op.drop_table(table)
    op.execute(SIM_JOURNAL_VIEW)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.execute("DROP VIEW IF EXISTS trade_journal")
    _sibling("0002_live_schema.py").upgrade()

    for stmt in drop_triggers_sql(dialect, "trade_reviews"):
        op.execute(stmt)
    with op.batch_alter_table('trade_reviews', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_trade_reviews_sim_trade_only'), type_='check')
        batch_op.add_column(sa.Column('live_trade_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_trade_reviews_live_trade_id_live_trades'), 'live_trades', ['live_trade_id'], ['id']
        )
        batch_op.create_index(batch_op.f('ix_trade_reviews_live_trade_id'), ['live_trade_id'], unique=False)
        batch_op.create_check_constraint(
            'exactly_one_trade',
            "(trade_kind = 'sim' AND sim_trade_id IS NOT NULL AND live_trade_id IS NULL)"
            " OR (trade_kind = 'live' AND live_trade_id IS NOT NULL AND sim_trade_id IS NULL)",
        )
    for stmt in append_only_sql(dialect, "trade_reviews"):
        op.execute(stmt)
    _sibling("0003_journal.py")._create_journal_view(dialect)
