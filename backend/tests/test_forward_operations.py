import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.brokers.upstox_stream import SOURCE, Response, UpstoxStream
from app.config import Settings
from app.trading import forward_fees, paper_fees
from app.trading.forward_feed import ForwardInputs, OperationalFeedAgent
from app.trading.forward_operations import report, session_evidence
from app.trading.market_validation import closed_bars
from app.trading.paper_exec import contract_quote
from app.trading.paper_risk import size
from app.trading.research_runtime import MarketView

NOW = datetime(2026, 11, 16, 4, 30, tzinfo=UTC)
KEY = "NSE_FO|12345"


def stream():
    s = UpstoxStream("test-token", authorized=True, clock=lambda: NOW)
    s.contracts = {KEY: {"expiry": "2026-11-17", "strike_price": Decimal(25000), "lot_size": 65,
                         "trading_symbol": "NIFTY26NOV25000CE", "instrument_type": "CE"}}
    s.expiry = "2026-11-17"
    return s


def packet(*, age=0, kind=1, market=2, bid=20, ask=20.05, delta=0.4):
    p = Response(type=kind, currentTs=int((NOW - timedelta(seconds=age)).timestamp() * 1000))
    p.marketInfo.segmentStatus["NSE_FO"] = market
    full = p.feeds[KEY].fullFeed.marketFF
    full.ltpc.ltp = 20
    full.marketLevel.bidAskQuote.add(bidQ=65, bidP=bid, askQ=65, askP=ask)
    full.optionGreeks.delta = delta
    return p.SerializeToString()


@pytest.mark.parametrize("kwargs", [{"age": 11}, {"age": -1}, {"kind": 0}, {"market": 3}, {"bid": 21}, {"delta": -0.5}, {"delta": 0}])
def test_stream_refuses_unusable_snapshots(kwargs):
    s = stream()
    s.accept(packet(**kwargs))
    assert not s.evidence()
    assert "liveData" not in s.chain()["optionChain"]["contracts"][0]["ce"]


def tariff_html():
    rows = {"Brokerage": "Flat ₹20 per executed order", "STT/CTT (From 1st April 2026)": "0.15% on sell side (on premium)",
            "GST": "18% (on brokerage + transaction charges + IPFT charges)", "Stamp Duty": "0.003% or ₹300 / crore on buy side",
            "Transaction charges (From 1st March 2026)": "NSE: 0.03553% (on premium)",
            "IPFT": "₹0.01 per crore on Traded value (On Premium) + GST", "SEBI Charges": "₹10/crore"}
    return "<table>" + "".join(f"<tr><td>{k}</td><td>x</td><td>x</td><td>x</td><td>{v}</td></tr>" for k, v in rows.items()) + "</table>"


def daily_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_fees, "DIRECTORY", tmp_path)
    body = tariff_html().encode()
    (tmp_path / "2026-11-16.html").write_bytes(body)
    (tmp_path / "2026-11-16.json").write_text(json.dumps({"market_day": "2026-11-16", "checked_at": NOW.isoformat(),
        "rates": forward_fees.RATES, "source": forward_fees.URL, "source_sha256": hashlib.sha256(body).hexdigest()}))


def test_stream_quote_identity_delta_and_risk_sizing_share_timestamp(tmp_path, monkeypatch):
    daily_policy(tmp_path, monkeypatch)
    s = stream()
    s.accept(packet())
    oc = s.chain()["optionChain"]
    chain = {"contracts": oc["contracts"], "expiry": s.expiry, "fetched_at": NOW}
    q = contract_quote(chain, strike=25000, option_type="CE", expiry=s.expiry, underlying="NIFTY")
    assert q is not None and q.lot_size == 65 and q.provider_instrument_id == KEY
    assert q.quote_time == NOW
    evidence = s.evidence()[q.tradingsymbol]
    assert evidence.observed_at == NOW and evidence.contract == q.tradingsymbol
    decision = size(quote=q, evidence=evidence, trusted_sources=frozenset({SOURCE}), now=NOW,
                    settings=Settings(_env_file=None), underlying_entry=Decimal(25000), underlying_stop=Decimal(24990),
                    stop_budget=Decimal(1000), gross_daily_loss=Decimal(0), existing_reservations=Decimal(0),
                    available_cash=Decimal(50000))
    assert decision.entry is not None, decision.reason
    # A later partial/old tick must never refresh either timestamp.
    s.accept(packet(age=1))
    assert s.evidence()[q.tradingsymbol].observed_at == NOW
    s.clock = lambda: NOW + timedelta(seconds=11)
    assert not s.evidence()
    assert "liveData" not in s.chain()["optionChain"]["contracts"][0]["ce"]


