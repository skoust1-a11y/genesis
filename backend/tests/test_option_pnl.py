"""Option P&L from any QuoteSource: Groww's page parsed into a BrokerFeed snapshot, then priced at stop and targets."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.brokers import groww
from app.brokers.groww import GrowwQuotes
from app.config import get_settings
from app.market.feed import IST, BrokerFeed
from app.trading.option_pnl import black76, plan_pnl

SPOT, BASIS, IV = 22700.0, 80.0, 0.13
TODAY = datetime.now(IST).date()
NEAR, FAR = (TODAY + timedelta(days=6)).isoformat(), (TODAY + timedelta(days=13)).isoformat()


def page_html(expiry: str) -> str:
    expires = datetime.combine(datetime.fromisoformat(expiry).date(), time(15, 30), IST)
    years = (expires - datetime.now(IST)).total_seconds() / 86400 / 365
    rows = [
        {"strikePrice": int(k * 100)} | {
            side.lower(): {
                "growwContractId": f"NIFTY{expiry}{k}{side}", "marketLot": 65,
                "liveData": {"ltp": round(black76(SPOT + BASIS, k, years, IV, side), 2)},
            } for side in ("CE", "PE")
        }
        for k in range(22000, 23450, 50)
    ]  # fmt: skip
    data = {
        "optionChain": {
            "optionContracts": rows,
            "aggregatedDetails": {"currentExpiry": expiry, "expiryDates": [NEAR, FAR], "lotSize": 65, "freezeQty": 1800},
        },
        "company": {"liveData": {"ltp": SPOT}},
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"data": data}}})}</script>'


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def get(url, params=None, **_):
        seen.append(params)
        return SimpleNamespace(text=page_html((params or {}).get("expiry", NEAR)), raise_for_status=lambda: None)

    monkeypatch.setattr(groww.httpx, "get", get)
    return seen


def snapshot():
    return BrokerFeed(GrowwQuotes(), get_settings(), iv_rank=lambda *_: None, prev_range=lambda *_: None).snapshot("NIFTY")


def test_groww_page_becomes_a_feed_snapshot_with_one_fetch_per_expiry(calls):
    snap = snapshot()
    assert (snap.spot, snap.expiry, snap.lot_size) == (SPOT, NEAR, 65)
    assert (22700.0, "CE", NEAR) in snap.quotes and (22700.0, "PE", NEAR) in snap.quotes
    assert calls == [None, {"expiry": FAR}]  # the default page is reused for spot, instruments and quotes
    assert groww.parse_page(page_html(NEAR))["company"]["liveData"]["ltp"] == SPOT
    with pytest.raises(ValueError):
        groww.parse_page("<html></html>")


def test_plan_reprices_the_quote_and_signs_pnl_by_direction(calls):
    snap = snapshot()
    settings = get_settings()
    call = plan_pnl(snap, settings, "CE", 22700, SPOT, {"flat": SPOT, "stop": 22580, "target": 22900}, hold_days=0)
    assert abs(call["forward_basis"] - BASIS) < 0.5 and abs(call["iv_pct"] - IV * 100) < 0.5
    flat, stop, target = call["exits"]
    assert abs(call["entry_premium"] - call["quote"]) < 0.05 and abs(flat["pnl"] + flat["charges"]) < 5
    assert stop["pnl"] < 0 < target["pnl"]
    put = plan_pnl(snap, settings, "PE", 22700, SPOT, {"stop": 22900, "target": 22500})
    assert put["exits"][0]["pnl"] < 0 < put["exits"][1]["pnl"]
    with pytest.raises(ValueError, match="no quote"):
        plan_pnl(snap, settings, "CE", 30000, SPOT, {"t": SPOT})


def test_option_pnl_endpoint_defaults_to_atm_and_rejects_unknown_providers(client, calls):
    body = client.get(
        "/api/market/option-pnl", params={"entry": 22650, "stop": 22550, "target1": 22800, "target2": 22950}
    ).json()  # fmt: skip
    assert body["option"] == "22700 CE" and body["provider"] == "groww" and body["quantity"] == 65
    assert [e["exit"] for e in body["exits"]] == ["stop", "target1", "target2"]
    assert body["exits"][0]["pnl"] < 0 < body["exits"][1]["pnl"] < body["exits"][2]["pnl"]
    assert client.get("/api/market/option-pnl", params={"entry": 1, "stop": 1, "target1": 1, "provider": "x"}).status_code == 400
