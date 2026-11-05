"""Sprint 2A fail-closed risk file. Every path is under tmp_path."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.manager import TradeManager
from app.trading.crew.risk_agent import RiskAgent, risk_lock_path
from app.trading.post_market_crew import safety_wall_agent
from app.trading.post_market_crew.safety_wall_agent import SafetyWallAgent


_ACCOUNTING = (
    "daily_losses_count",
    "daily_cumulative_r",
    "safety_wall_reserve",
    "is_safety_wall_locked",
    "session_date",
)
_REQUIRED_RECORD = (
    "trade_id",
    "journal_exit_reference",
    "broker_confirmed_exit_quantity",
    "broker_confirmed_exit_price",
    "broker_confirmed_net_pnl",
    "risk_accounting_status",
    "reconciled_at",
    "operator",
    "action_reference",
    "resumption_scope",
    "resumption_authorized_by",
    "resumption_authorized_at",
)


def _ledger(**overrides: object) -> dict:
    body: dict = {
        "base_capital": 50000.0,
        "safety_wall_reserve": 10.0,
        "daily_losses_count": 0,
        "daily_cumulative_r": 0.0,
        "is_safety_wall_locked": False,
        "session_date": "2026-11-04",
        "reserve_basis": "index_points_x_0.5_frozen",
    }
    body.update(overrides)
    return body


def _write(path: Path, body: dict) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    path.write_bytes(payload)
    return payload


def _bars() -> pd.DataFrame:
    return pd.DataFrame({
        "open": [23000.0],
        "high": [23010.0],
        "low": [22950.0],
        "close": [22970.0],
        "volume": [1000],
    })


def _open_book() -> dict:
    return {
        "T1": {
            "trade_id": "T1",
            "symbol": "NIFTY",
            "direction": "CALL",
            "entry_price": 23000.0,
            "stop_loss": 22980.0,
            "target_price": 23120.0,
            "risk_points": 20.0,
            "lots": 1,
            "quantity": 65,
            "suggested_strike": "NIFTY 23000 CE",
            "timestamp": "2026-11-04 10:00:00",
            "status": "OPEN",
            "strategy": "LIQUIDITY_SWEEP",
        }
    }


def _tick(tm: TradeManager) -> dict:
    return tm.on_market_tick(
        cmp=22970.0,
        df_5m=_bars(),
        pdh=23100.0,
        pdl=22900.0,
        date_str="2026-11-04",
        current_time_str="10:15",
    )


def _manager(tmp_path: Path, risk: bytes | None) -> tuple[TradeManager, Path]:
    trades = tmp_path / "trades"
    trades.mkdir(parents=True)
    if risk is not None:
        (trades / "daily_risk_config.json").write_bytes(risk)
    journal = JournalAgent(trades_dir=trades, db_path=tmp_path / "journal.db")
    journal.save_active_trades(_open_book(), "2026-11-04")
    tm = TradeManager(trades_dir=trades, db_path=tmp_path / "journal.db")
    return tm, trades


def _silence_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.db.trade_store.upsert_trade", lambda trade, db_path=None: "T1")


def _id_in_document(document: dict, trade_id: str) -> bool:
    for key in ("loss_counted_trade_ids", "reserve_posted_for"):
        items = document.get(key)
        if isinstance(items, list) and trade_id in items:
            return True
    return False


def _accounting_same(document: dict, before: dict) -> bool:
    return all(document.get(key) == before.get(key) for key in _ACCOUNTING)


def classify_unposted_replace(
    ledger: dict | None,
    before: dict,
    trade_id: str,
    broker_values: tuple | None,
) -> str:
    """Section 3.5 labels. Empty means the record is not a finished status."""
    if not isinstance(ledger, dict) or _id_in_document(ledger, trade_id):
        return ""
    if not _accounting_same(ledger, before):
        return ""
    if broker_values is None:
        return "UNPOSTED"
    if any(value is None for value in broker_values):
        return "BROKER_UNCONFIRMED"
    return "RECONCILED_UNPOSTED"


def classify_posted_durability(
    parsed: dict | None,
    expected: dict,
    trade_id: str,
) -> str:
    """Section 3.6. A hash without parsed fields cannot take the label."""
    if not isinstance(parsed, dict) or not _id_in_document(parsed, trade_id):
        return ""
    if any(parsed.get(key) != value for key, value in expected.items()):
        return ""
    return "POSTED_DURABILITY_UNCONFIRMED"


def _operator_record(status: str, trade_id: str, broker: dict) -> dict:
    assert status != "entries_allowed"
    scope = "reload_only" if status == "RECONCILED_UNPOSTED" else "none"
    operator = "Ada"
    signed_at = "2026-11-04T12:00:00+05:30"
    record = {
        "trade_id": trade_id,
        "journal_exit_reference": {
            "path": "executed-log",
            "trade_id": trade_id,
            "exit_timestamp": "2026-11-04 10:15:00",
        },
        "broker_confirmed_exit_quantity": broker.get("qty"),
        "broker_confirmed_exit_price": broker.get("price"),
        "broker_confirmed_net_pnl": broker.get("pnl"),
        "risk_accounting_status": status,
        "reconciled_at": signed_at,
        "operator": operator,
        "action_reference": (
            "outcome left unposted; active row not restored; entries stay off"
            if status != "POSTED_DURABILITY_UNCONFIRMED"
            else "replacement completed; outcome not posted again; entries stay off; nothing replays"
        ),
        "resumption_scope": scope,
        "resumption_authorized_by": operator if scope == "reload_only" else "",
        "resumption_authorized_at": signed_at if scope == "reload_only" else "",
    }
    if scope == "reload_only":
        assert record["resumption_authorized_by"] == record["operator"]
    return record


def test_b01_corrupt_file_blocks_session_outcome_and_exit(tmp_path, monkeypatch):
    _silence_store(monkeypatch)
    cfg = tmp_path / "risk.json"
    raw = b"{"
    cfg.write_bytes(raw)
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is False
    assert ra.save_config() is False
    assert ra.evaluate_order(23000.0, 22970.0).approved is False
    assert ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8) is False
    assert cfg.read_bytes() == raw

    tm, trades = _manager(tmp_path / "mgr", raw)
    exits = {"n": 0}
    alerts = {"n": 0}
    real_exit = tm.journal_agent.record_exit

    def _exit(*args, **kwargs):
        exits["n"] += 1
        return real_exit(*args, **kwargs)

    def _alert(*args, **kwargs):
        alerts["n"] += 1
        return False

    tm.journal_agent.record_exit = _exit
    tm.alert_agent.send_exit_alert = _alert
    active = trades / ".active_trades_2026-11-04.json"
    before = active.read_bytes()
    result = _tick(tm)
    assert any(action["action"] == "EXIT_BLOCKED" for action in result["actions"])
    assert exits["n"] == 0
    assert alerts["n"] == 0
    assert "T1" in json.loads(active.read_text())
    assert b"exit_held_for_risk" not in active.read_bytes()
    assert active.read_bytes() == before
    assert (trades / "daily_risk_config.json").read_bytes() == raw


def test_b03_manual_lock_and_reserve_survive_the_new_date(tmp_path):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(
        session_date="2026-11-03",
        manual_lock=True,
        is_safety_wall_locked=True,
        safety_wall_reserve=77.5,
        daily_losses_count=2,
    ))
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is True
    saved = json.loads(cfg.read_text())
    assert saved["manual_lock"] is True
    assert saved["is_safety_wall_locked"] is True
    assert saved["safety_wall_reserve"] == 77.5
    assert ra.evaluate_order(23000.0, 22970.0).approved is False


def test_b04_replace_failure_false_and_none_are_not_success(tmp_path, monkeypatch):
    cfg = tmp_path / "risk.json"
    raw = _write(cfg, _ledger(session_date="2026-11-03", daily_losses_count=2))
    ra = RiskAgent(config_path=cfg)

    def _explode(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr("app.trading.crew.risk_agent.os.replace", _explode)
    assert ra.begin_session(date(2026, 11, 4)) is False
    assert ra.daily_losses_count == 2
    assert ra.session_date == "2026-11-03"
    assert cfg.read_bytes() == raw
    assert ra.evaluate_order(23000.0, 22970.0).approved is False
    monkeypatch.undo()

    other = tmp_path / "other.json"
    _write(other, _ledger(session_date="2026-11-03", daily_losses_count=2))
    rb = RiskAgent(config_path=other)
    rb.save_config = lambda: False
    assert rb.begin_session(date(2026, 11, 4)) is False
    assert rb.session_date == "2026-11-03"
    assert json.loads(other.read_text())["daily_losses_count"] == 2

    third = tmp_path / "third.json"
    _write(third, _ledger(session_date="2026-11-03", daily_losses_count=2))
    rc = RiskAgent(config_path=third)
    rc.save_config = lambda: None
    assert rc.begin_session(date(2026, 11, 4)) is False
    assert rc.session_date == "2026-11-03"
    assert rc.session_blocked is True


def test_b26h_same_date_keeps_count_and_does_not_write(tmp_path):
    cfg = tmp_path / "risk.json"
    raw = _write(cfg, _ledger(daily_losses_count=2))
    ra = RiskAgent(config_path=cfg)
    assert ra.session_blocked is False
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 2
    assert cfg.read_bytes() == raw


def test_b26i_failed_save_keeps_the_old_date_and_unreadable_bytes(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{")
    ra = RiskAgent(config_path=bad)
    assert ra.begin_session(date(2026, 11, 4)) is False
    assert bad.read_bytes() == b"{"
    assert b"daily_losses_count" not in bad.read_bytes()

    good = tmp_path / "good.json"
    _write(good, _ledger(session_date="2026-11-03", daily_losses_count=2))
    rb = RiskAgent(config_path=good)
    rb.save_config = lambda: False
    assert rb.begin_session(date(2026, 11, 4)) is False
    assert rb.session_date == "2026-11-03"
    saved = json.loads(good.read_text())
    assert saved["daily_losses_count"] == 2
    assert saved["session_date"] == "2026-11-03"


def test_j01_journal_refusal_does_not_post_or_drop_the_active_id(tmp_path, monkeypatch):
    _silence_store(monkeypatch)
    raw = json.dumps(_ledger()).encode("utf-8")
    tm, trades = _manager(tmp_path, raw)
    outcomes = {"n": 0}
    real_outcome = tm.risk_agent.record_trade_outcome

    def _outcome(*args, **kwargs):
        outcomes["n"] += 1
        return real_outcome(*args, **kwargs)

    tm.risk_agent.record_trade_outcome = _outcome
    tm.journal_agent.record_exit = lambda *args, **kwargs: None
    active = trades / ".active_trades_2026-11-04.json"
    risk = trades / "daily_risk_config.json"
    risk_before = risk.read_bytes()
    result = _tick(tm)
    assert any(action["action"] == "EXIT_JOURNAL_REFUSED" for action in result["actions"])
    assert outcomes["n"] == 0
    assert "T1" in json.loads(active.read_text())
    assert risk.read_bytes() == risk_before
    executed = trades / "live_executed_trades.json"
    assert not executed.exists()


def test_r01_risk_save_failure_after_journal_does_not_post_twice(tmp_path, monkeypatch):
    _silence_store(monkeypatch)
    raw = json.dumps(_ledger()).encode("utf-8")
    tm, trades = _manager(tmp_path, raw)
    outcomes = {"n": 0}
    real_outcome = tm.risk_agent.record_trade_outcome

    def _outcome(*args, **kwargs):
        outcomes["n"] += 1
        return real_outcome(*args, **kwargs)

    tm.risk_agent.record_trade_outcome = _outcome
    tm.risk_agent.save_config = lambda: False
    risk = trades / "daily_risk_config.json"
    risk_before = risk.read_bytes()
    first = _tick(tm)
    assert any(action["action"] == "EXIT_RISK_UNPERSISTED" for action in first["actions"])
    executed = json.loads((trades / "live_executed_trades.json").read_text())
    assert [row["trade_id"] for row in executed] == ["T1"]
    active = json.loads((trades / ".active_trades_2026-11-04.json").read_text())
    assert "T1" not in active
    assert risk.read_bytes() == risk_before
    assert outcomes["n"] == 1

    second = _tick(tm)
    assert outcomes["n"] == 1
    again = json.loads((trades / "live_executed_trades.json").read_text())
    assert [row["trade_id"] for row in again] == ["T1"]
    assert not any(action["action"] == "EXIT_RISK_UNPERSISTED" for action in second["actions"])
    assert risk.read_bytes() == risk_before


def test_m01_reconciliation_record_does_not_post_or_authorize_entries(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    outside = tmp_path / "operator"
    outside.mkdir()
    before = _ledger(daily_losses_count=1, safety_wall_reserve=15.0)
    cfg = ledger_dir / "daily_risk_config.json"
    raw = _write(cfg, before)
    incident = outside / "incident.json"
    trade_id = "T-m01"
    status = classify_unposted_replace(before, before, trade_id, (None, 10.0, 1.0))
    assert status == "BROKER_UNCONFIRMED"
    record = _operator_record(status, trade_id, {"qty": None, "price": 10.0, "pnl": 1.0})
    assert record["risk_accounting_status"] != "entries_allowed"
    for key in _REQUIRED_RECORD:
        assert key in record
    incident.write_text(json.dumps(record), encoding="utf-8")

    filled = classify_unposted_replace(before, before, trade_id, (1, 10.0, 1.0))
    assert filled == "RECONCILED_UNPOSTED"
    omitted = classify_unposted_replace(before, before, trade_id, None)
    assert omitted == "UNPOSTED"
    changed = dict(before)
    changed["daily_losses_count"] = 9
    assert classify_unposted_replace(changed, before, trade_id, None) == ""
    assert classify_unposted_replace(changed, before, trade_id, (1, 10.0, 1.0)) == ""

    ra = RiskAgent(config_path=cfg)
    assert ra.config_path != incident
    assert not str(incident).startswith(str(ra.config_path.parent) + "/")
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert cfg.read_bytes() == raw
    assert ra.daily_losses_count == 1


def test_m02_posted_durability_needs_parsed_fields_and_does_not_post(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    outside = tmp_path / "operator"
    outside.mkdir()
    trade_id = "T-m02"
    expected = {
        "daily_losses_count": 1,
        "daily_cumulative_r": -0.8,
        "safety_wall_reserve": 10.0,
        "is_safety_wall_locked": False,
        "session_date": "2026-11-04",
    }
    body = _ledger(**expected, loss_counted_trade_ids=[trade_id])
    cfg = ledger_dir / "daily_risk_config.json"
    raw = _write(cfg, body)
    assert classify_posted_durability(None, expected, trade_id) == ""
    assert classify_posted_durability(body, expected, trade_id) == "POSTED_DURABILITY_UNCONFIRMED"
    record = _operator_record("POSTED_DURABILITY_UNCONFIRMED", trade_id, {"qty": None, "price": None, "pnl": None})
    assert record["resumption_scope"] == "none"
    assert record["resumption_authorized_by"] == ""
    assert record["risk_accounting_status"] != "RECONCILED_UNPOSTED"
    incident = outside / "incident.json"
    incident.write_text(json.dumps(record), encoding="utf-8")

    mismatched = dict(body)
    mismatched["daily_losses_count"] = 4
    assert classify_posted_durability(mismatched, expected, trade_id) == ""
    absent = _ledger(**expected)
    assert classify_posted_durability(absent, expected, trade_id) == ""
    try:
        parsed = json.loads(b"{")
    except json.JSONDecodeError:
        parsed = None
    assert classify_posted_durability(parsed, expected, trade_id) == ""

    ra = RiskAgent(config_path=cfg)
    assert ra.config_path != incident
    assert not str(incident).startswith(str(cfg.parent) + "/")
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert cfg.read_bytes() == raw


def test_f01_directory_fsync_failure_keeps_the_new_document(tmp_path, monkeypatch):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(session_date="2026-11-03", daily_losses_count=2))

    def _boom(directory: Path) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr("app.trading.crew.risk_agent._fsync_directory", _boom)
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is False
    assert ra.directory_durability_unconfirmed is True
    assert ra.session_date == "2026-11-04"
    assert ra.daily_losses_count == 0
    disk = json.loads(cfg.read_text())
    assert disk["session_date"] == "2026-11-04"
    assert disk["daily_losses_count"] == 0
    frozen = cfg.read_bytes()
    assert ra.begin_session(date(2026, 12, 1)) is False
    assert ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8) is False
    assert cfg.read_bytes() == frozen
    assert ra.session_date == "2026-11-04"
    assert ra.daily_losses_count == 0


def test_w01_locked_outcomes_do_not_lose_an_update_or_overwrite_a_newer_file(tmp_path):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(daily_losses_count=0))
    agents = [RiskAgent(config_path=cfg), RiskAgent(config_path=cfg)]
    barrier = threading.Barrier(2)
    errors: list[object] = []

    def _worker(agent: RiskAgent) -> None:
        try:
            barrier.wait(timeout=5)
            saved = agent.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8)
            if saved is not True:
                errors.append(saved)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert json.loads(cfg.read_text())["daily_losses_count"] == 2

    stale = tmp_path / "stale.json"
    before = _write(stale, _ledger(daily_losses_count=7))
    ra = RiskAgent(config_path=stale)
    ra.daily_losses_count = 1
    assert ra.save_config() is False
    assert stale.read_bytes() == before
    assert ra.daily_losses_count == 7


def test_s01_priced_safety_wall_does_not_write_either_file(tmp_path, monkeypatch):
    assert safety_wall_agent.PREMIUM_PNL_METHODS == frozenset()
    monkeypatch.setattr(
        safety_wall_agent,
        "PREMIUM_PNL_METHODS",
        frozenset({"broker_premium_net"}),
    )
    state = tmp_path / "safety_wall_state.json"
    risk = tmp_path / "daily_risk_config.json"
    agent = SafetyWallAgent(base_capital=50_000.0, state_file=state, config_file=risk)
    agent.reserve_balance = 123.0
    agent.current_lots = 4
    summary = agent.process_daily_pnl([
        {"pnl_method": "broker_premium_net", "pnl_rupees": 80.0},
    ])
    assert summary.today_net_pnl == 80.0
    assert summary.added_to_reserve == 0.0
    assert summary.current_reserve_balance == 123.0
    assert summary.tomorrow_approved_lots == 4
    assert agent.reserve_balance == 123.0
    assert agent.current_lots == 4
    assert "not written" in summary.action_taken
    assert not state.exists()
    assert not risk.exists()


def test_same_date_blocked_session_does_not_zero_or_write(tmp_path):
    cfg = tmp_path / "risk.json"
    raw = _write(cfg, _ledger(daily_losses_count=2))
    ra = RiskAgent(config_path=cfg)
    ra.session_blocked = True
    assert ra.begin_session(date(2026, 11, 4)) is False
    assert ra.daily_losses_count == 2
    assert ra.session_blocked is True
    assert cfg.read_bytes() == raw


def test_b02_missing_date_resets_count_and_copies_marker_lists(tmp_path):
    cfg = tmp_path / "risk.json"
    body = _ledger(safety_wall_reserve=77.5, daily_cumulative_r=4.25, daily_losses_count=1)
    del body["session_date"]
    body["loss_counted_trade_ids"] = ["keep-me"]
    _write(cfg, body)
    ra = RiskAgent(config_path=cfg)
    assert ra.config_unreadable is False
    assert ra.begin_session(date(2026, 11, 5)) is True
    saved = json.loads(cfg.read_text())
    assert saved["safety_wall_reserve"] == 77.5
    assert saved["daily_cumulative_r"] == 0.0
    assert saved["daily_losses_count"] == 0
    assert saved["session_date"] == "2026-11-05"
    assert saved["loss_counted_trade_ids"] == ["keep-me"]
    assert "reserve_posted_for" not in saved


def test_b26g_first_true_zeros_count_and_the_second_call_does_not(tmp_path):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(
        session_date="2026-11-03",
        daily_losses_count=2,
        loss_counted_trade_ids=["a", "a"],
        reserve_posted_for=[],
    ))
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 0
    saved = json.loads(cfg.read_text())
    assert saved["loss_counted_trade_ids"] == ["a", "a"]
    assert saved["reserve_posted_for"] == []
    ra.daily_losses_count = 4
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert ra.daily_losses_count == 4
    again = json.loads(cfg.read_text())
    assert again["daily_losses_count"] == 0
    assert again["loss_counted_trade_ids"] == ["a", "a"]


def test_missing_file_is_not_unreadable_and_true_creates_it(tmp_path):
    cfg = tmp_path / "missing.json"
    ra = RiskAgent(config_path=cfg)
    ra.safety_wall_reserve = 42.0
    assert ra.config_unreadable is False
    assert ra.begin_session(date(2026, 11, 4)) is True
    saved = json.loads(cfg.read_text())
    assert saved["safety_wall_reserve"] == 42.0
    assert saved["session_date"] == "2026-11-04"
    assert saved["daily_losses_count"] == 0


def test_bad_shapes_are_unreadable_and_bytes_stay(tmp_path):
    cases = [
        b"[1, 2]",
        json.dumps(_ledger(safety_wall_reserve="10")).encode("utf-8"),
        json.dumps(_ledger(daily_losses_count=1.5)).encode("utf-8"),
        json.dumps(_ledger(is_safety_wall_locked="false")).encode("utf-8"),
        json.dumps(_ledger(loss_counted_trade_ids={"id": "T"})).encode("utf-8"),
    ]
    for index, raw in enumerate(cases):
        cfg = tmp_path / f"bad-{index}.json"
        cfg.write_bytes(raw)
        ra = RiskAgent(config_path=cfg)
        assert ra.config_unreadable is True
        assert ra.session_blocked is True
        assert ra.begin_session(date(2026, 11, 4)) is False
        assert cfg.read_bytes() == raw


def test_null_session_date_is_the_missing_date_reset(tmp_path):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(
        session_date=None,
        daily_losses_count=4,
        daily_cumulative_r=3.5,
        safety_wall_reserve=12.0,
    ))
    ra = RiskAgent(config_path=cfg)
    assert ra.config_unreadable is False
    assert ra.session_date is None
    assert ra.begin_session(date(2026, 11, 5)) is True
    saved = json.loads(cfg.read_text())
    assert saved["daily_losses_count"] == 0
    assert saved["daily_cumulative_r"] == 0.0
    assert saved["safety_wall_reserve"] == 12.0
    assert saved["session_date"] == "2026-11-05"


def test_risk_lock_path_is_not_with_suffix(tmp_path):
    cfg = tmp_path / "daily_risk_config.json"
    assert risk_lock_path(cfg) == cfg.parent / (cfg.name + ".lock")
    assert risk_lock_path(cfg) != cfg.with_suffix(".lock")
    ra = RiskAgent(config_path=cfg)
    assert ra.begin_session(date(2026, 11, 4)) is True
    assert risk_lock_path(cfg).exists()
    assert not cfg.with_suffix(".lock").exists()
    production = Path(__file__).resolve().parents[2] / "trades" / "daily_risk_config.json.lock"
    assert not production.exists()


def test_stale_temp_is_not_loaded_and_a_locked_save_removes_it(tmp_path):
    cfg = tmp_path / "risk.json"
    _write(cfg, _ledger(daily_losses_count=0, safety_wall_reserve=5.0))
    leftover = tmp_path / ".risk.json.stale.tmp"
    leftover.write_text("{", encoding="utf-8")
    ra = RiskAgent(config_path=cfg)
    assert ra.config_unreadable is False
    assert ra.safety_wall_reserve == 5.0
    assert ra.daily_losses_count == 0
    assert ra.record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-0.8) is True
    assert not leftover.exists()
    assert json.loads(cfg.read_text())["daily_losses_count"] == 1


def test_reset_daily_counters_delegates_and_warns(tmp_path):
    cfg = tmp_path / "risk.json"
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    _write(cfg, _ledger(session_date=today.isoformat(), daily_losses_count=2))
    ra = RiskAgent(config_path=cfg)
    with pytest.warns(DeprecationWarning):
        assert ra.reset_daily_counters() is True
    assert ra.daily_losses_count == 2
    later = date(today.year + 1, 1, 2)
    with pytest.warns(DeprecationWarning):
        assert ra.reset_daily_counters(later) is True
    assert ra.daily_losses_count == 0
    assert ra.session_date == later.isoformat()