def test_daily_tariff_is_bounded_and_tamper_evident(tmp_path, monkeypatch):
    daily_policy(tmp_path, monkeypatch)
    spec = paper_fees.resolve_policy("2026-11-16")
    assert spec.policy_id == forward_fees.PREFIX + "2026-11-16-v1"
    assert paper_fees.is_known_policy_id(spec.policy_id)
    with pytest.raises(paper_fees.FeePolicyUnavailable):
        paper_fees.resolve_policy("2026-11-17", policy_id=spec.policy_id)
    with pytest.raises(paper_fees.FeePolicyUnavailable):
        paper_fees.resolve_policy("2026-11-17")
    (tmp_path / "2026-11-16.html").write_text(tariff_html().replace("Flat ₹20", "Flat ₹30"))
    assert not paper_fees.is_known_policy_id(spec.policy_id)
    with pytest.raises(paper_fees.FeePolicyUnavailable):
        paper_fees.resolve_policy("2026-11-16")


def test_tariff_parser_rejects_changed_or_incomplete_rates():
    assert forward_fees.validate_tariff(tariff_html()) == forward_fees.RATES
    for changed in [tariff_html().replace("0.15%", "0.20%"), tariff_html().replace("<td>IPFT</td>", "<td>unknown</td>"),
                    tariff_html().replace("From 1st March 2026", "From 1st November 2026")]:
        with pytest.raises(ValueError):
            forward_fees.validate_tariff(changed)


def test_missing_greeks_and_unknown_instruments_are_not_trusted():
    s = stream()
    p = Response.FromString(packet())
    p.feeds[KEY].fullFeed.marketFF.ClearField("optionGreeks")
    s.accept(p.SerializeToString())
    assert not s.cache
    s.contracts = {}
    s.accept(packet())
    assert not s.cache


def test_operations_requires_fresh_heartbeat(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"state": "WAITING_FOR_MARKET", "updated_at": NOW.isoformat()}))
    assert report(str(tmp_path / "journal.db"), now=NOW)["runner"] == "WAITING_FOR_MARKET"
    assert report(str(tmp_path / "journal.db"), now=NOW + timedelta(seconds=91))["runner"] == "HEARTBEAT_STALE"
    path.write_text("invalid")
    assert report(str(tmp_path / "journal.db"), now=NOW)["runner"] == "NO_HEARTBEAT"


def test_stale_news_blocks_entries_but_preserves_exit_bars():
    inputs = ForwardInputs.__new__(ForwardInputs)
    inputs.config = SimpleNamespace(news_lookback_hours=24)
    inputs.scanner = SimpleNamespace(news_agent=SimpleNamespace(cached_headlines=[]))
    inputs.stream = SimpleNamespace(evidence=dict)
    bars = pd.DataFrame([{"close": 25000}])
    inputs.feed = lambda now: MarketView(bars, ["setup"], None, [])
    inputs.headlines = [{"title": "old", "published_at": (NOW - timedelta(hours=25)).isoformat()}]
    view = inputs(NOW)
    assert not view.setups and view.df_5m is bars
    assert view.rejections["5m"] == ["RECENT_NEWS_UNAVAILABLE"]
    inputs.headlines[0]["published_at"] = NOW.isoformat()
    assert inputs(NOW).setups == ["setup"]


