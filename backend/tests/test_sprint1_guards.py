"""Sprint 1 guards: unpriced P&L, IST reset, parameter proposals, journal fail-closed."""

from __future__ import annotations

import builtins
import json
from datetime import date
from pathlib import Path

import pandas as pd

from app.trading.crew.exit_agent import ExitDecision
from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.manager import TradeManager
from app.trading.crew.risk_agent import RiskAgent
from app.trading.crew.entry_agent import TradeSignal
from app.trading.post_market_crew.post_market_manager import PostMarketManager


def _exit(points: float = 20.0, r: float = 0.8) -> ExitDecision:
    return ExitDecision(
        action="TARGET_HIT",
        exit_price=23020.0,
        realized_points=points,
        realized_r=r,
        exit_reason="target",
        is_fully_closed=True,
    )


def test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.trade_store.upsert_trade", lambda trade, db_path=None: "T1")
    ja = JournalAgent(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    ja.save_active_trades({
        "T1": {"trade_id": "T1", "lots": 2, "status": "OPEN", "milestones_hit": []},
    }, date_str="2026-11-04")
    closed = ja.record_exit("T1", _exit(20.0), date_str="2026-11-04", lot_qty=65)
    assert closed is not None
    assert closed["pnl_rupees"] is None
    assert closed["pnl_method"] == "index_points_not_priced"
    assert closed["realized_points"] == 20.0
    assert closed["realized_r"] == 0.8
    # The old formula was index points x 0.5 x quantity = 20 * 0.5 * 130.
    assert closed["pnl_rupees"] != 1300.0

    cfg = tmp_path / "risk.json"
    ra = RiskAgent(base_capital=50_000.0, config_path=cfg)
    ra.safety_wall_reserve = 213954.1
    ra.record_trade_outcome(
        is_win=True,
        pnl_rupees=closed["pnl_rupees"],
        realized_r=closed["realized_r"],
        pnl_method=closed["pnl_method"],
    )
    assert ra.safety_wall_reserve == 213954.1
    saved = json.loads(cfg.read_text())
    assert saved["safety_wall_reserve"] == 213954.1
    assert saved["reserve_basis"] == "index_points_x_0.5_frozen"


def test_ist_session_resets_on_date_change_and_blocks_when_save_fails(tmp_path):
    cfg = tmp_path / "risk.json"
    ra = RiskAgent(base_capital=50_000.0, default_lots=3, config_path=cfg)
    ra.daily_losses_count = 2
    ra.daily_cumulative_r = 9.65
    ra.is_safety_wall_locked = True
    ra.safety_wall_reserve = 1000.0
    ra.session_date = "2026-11-03"
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 0
    assert ra.daily_cumulative_r == 0.0
    assert ra.is_safety_wall_locked is False
    assert ra.safety_wall_reserve == 1000.0

    ra.daily_losses_count = 1
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 1

    ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-1.0)
    ra.record_trade_outcome(is_win=True, pnl_rupees=None, realized_r=0.4)
    assert ra.daily_losses_count == 0
    ra.record_trade_outcome(is_win=False, pnl_rupees=-2000.0, realized_r=0.0)
    assert ra.daily_losses_count == 0
    ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8)
    ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8)
    assert ra.evaluate_order(23000.0, 22970.0).approved is False

    ra.session_blocked = False
    ra.daily_losses_count = 0
    ra.daily_cumulative_r = 0.0
    ra.is_safety_wall_locked = False
    ra.session_date = "2026-11-04"

    def _boom() -> None:
        raise OSError("disk full")

    ra.save_config = _boom  # type: ignore[method-assign]
    assert ra.begin_session(date(2026, 11, 5)) is False
    assert ra.session_date == "2026-11-04"
    assert ra.session_blocked is True
    blocked = ra.evaluate_order(23000.0, 22970.0)
    assert blocked.approved is False
    assert "Session reset failed" in (blocked.rejection_reason or "")


def test_manual_lock_survives_the_new_session(tmp_path):
    cfg = tmp_path / "risk.json"
    cfg.write_text(json.dumps({
        "base_capital": 50000,
        "safety_wall_reserve": 10,
        "daily_losses_count": 2,
        "daily_cumulative_r": -2.5,
        "is_safety_wall_locked": True,
        "session_date": "2026-11-03",
        "manual_lock": True,
    }))
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 0
    assert ra.manual_lock is True
    assert ra.is_safety_wall_locked is True
    assert ra.evaluate_order(23000.0, 22970.0).approved is False
    saved = json.loads(cfg.read_text())
    assert saved["manual_lock"] is True
    assert saved["safety_wall_reserve"] == 10


