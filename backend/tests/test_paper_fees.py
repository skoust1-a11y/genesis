from decimal import Decimal

import pytest

from app.trading.paper_fees import FeePolicyUnavailable, order_charges


def fees(price="100", quantity=50, side="BUY", market_day="2026-11-06"):
    return order_charges(price=Decimal(price), quantity=quantity, side=side, market_day=market_day)


def test_hand_calculated_components_and_total():
    buy = fees()
    assert buy["components"] == {"brokerage": "20.00", "stt": "0.00", "exchange_txn": "1.78",
                                 "ipft": "0.00", "sebi": "0.01", "stamp_duty": "0.15", "gst": "3.92"}
    assert buy["total"] == "25.86"
    sell = fees(side="SELL")
    assert sell["components"]["stt"] == "8.00"  # 5000 * .0015 = 7.50, rupee half up
    assert sell["total"] == "33.71"
    assert buy["policy_hash"] == sell["policy_hash"]
    for result in (buy, sell, fees(price="0", side="SELL")):
        assert sum(map(Decimal, result["components"].values())) == Decimal(result["total"])
    assert fees(price="0", side="SELL")["total"] == "23.60"


@pytest.mark.parametrize("day", ["2026-11-05", "2026-11-07", "2000-01-01"])
def test_no_silent_historical_or_future_rate_extension(day):
    with pytest.raises(FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        fees(market_day=day)


@pytest.mark.parametrize("updates", [{"price": -1}, {"price": 1.2}, {"price": Decimal("NaN")},
                                    {"quantity": 0}, {"quantity": True}, {"side": "EXERCISE"}])
def test_invalid_financial_inputs_fail_closed(updates):
    args = dict(price=Decimal("100"), quantity=50, side="BUY", market_day="2026-11-06")
    with pytest.raises(ValueError):
        order_charges(**(args | updates))


def test_snapshot_mutation_cannot_change_next_calculation():
    first = fees()
    digest = first["policy_hash"]
    first["policy"]["rates"]["gst"] = "0"
    assert fees()["policy_hash"] == digest


def test_fill_and_liquidation_reserve_use_explicit_policy():
    from datetime import datetime
    from app.trading import paper_exec as px, paper_fees as pf
    from tests.test_paper_trading import quote, SETTINGS

    now = datetime.fromisoformat("2026-11-06T10:19:00+05:30")
    fill = px.fill(quote(now), "BUY", 1, now=now, settings=SETTINGS, ticks=1, cost_policy_id=pf.POLICY_ID)
    assert fill.charges["policy"]["policy_id"] == pf.POLICY_ID
    assert fill.charges_inr == sum(Decimal(fill.charges[k]) for k in
                                 ("brokerage", "stt", "exchange_txn", "ipft", "sebi", "stamp_duty", "gst"))
    assert px.max_loss(fill, SETTINGS, "2026-11-06") == fill.value + fill.charges_inr + Decimal("23.60")
    with pytest.raises(FeePolicyUnavailable):
        px.fill(quote(now), "BUY", 1, now=now, settings=SETTINGS, ticks=1, cost_policy_id="unknown")


def test_v2_fill_does_not_depend_on_global_decimal_precision():
    from datetime import datetime
    from decimal import localcontext
    from app.trading import paper_exec as px, paper_fees as pf
    from tests.test_paper_trading import quote, SETTINGS
    now = datetime.fromisoformat("2026-11-06T10:19:00+05:30")
    args = dict(now=now, settings=SETTINGS, ticks=1, cost_policy_id=pf.POLICY_ID)
    expected = px.fill(quote(now), "BUY", 3, **args)
    expected_loss = px.max_loss(expected, SETTINGS, "2026-11-06")
    with localcontext() as context:
        context.prec = 6
        actual = px.fill(quote(now), "BUY", 3, **args)
        assert actual == expected
        assert actual.value == expected.value
        assert px.max_loss(actual, SETTINGS, "2026-11-06") == expected_loss


def test_stage_a_2026_11_06_policy_identity_and_hash_are_byte_identical():
    import hashlib
    import json
    from app.trading import paper_fees as pf

    receipt = fees(market_day="2026-11-06")
    assert receipt["policy"]["policy_id"] == "nse-index-option-groww-paper-2026-11-06-v1"
    assert "brokerage_provenance" not in receipt["policy"]
    expected_snapshot = {
        "policy_id": "nse-index-option-groww-paper-2026-11-06-v1",
        "verified_from": "2026-11-06",
        "verified_through": "2026-11-06",
        "rates": dict(pf.RATES),
        "sources": list(pf.SOURCES),
        "rounding": "HALF_UP; STT value and tax rupees; other components paisa",
        "accrual_unit": "one simulated executed order",
        "gst_basis": ["brokerage", "exchange_txn", "ipft", "sebi"],
    }
    assert receipt["policy"] == expected_snapshot
    expected_hash = hashlib.sha256(
        json.dumps(expected_snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert receipt["policy_hash"] == expected_hash


@pytest.mark.parametrize(
    "day,expected_policy_id,expected_buy_total,expected_sell_stt,expected_sell_total,expected_exch",
    [
        ("2024-10-01", "nse-index-option-historical-statutory-2024-10-01-to-2026-02-28-v1", "25.86", "5.00", "30.71", "1.75"),
        ("2026-02-28", "nse-index-option-historical-statutory-2024-10-01-to-2026-02-28-v1", "25.86", "5.00", "30.71", "1.75"),
        ("2026-03-01", "nse-index-option-historical-statutory-2026-03-01-to-2026-03-31-v1", "25.86", "5.00", "30.71", "1.78"),
        ("2026-03-31", "nse-index-option-historical-statutory-2026-03-01-to-2026-03-31-v1", "25.86", "5.00", "30.71", "1.78"),
        ("2026-04-01", "nse-index-option-historical-statutory-2026-04-01-to-2026-11-05-v1", "25.86", "8.00", "33.71", "1.78"),
        ("2026-11-05", "nse-index-option-historical-statutory-2026-04-01-to-2026-11-05-v1", "25.86", "8.00", "33.71", "1.78"),
    ],
)
def test_historical_date_dispatched_policies_with_explicit_assumption(
    day, expected_policy_id, expected_buy_total, expected_sell_stt, expected_sell_total, expected_exch
):
    from app.trading.paper_fees import HISTORICAL_BROKERAGE_PROVENANCE

    with pytest.raises(FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        order_charges(price=Decimal("100"), quantity=50, side="BUY", market_day=day)

    with pytest.raises(FeePolicyUnavailable, match="COST_POLICY_BROKERAGE_ASSUMPTION_NOT_ENABLED"):
        order_charges(
            price=Decimal("100"),
            quantity=50,
            side="BUY",
            market_day=day,
            policy_id=expected_policy_id,
            allow_historical_brokerage_assumption=False,
        )

    buy = order_charges(
        price=Decimal("100"),
        quantity=50,
        side="BUY",
        market_day=day,
        allow_historical_brokerage_assumption=True,
    )
    sell = order_charges(
        price=Decimal("100"),
        quantity=50,
        side="SELL",
        market_day=day,
        allow_historical_brokerage_assumption=True,
    )
    assert buy["policy"]["policy_id"] == expected_policy_id
    assert buy["policy"]["brokerage_provenance"] == HISTORICAL_BROKERAGE_PROVENANCE
    assert buy["policy_hash"] == sell["policy_hash"]
    assert buy["components"]["exchange_txn"] == expected_exch
    if day <= "2026-02-28":
        assert buy["components"]["ipft"] == "0.03"
        assert sell["components"]["ipft"] == "0.03"
    assert buy["total"] == expected_buy_total
    assert sell["components"]["stt"] == expected_sell_stt
    assert sell["total"] == expected_sell_total
    assert sum(map(Decimal, buy["components"].values())) == Decimal(buy["total"])
    assert sum(map(Decimal, sell["components"].values())) == Decimal(sell["total"])


@pytest.mark.parametrize("unverified_day", ["2024-09-30", "2023-04-01", "2020-07-01", "2026-11-07", "2027-01-01"])
def test_unverified_historical_and_future_dates_fail_closed_even_with_assumption_flag(unverified_day):
    with pytest.raises(FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        order_charges(
            price=Decimal("100"),
            quantity=50,
            side="BUY",
            market_day=unverified_day,
            allow_historical_brokerage_assumption=True,
        )


def test_distinct_policy_hashes_across_all_four_tariff_regimes():
    days = [
        ("2025-01-15", True),
        ("2026-03-16", True),
        ("2026-04-15", True),
        ("2026-11-06", False),
    ]
    hashes = []
    ids = []
    for day, hist in days:
        res = order_charges(
            price=Decimal("100"),
            quantity=50,
            side="SELL",
            market_day=day,
            allow_historical_brokerage_assumption=hist,
        )
        ids.append(res["policy"]["policy_id"])
        hashes.append(res["policy_hash"])
    assert len(set(ids)) == 4
    assert len(set(hashes)) == 4
    # Cross-date policy mismatch must fail closed
    with pytest.raises(FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        order_charges(
            price=Decimal("100"),
            quantity=50,
            side="BUY",
            market_day="2026-04-15",
            policy_id="nse-index-option-historical-statutory-2026-03-01-to-2026-03-31-v1",
            allow_historical_brokerage_assumption=True,
        )