def test_upstox_chain_adapter_preserves_contracts_expiry_and_executable_prices():
    s = stream()
    s.accept(packet())
    inputs = ForwardInputs.__new__(ForwardInputs)
    inputs.stream = s
    adapted = inputs._baseline_chain()["optionChain"]
    assert adapted["aggregatedDetails"]["currentExpiry"] == "2026-11-17"
    contracts = adapted["optionContracts"]
    assert len(contracts) == 1
    assert contracts[0]["ce"]["liveData"]["bid"] == 20
    assert contracts[0]["ce"]["liveData"]["ask"] == 20.05
    # The adapter cannot invent a tradable price when the provider snapshot expires.
    s.clock = lambda: NOW + timedelta(seconds=11)
    assert "liveData" not in inputs._baseline_chain()["optionChain"]["optionContracts"][0]["ce"]


@pytest.mark.parametrize("minutes", [5, 15])
def test_operational_adapter_excludes_only_current_off_grid_live_price_print(minutes):
    from app.trading.option_quote import IST

    opening = NOW.astimezone(IST).replace(hour=9, minute=15)
    now = opening + timedelta(minutes=2 * minutes + 1)
    frame = pd.DataFrame([
        {"date": at.isoformat(), "open": 25000, "high": 25000, "low": 25000,
         "close": 25000, "volume": 0}
        for at in [opening, opening + timedelta(minutes=minutes), now]
    ])
    adapter = OperationalFeedAgent()
    assert closed_bars(frame, now, minutes).empty
    cleaned = adapter.candle_rows(frame, f"{minutes}m", now)
    assert len(cleaned) == 2
    assert len(closed_bars(cleaned, now, minutes)) == 2
    assert adapter.live_prints[f"{minutes}m"]["date"] == now.isoformat()
    assert len(frame) == 3  # Provider data is not overwritten.
    # An old off-grid row must remain invalid, rather than being erased as a live print.
    assert len(adapter.candle_rows(frame, f"{minutes}m", now + timedelta(minutes=minutes))) == 3
    ranged = frame.copy()
    ranged.loc[2, "high"] = 25001
    assert len(adapter.candle_rows(ranged, f"{minutes}m", now)) == 3
    future = frame.copy()
    future.loc[2, "date"] = (now + timedelta(minutes=1)).isoformat()
    assert len(adapter.candle_rows(future, f"{minutes}m", now)) == 3


def test_session_verification_needs_matching_entry_and_exit_acknowledgments(tmp_path):
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE crew_paper_trades (market_day TEXT, paper_trade_id TEXT, state TEXT);
        CREATE TABLE crew_outbox (kind TEXT, state TEXT, purpose TEXT);
        INSERT INTO crew_paper_trades VALUES ('2026-11-16','p1','CLOSED');
        INSERT INTO crew_outbox VALUES ('OPS','ACKED','paper|DAILY_STRATEGY_SUMMARY|SHARED|2026-11-16');
        INSERT INTO crew_outbox VALUES ('OPS','ACKED','paper|PAPER_ENTRY_FILLED|p1|1');
        INSERT INTO crew_outbox VALUES ('OPS','ACKED','paper|PAPER_FINAL_EXIT|different-trade|2');
    """)
    observation = {"market_day": "2026-11-16", "source": SOURCE}
    assert not session_evidence(path, "2026-11-16", observation)["verified"]
    conn.execute("INSERT INTO crew_outbox VALUES ('OPS','ACKED','paper|PAPER_FINAL_EXIT|p1|2')")
    conn.commit(); conn.close()
    before = path.read_bytes()
    assert session_evidence(path, "2026-11-16", observation)["verified"]
    assert not session_evidence(path, "2026-11-16", None)["verified"]
    assert not session_evidence(path, "2026-11-17", observation)["verified"]
    assert path.read_bytes() == before
