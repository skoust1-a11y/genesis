from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.strategies.clarify import answers_as_hints, clarifying_questions
from app.strategies.schema import parse_strategy_markdown
from tests.conftest import APPROVABLE_MARKDOWN

VAGUE = """---
name: vague
rules: {}
---
# Thesis
Buy pullbacks in an uptrend.

## Gaps
- Which timeframe defines the trend?
"""


def test_vague_strategy_gets_required_questions_and_complete_one_does_not():
    ids = {q["id"]: q for q in clarifying_questions(parse_strategy_markdown(VAGUE))}
    assert {"underlying", "structure", "entry", "stop", "target", "max_lots"} <= {i for i, q in ids.items() if q["required"]}
    assert not ids["blackout"]["required"] and ids["gap_0"]["question"] == "Which timeframe defines the trend?"

    complete = clarifying_questions(parse_strategy_markdown(APPROVABLE_MARKDOWN))
    assert not [q for q in complete if q["required"]]
    assert {"blackout", "vix_range", "max_entries_per_day"} <= {q["id"] for q in complete}


def test_answers_become_hints_and_blank_answers_are_dropped():
    qs = clarifying_questions(parse_strategy_markdown(VAGUE))
    hints = answers_as_hints(qs, {"underlying": "NIFTY", "entry": "  "})
    assert "Which index does this trade? -> NIFTY" in hints and "measurable" not in hints
    assert answers_as_hints(qs, {}) == ""


def test_extract_returns_questions_and_clarify_needs_llm(client):
    body = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": VAGUE}).json()
    assert any(q["id"] == "entry" and q["required"] for q in body["questions"])
    r = client.post("/api/ingest/clarify", json={"strategy_id": body["strategy_id"], "answers": {"entry": "x"}})
    assert r.status_code == 400


def test_verdict_flags_edge_before_costs_and_thin_samples(demo):
    report = demo["report"]
    for r in report["scenario_results"]:
        assert r["edge_before_costs"] == (r["backtest"]["gross_pnl"] > 0 and r["forward"]["gross_pnl"] > 0)
        assert isinstance(r["too_few_trades"], bool)
    assert report["verdict"]["scenarios_with_too_few_trades"] == sum(r["too_few_trades"] for r in report["scenario_results"])


def test_net_positions_average_cost_and_flip():
    from app.paper import net_positions

    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def order(i, side, lots, price):
        return SimpleNamespace(
            underlying="NIFTY", instrument="FUT", strike=None, expiry=None, lot_size=75, side=side, lots=lots,
            price=price, charges={"total": 10.0}, created_at=t0 + timedelta(minutes=i),
        )  # fmt: skip

    [p] = net_positions([order(0, "BUY", 1, 100), order(1, "BUY", 1, 110), order(2, "SELL", 3, 120)])
    assert p["realized_pnl"] == 150 * 15  # 150 units closed 15 above the 105 average
    assert p["quantity"] == -75 and p["avg_price"] == 120 and p["charges"] == 30


def test_parse_yahoo_chart_ist_times_and_gaps():
    from app.api.market import parse_yahoo_chart

    payload = {"chart": {"result": [{
        "timestamp": [1759981500, 1759981800],  # 2025-10-09 03:45 and 03:50 UTC
        "indicators": {"quote": [{"open": [1, 2], "high": [3, 4], "low": [0.5, 1], "close": [2, None], "volume": [500, 0]}]},
    }]}}  # fmt: skip
    assert parse_yahoo_chart(payload) == [
        {"date": "2025-10-09 09:15", "open": 1, "high": 3, "low": 0.5, "close": 2, "volume": 500}
    ]
    assert parse_yahoo_chart({"chart": {"result": None}}) == []


def test_resolve_segments_and_quote_parsing():
    import pytest
    from fastapi import HTTPException

    from app.api.market import parse_quote, resolve

    assert resolve("nifty") == ("index", "^NSEI")
    assert resolve("GOLD") == ("commodity", "GC=F")
    assert resolve("M&M") == ("stock", "M&M.NS")
    with pytest.raises(HTTPException):
        resolve("../etc")
    meta = {"regularMarketPrice": 110.0, "chartPreviousClose": 100.0, "currency": "INR", "regularMarketTime": 1759981500}
    q = parse_quote("RELIANCE", "stock", {"chart": {"result": [{"meta": meta}]}})
    assert (q["change"], q["change_pct"], q["time"]) == (10.0, 10.0, "2025-10-09 09:15")


def test_trading_levels_use_previous_session():
    from app.api.market import trading_levels

    prev = {"date": "2025-10-08", "open": 100, "high": 110, "low": 90, "close": 105}
    today = {"date": "2025-10-09", "open": 106, "high": 200, "low": 1, "close": 150}
    levels = {lv["label"]: lv["price"] for lv in trading_levels([prev, today])}
    p = (110 + 90 + 105) / 3
    assert (levels["PDH"], levels["PDL"], levels["PDC"]) == (110, 90, 105)
    assert levels["P"] == round(p, 2)
    assert (levels["R1"], levels["S1"]) == (round(2 * p - 90, 2), round(2 * p - 110, 2))
    assert (levels["R2"], levels["S2"]) == (round(p + 20, 2), round(p - 20, 2))
    assert trading_levels([today]) == []


def test_paper_orders_by_segment(client, monkeypatch):
    from app.api import market

    monkeypatch.setattr(market, "fetch_quotes", lambda symbols: {s: {"price": 1010.0, "time": "t"} for s in symbols})

    def order(**kw):
        return client.post("/api/paper/orders", json={"side": "BUY", "lots": 10, "price": 1000} | kw)

    assert order(underlying="GOLD", instrument="EQ").status_code == 400  # commodities are view-only
    assert order(underlying="NIFTY", instrument="EQ").status_code == 400
    assert order(underlying="RELIANCE", instrument="FUT").status_code == 400
    buy = order(underlying="reliance", instrument="EQ").json()
    sell = order(underlying="RELIANCE", instrument="EQ", side="SELL", lots=4).json()
    assert buy["underlying"] == "RELIANCE" and buy["lot_size"] == 1
    assert buy["charges"]["stt"] == sell["charges"]["stt"] * 2.5 == 10.0  # delivery STT on both sides
    [p] = client.get("/api/paper/positions").json()["positions"]
    assert p["quantity"] == 6 and p["unrealized_pnl"] == 60.0 and p["mark_price"] == 1010.0


def test_paper_order_flow(client):
    bad = client.post("/api/paper/orders", json={"underlying": "NIFTY", "instrument": "CE", "side": "BUY", "lots": 1, "price": 50})
    assert bad.status_code == 422
    for side, price in (("BUY", 24000), ("SELL", 24100)):
        r = client.post(
            "/api/paper/orders",
            json={"underlying": "NIFTY", "instrument": "FUT", "side": side, "lots": 1, "price": price},
        )
        assert r.status_code == 200, r.text
        assert r.json()["charges"]["total"] > 0
    book = client.get("/api/paper/positions").json()
    [p] = book["positions"]
    assert p["quantity"] == 0 and p["realized_pnl"] == 100 * p["lot_size"]
    assert book["net_pnl"] == round(p["realized_pnl"] - p["charges"], 2)
    assert len(client.get("/api/paper/orders").json()["orders"]) == 2
    assert client.get("/api/audit/verify").json()["ok"] is True
