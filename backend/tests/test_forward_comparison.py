"""Isolated tests for the new experimental account; no network, real state or messages."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.brokers.upstox_stream import SOURCE
from app.config import Settings
from app.telegram_transport import UNKNOWN, DeliveryOutcome
from app.trading import forward_comparison as comparison
from app.trading import forward_zone_candidate as zone
from app.trading.forward_candidate_account import CandidateAccount
from app.trading.option_quote import IST
from app.trading.paper_risk import DeltaEvidence
from tests.test_forward_operations import daily_policy

NOW = datetime(2026, 11, 16, 10, 1, tzinfo=IST)


def raw(now=NOW, spot=25000):
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return pd.DataFrame(
        [
            {
                "date": (start + timedelta(minutes=i)).isoformat(),
                "open": spot,
                "high": spot + 1,
                "low": spot - 1,
                "close": spot,
                "volume": 0,
            }
            for i in range(int((now - start).total_seconds() / 60))
        ]
    )


def quote(now=NOW, price=20, lot=65):
    symbol = "NIFTY26NOV25100CE"
    chain = {
        "expiry": "2026-11-17",
        "fetched_at": now,
        "contracts": [
            {
                "underlying": "NIFTY",
                "strike": 25100,
                "ce": {
                    "marketLot": lot,
                    "growwContractId": "NSE_FO|12345",
                    "tradingSymbol": symbol,
                    "liveData": {
                        "ltp": price,
                        "bid": str(price),
                        "ask": str(price + 0.05),
                        "quote_time": now.isoformat(),
                    },
                },
            }
        ],
    }
    evidence = {symbol: DeltaEvidence(Decimal(".4"), now, symbol, SOURCE)}
    return chain, evidence


@pytest.fixture
def config(tmp_path, monkeypatch):
    fees = tmp_path / "fees"
    fees.mkdir()
    daily_policy(fees, monkeypatch)
    (tmp_path / "holidays").mkdir()
    (tmp_path / "lot_sizes").mkdir()
    (tmp_path / "holidays/nse.yaml").write_text(
        "holidays:\n - {date: 2026-11-10, name: TEST_ONLY}\n"
    )
    (tmp_path / "lot_sizes/nse.yaml").write_text(
        "changes:\n - {underlying: NIFTY, from: 2026-01-01, lot_size: 65}\n"
    )
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        live_trading_enabled=False,
        sandbox_allow_live=False,
    )


def signal(now=NOW, id="s1"):
    return {
        "signal_id": id,
        "version": zone.VERSION,
        "day": now.date().isoformat(),
        "side": "CALL",
        "zone_center": 25000,
        "signal_at": now.isoformat(),
        "entry_spot": 25000,
        "stop_spot": 24990,
        "target_spot": 25030,
        "risk_points": 10,
    }


def tick(
    account,
    config,
    monkeypatch,
    *,
    now=NOW,
    spot=25000,
    news=True,
    fee=True,
    lot=65,
    price=20,
    evidence_age=0,
    sig=None,
):
    sig = sig or signal()
    monkeypatch.setattr(zone, "advance", lambda rows, state: (state, [sig]))
    chain, evidence = quote(now, price, lot)
    if evidence_age:
        for k, e in list(evidence.items()):
            evidence[k] = DeltaEvidence(
                e.value, now - timedelta(seconds=evidence_age), k, SOURCE
            )
    from app.trading.forward_fees import policy

    return account.tick(
        now=now,
        raw_1m=raw(now, spot),
        chain=chain,
        evidence=evidence,
        news_safe=news,
        fee_policy=policy(now.date().isoformat()).policy_id if fee else None,
    )


def test_fresh_fill_dedup_restart_and_priced_exit(config, tmp_path, monkeypatch):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    tick(a, config, monkeypatch)
    tick(a, config, monkeypatch)
    assert a.stats()["entries"] == 1
    assert a.stats()["equity_inr"] is None
    a.close()
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    tick(a, config, monkeypatch, now=NOW + timedelta(minutes=1), spot=25035, price=25)
    s = a.stats()
    assert s["entries"] == s["closed"] == 1
    assert Decimal(s["closed_net_pnl_inr"]) > 0
    assert Decimal(s["equity_inr"]) == Decimal(50000) + Decimal(s["closed_net_pnl_inr"])
    a.close()


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"news": False}, "NEWS_UNAVAILABLE_OR_UNSAFE"),
        ({"fee": False}, "FEE_POLICY_UNVERIFIED"),
        ({"lot": 75}, "LOT_MISMATCH"),
        ({"evidence_age": 11}, "STOP_RISK_UNVERIFIABLE"),
        ({"price": 100}, "FULL_PREMIUM_LIMIT"),
        ({"sig": signal(NOW - timedelta(minutes=5))}, "SIGNAL_STALE_AT_RESTART"),
    ],
)
def test_unverified_inputs_never_fill(config, tmp_path, monkeypatch, kwargs, reason):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    result = tick(a, config, monkeypatch, **kwargs)
    assert reason in result["reasons"]
    assert a.stats()["entries"] == 0
    assert a.stats()["cash_inr"] == "50000.00"
    a.close()


def test_unpriced_exit_survives_restart_and_blocks_new_entry(
    config, tmp_path, monkeypatch
):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    tick(a, config, monkeypatch)
    later = NOW + timedelta(minutes=1)
    # No fee proof prevents the exit from becoming an invented priced closure.
    tick(a, config, monkeypatch, now=later, spot=25035, fee=False)
    assert a.stats()["unresolved"] == 1
    a.close()
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    chain, evidence = quote(later)
    chain["contracts"] = []
    monkeypatch.setattr(
        zone, "advance", lambda rows, state: (state, [signal(later, id="s2")])
    )
    result = a.tick(
        now=later,
        raw_1m=raw(later, 25035),
        chain=chain,
        evidence=evidence,
        news_safe=True,
        fee_policy="missing",
    )
    assert a.stats()["entries"] == 1
    assert a.stats()["unresolved"] == 1
    assert "POSITION_OR_EXIT_PENDING" in result["reasons"]
    a.close()


def test_candle_gap_and_unclosed_bar_do_not_create_entries(
    config, tmp_path, monkeypatch
):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    chain, evidence = quote()
    frame = raw().drop(index=5)
    result = a.tick(
        now=NOW,
        raw_1m=frame,
        chain=chain,
        evidence=evidence,
        news_safe=True,
        fee_policy=None,
    )
    assert result["state"] == "WAITING_FOR_1M"
    assert a.stats()["entries"] == 0
    a.close()


def test_rules_and_capital_cannot_be_changed_on_existing_journal(config, tmp_path):
    path = tmp_path / "candidate.db"
    a = CandidateAccount(path, config, run_id="test")
    a.close()
    with pytest.raises(ValueError, match="changed"):
        CandidateAccount(path, config, run_id="different")
    import sqlite3

    with sqlite3.connect(path) as c:
        c.execute("UPDATE meta SET capital='60000'")
    with pytest.raises(ValueError, match="changed"):
        CandidateAccount(path, config, run_id="test")


def test_unknown_telegram_outcome_is_not_retried(config, tmp_path, monkeypatch):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    with a.connect() as c:
        c.execute("INSERT INTO outbox(key,body) VALUES ('entry:x','test')")
    import app.telegram_transport as transport

    calls = []

    class Fake:
        def __init__(self, *a):
            pass

        def send(self, chat, body):
            calls.append((chat, body))
            return DeliveryOutcome(UNKNOWN)

    monkeypatch.setattr(transport, "delivery_authorized", lambda chat: True)
    monkeypatch.setattr(transport, "TelegramTransport", Fake)
    config = config.model_copy(
        update={"telegram_delivery_enabled": True, "signals_telegram_chat_id": "-test"}
    )
    comparison.dispatch(a, config)
    comparison.dispatch(a, config)
    assert len(calls) == 1
    assert a.stats()["notification_states"] == [{"state": "UNKNOWN", "count": 1}]
    a.close()


def rows_for_zone():
    rows = []
    start = NOW.replace(hour=9, minute=15)
    for i, high in enumerate([100, 120, 99, 98, 99, 102, 98]):
        for j in range(5):
            rows.append(
                {
                    "date": (start + timedelta(minutes=i * 5 + j)).isoformat(),
                    "open": high - 12,
                    "high": high,
                    "low": high - 25,
                    "close": high - 12,
                }
            )
    rows.append(
        {
            "date": (start + timedelta(minutes=35)).isoformat(),
            "open": 120,
            "high": 124,
            "low": 108,
            "close": 112,
        }
    )
    rows.append(
        {
            "date": (start + timedelta(minutes=36)).isoformat(),
            "open": 112,
            "high": 113,
            "low": 106,
            "close": 107,
        }
    )
    return rows


def test_zone_uses_twenty_point_span_and_confirmed_pivot_then_one_minute_break():
    rows = rows_for_zone()
    state = zone.initial_state("2026-11-16")
    state, signals = zone.advance(rows[:35], state)
    assert not signals
    assert any(z["side"] == "PUT" and z["center"] == 111 for z in state["zones"])
    state, signals = zone.advance(rows[:36], state)
    assert not signals
    state, signals = zone.advance(rows, state)
    assert any(s["side"] == "PUT" and s["zone_center"] == 111 for s in signals)
    assert all(s["signal_at"] > s["zone_known_at"] for s in signals)
    assert zone.advance(rows, state)[1] == []


def test_detector_incremental_restart_matches_full_prefix_and_future_cannot_change_past():
    rows = rows_for_zone()
    _, full = zone.advance(rows, zone.initial_state("2026-11-16"))
    state = zone.initial_state("2026-11-16")
    seen = []
    for i in range(1, len(rows) + 1):
        state, new = zone.advance(rows[:i], state)
        seen += new
        state = json.loads(json.dumps(state))
    assert seen == full
    future = [
        {
            "date": (NOW.replace(hour=9, minute=52) + timedelta(minutes=i)).isoformat(),
            "open": 1000,
            "high": 1100,
            "low": 900,
            "close": 1000,
        }
        for i in range(5)
    ]
    _, all_signals = zone.advance(rows + future, zone.initial_state("2026-11-16"))
    assert all_signals[: len(full)] == full


def test_baseline_and_candidate_are_independent(config, tmp_path):
    path = tmp_path / "baseline.db"
    comparison.initialize_baseline(path, config)
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    assert comparison.baseline_stats(path)["capital_inr"] == "50000.00"
    assert a.stats()["capital_inr"] == "50000.00"
    assert comparison.baseline_stats(path)["entries"] == a.stats()["entries"] == 0
    a.close()


def test_old_entry_alerts_are_suppressed_without_network(config, tmp_path, monkeypatch):
    a = CandidateAccount(tmp_path / "candidate.db", config, run_id="test")
    with a.connect() as c:
        c.execute(
            "INSERT INTO outbox(key,body,created_at,actionable) VALUES ('entry:old','old','2000-01-01T00:00:00+00:00',1)"
        )
    import app.telegram_transport as transport

    monkeypatch.setattr(transport, "delivery_authorized", lambda chat: True)

    class NeverSend:
        def __init__(self, *args):
            pass

        def send(self, *args):
            raise AssertionError("Old alert must never be sent")

    monkeypatch.setattr(transport, "TelegramTransport", NeverSend)
    comparison.dispatch(
        a, config.model_copy(update={"telegram_delivery_enabled": True})
    )
    assert a.stats()["notification_states"] == [{"state": "SUPPRESSED", "count": 1}]
    a.close()


def test_fill_corruption_blocks_recovery_instead_of_resetting_account(
    config, tmp_path, monkeypatch
):
    path = tmp_path / "candidate.db"
    a = CandidateAccount(path, config, run_id="test")
    tick(a, config, monkeypatch)
    with a.connect() as c:
        c.execute("UPDATE trades SET state='CLOSED'")
    a.close()
    with pytest.raises(ValueError, match="fill/state mismatch"):
        CandidateAccount(path, config, run_id="test")


def test_crossing_zone_midpoint_is_not_an_outer_edge_sweep():
    rows = rows_for_zone()[:35]
    stamp = NOW.replace(hour=9, minute=50)
    rows += [
        {"date": stamp.isoformat(), "open": 110, "high": 115, "low": 108, "close": 110},
        {
            "date": (stamp + timedelta(minutes=1)).isoformat(),
            "open": 110,
            "high": 111,
            "low": 105,
            "close": 106,
        },
    ]
    _, signals = zone.advance(rows, zone.initial_state("2026-11-16"))
    assert not [s for s in signals if s["side"] == "PUT" and s["zone_center"] == 111]


def test_market_receipts_are_append_only_and_hash_the_exact_inputs(tmp_path):
    import gzip
    import hashlib

    receipt = comparison.record_inputs(
        tmp_path,
        "candidate",
        NOW,
        {"raw_1m": [{"close": 25000}], "fee_policy_id": "test-only"},
    )
    with gzip.open(receipt["file"], "rb") as stream:
        raw_bytes = stream.read()
    assert hashlib.sha256(raw_bytes).hexdigest() == receipt["sha256"]
    assert json.loads(raw_bytes)["raw_1m"] == [{"close": 25000}]
    with pytest.raises(FileExistsError):
        comparison.record_inputs(tmp_path, "candidate", NOW, {"raw_1m": []})