def test_daily_review_does_not_rewrite_active_parameters(tmp_path):
    trades = tmp_path / "trades"
    trades.mkdir()
    active = trades / "active_strategy_params.json"
    body = '{"micro_wick_stop_buffer": 10, "otm_steps": 2}\n'
    active.write_text(body, encoding="utf-8")
    before = active.read_bytes()
    pmm = PostMarketManager(
        base_capital=50_000.0,
        reports_dir=tmp_path / "reports",
        trades_dir=trades,
    )
    pmm.briefing_agent.bot_token = None
    df = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5]})
    pmm.run_daily_review(
        df_intraday=df,
        pdh=2.0,
        pdl=0.5,
        date_str="2026-11-04",
        executed_trades=[{
            "trade_id": "T",
            "strategy": "LIQUIDITY_SWEEP",
            "direction": "CALL",
            "entry_price": 100.0,
            "exit_price": 80.0,
            "risk_points": 20.0,
            "realized_points": -20.0,
            "realized_r": -1.0,
            "timestamp": "2026-11-04 10:00:00",
        }],
    )
    assert active.read_bytes() == before
    log = (trades / "parameter_proposals.jsonl").read_text(encoding="utf-8")
    assert '"approval_status": "pending"' in log
    assert '"approval_status": "approved"' not in log


def test_journal_missing_file_is_an_empty_book(tmp_path):
    ja = JournalAgent(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    assert ja.load_active_trades("2026-11-04") == {}
    assert ja.load_failed is False


def test_journal_malformed_file_is_not_replaced(tmp_path):
    ja = JournalAgent(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    path = ja._get_active_file("2026-11-04")
    path.write_text("{", encoding="utf-8")
    raw = path.read_bytes()
    assert ja.load_active_trades("2026-11-04") == {}
    assert ja.load_failed is True
    ja.save_active_trades({}, "2026-11-04")
    assert path.read_bytes() == raw
    assert ja.record_exit("missing", _exit(), date_str="2026-11-04") is None
    assert path.read_bytes() == raw


def test_journal_unreadable_file_is_not_replaced(tmp_path, monkeypatch):
    ja = JournalAgent(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    path = ja._get_active_file("2026-11-04")
    path.write_text('{"T1": {"trade_id": "T1"}}', encoding="utf-8")
    raw = path.read_bytes()
    real_open = builtins.open

    def guarded(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "r" in str(mode):
            raise PermissionError("denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    assert ja.load_active_trades("2026-11-04") == {}
    assert ja.load_failed is True
    ja.save_active_trades({}, "2026-11-04")
    assert path.read_bytes() == raw


def test_journal_failed_load_refuses_entry_and_manager_does_not_trade(tmp_path, monkeypatch):
    trades = tmp_path / "trades"
    ja = JournalAgent(trades_dir=trades, db_path=tmp_path / "trades.db")
    path = ja._get_active_file("2026-11-04")
    path.write_text("{", encoding="utf-8")
    raw = path.read_bytes()
    sig = TradeSignal(
        strategy="Liquidity Sweep",
        symbol="NIFTY",
        direction="CALL",
        entry_price=23000.0,
        stop_loss=22975.0,
        target_price=23075.0,
        risk_points=25.0,
        target_points=75.0,
        rr_ratio=3.0,
        lots=2,
        quantity=130,
        suggested_strike="NIFTY 23000 CE",
        timestamp="2026-11-04 10:00:00",
        rationale="Test",
        regime="TRENDING_BULL",
        adx=24.0,
    )
    assert ja.record_entry(sig, date_str="2026-11-04") is None
    assert path.read_bytes() == raw

    tm = TradeManager(trades_dir=trades, db_path=tmp_path / "trades.db")
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]})
    res = tm.on_market_tick(
        cmp=23000.0,
        df_5m=df,
        pdh=23100.0,
        pdl=22900.0,
        date_str="2026-11-04",
        current_time_str="10:15",
    )
    assert any(a["action"] == "JOURNAL_UNREADABLE" for a in res["actions"])
    assert path.read_bytes() == raw
    assert not (trades / "live_executed_trades.json").exists()


def test_live_scanner_does_not_construct_an_execution_manager():
    root = Path(__file__).resolve().parents[2]
    text = (root / "scripts" / "live_scanner.py").read_text(encoding="utf-8")
    assert "ExecutionManager" not in text
    assert "Trade Action Executed" not in text
    assert "SIGNAL:" in text
    assert "is_live_broker=True" not in text
    assert "is_paper_trading=True" in text
    assert 'date_str=datetime.now(ZoneInfo("Asia/Kolkata"))' in text
