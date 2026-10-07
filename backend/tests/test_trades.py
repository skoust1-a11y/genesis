from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import select

from app.db.models import AuditEvent, SimTrade, SimulationRun
from app.engine.market import MARKET_DATA_PATH, load_sample_bars
from app.reports.store import TRADE_HASH_FIELDS_V3, rows_sha256, trade_hash_fields

EXIT_REASONS = {"profit_target", "stop_loss", "dte", "expiry", "window_end"}


def _runs(session, report_id):
    return session.scalars(select(SimulationRun).where(SimulationRun.report_id == report_id)).all()


def _trades(session, run_id):
    return session.scalars(
        select(SimTrade).where(SimTrade.simulation_run_id == run_id).order_by(SimTrade.seq)
    ).all()


def test_run_totals_reconcile_with_trades(session, demo):
    runs = _runs(session, demo["report_id"])
    assert len(runs) == 2 * demo["report"]["verdict"]["scenarios_tested"] > 2
    for run in runs:
        trades = _trades(session, run.id)
        assert run.trade_count == len(trades)
        assert sum((t.gross for t in trades), Decimal(0)) == run.gross_pnl
        assert sum((t.costs for t in trades), Decimal(0)) == run.total_costs
        assert sum((t.net for t in trades), Decimal(0)) == run.net_pnl
        for t in trades:
            assert sum(Decimal(v) for v in t.cost_breakdown.values()) == t.costs
            assert t.gross - t.costs == t.net
        if trades:
            assert trades[-1].equity_after == run.net_pnl
            assert max(t.drawdown_after for t in trades) == run.max_drawdown
            assert run.win_rate == sum(1 for t in trades if t.net > 0) / len(trades)
        assert trade_hash_fields(run) == TRADE_HASH_FIELDS_V3
        assert rows_sha256(trades, trade_hash_fields(run)) == run.trades_sha256
        assert run.data_source == "sample_synthetic"
        assert run.max_drawdown_mtm >= 0 and len(run.equity_curve) == run.bars_in_window


def test_audit_event_covers_trades(session, demo):
    event = session.scalars(select(AuditEvent).where(AuditEvent.action == "report.created")).one()
    trade_rows = session.scalars(select(SimTrade).where(SimTrade.report_id == demo["report_id"])).all()
    assert event.details["trade_count"] == len(trade_rows)
    stored = {r.id: r.trades_sha256 for r in _runs(session, demo["report_id"])}
    assert {s["simulation_run_id"]: s["trades_sha256"] for s in event.details["simulation_runs"]} == stored


def test_why_and_vision_columns(session, demo):
    for run in _runs(session, demo["report_id"]):
        for t in _trades(session, run.id):
            assert all(c["passed"] for c in t.entry_conditions)
            by_field = {}
            for c in t.entry_conditions:
                by_field.setdefault(c["field"], []).append(c["actual"])
            assert by_field["iv_rank"] == [t.market_snapshot["iv_rank"]]
            assert by_field["dte"] == [t.market_snapshot["dte"]] * 2
            assert t.market_snapshot["date"] == t.trade_date
            assert t.credit > 0 and t.target_pnl > 0 > t.stop_loss_pnl
            assert t.pnl_vs_target == t.net - t.target_pnl
            assert t.reason_text and t.entry_rule
            assert t.exit_reason in EXIT_REASONS and t.holding_bars >= 0
            assert t.trade_date <= t.exit_date <= run.window_end
            assert len(t.legs) == 4 and all(leg["expiry"] == t.expiry for leg in t.legs)
            if t.exit_reason != "expiry":
                assert t.exit_date <= t.expiry


def test_forward_window_covers_all_bars_after_split(session, demo):
    bars = load_sample_bars()
    split = int(len(bars) * 0.7)
    for run in _runs(session, demo["report_id"]):
        if run.mode == "forward":
            assert run.bars_in_window == len(bars) - split
            assert (run.window_start, run.window_end) == (bars[split].date, bars[-1].date)
        else:
            assert run.bars_in_window == split
            assert (run.window_start, run.window_end) == (bars[0].date, bars[split - 1].date)
        for t in _trades(session, run.id):
            assert run.window_start <= t.trade_date <= run.window_end


def test_report_inputs_capture_market_data_and_versions(client, demo):
    report = client.get(f"/api/reports/{demo['report_id']}").json()
    inputs = report["inputs"]
    assert inputs["market_data"]["sha256"] == hashlib.sha256(MARKET_DATA_PATH.read_bytes()).hexdigest()
    assert inputs["market_data"]["source"] == "sample_synthetic"
    assert inputs["engine_version"] == "4"
    assert inputs["settings_snapshot"]["verdict_min_backtest_trades"] == 3
    assert inputs["review_rules_version"] == "1"
    assert inputs["cost_profile"]["brokerage_per_order"] == 20
    assert "openai_api_key" not in str(inputs).lower()
