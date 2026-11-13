"""Hand-calculated accounting oracles, including loss capacity across partials and restarts."""
from __future__ import annotations

from copy import deepcopy
from decimal import ROUND_DOWN, Decimal, localcontext
import random

import pytest

from app.trading import paper_accounting as pa

D = Decimal
DAY = "2026-11-06"


def event(seq, side, quantity, price, fee, *, before=None, after="OPEN", day=DAY, slip="0.00"):
    return {"seq": seq, "side": side, "qty": quantity, "price_inr": price, "charges_inr": fee,
            "slippage_inr": slip, "from_state": before, "to_state": after,
            "event_type": "ENTRY" if side == "BUY" else "EXIT" if after == "CLOSED" else "PARTIAL_EXIT",
            "occurred_at": f"{day}T10:{seq:02}:00+05:30",
            "charges": {"brokerage": fee, "stt": "0.00", "exchange_txn": "0.00", "sebi": "0.00",
                        "stamp_duty": "0.00", "gst": "0.00", "slippage": "0.00", "total": fee}}


def entry(qty=2, price="1500.00", fee="25.00"):
    return event(1, "BUY", qty, price, fee)


def project(events, exit_cost="25.00"):
    return pa.project_trade(events, liquidation_cost=D(exit_cost))


def test_versioned_cost_snapshot_must_match_its_persisted_hash():
    from app.trading.paper_fees import order_charges
    receipt = order_charges(price=D("100"), quantity=50, side="BUY", market_day=DAY)
    row = entry(qty=50, price="100.00", fee=receipt["total"])
    row["charges"] = {**receipt["components"], "slippage": "0.00", "total": receipt["total"],
                      "policy": receipt["policy"], "policy_hash": receipt["policy_hash"]}
    assert project([row]).charge_discrepancies == ()
    row["charges"]["policy"]["rates"]["gst"] = "0"
    with pytest.raises(pa.AccountingError):
        project([row])


def test_historical_policy_hash_compatibility_and_intra_trade_consistency():
    from app.trading.paper_fees import order_charges

    for day, hist in [("2025-01-15", True), ("2026-03-16", True), ("2026-04-15", True), ("2026-11-06", False)]:
        buy_r = order_charges(
            price=D("100"), quantity=150, side="BUY", market_day=day, allow_historical_brokerage_assumption=hist
        )
        sell_r = order_charges(
            price=D("110"), quantity=150, side="SELL", market_day=day, allow_historical_brokerage_assumption=hist
        )
        e1 = event(1, "BUY", 150, "100.00", buy_r["total"], day=day)
        e1["charges"] = {
            **buy_r["components"], "slippage": "0.00", "total": buy_r["total"],
            "policy": buy_r["policy"], "policy_hash": buy_r["policy_hash"],
        }
        e2 = event(2, "SELL", 150, "110.00", sell_r["total"], before="OPEN", after="CLOSED", day=day)
        e2["charges"] = {
            **sell_r["components"], "slippage": "0.00", "total": sell_r["total"],
            "policy": sell_r["policy"], "policy_hash": sell_r["policy_hash"],
        }
        tp = project([e1, e2])
        assert tp.charge_discrepancies == ()
        assert tp.charges == D(buy_r["total"]) + D(sell_r["total"])

    # Switching cost policy mid-trade must fail closed
    march_buy = order_charges(
        price=D("100"), quantity=150, side="BUY", market_day="2026-03-31", allow_historical_brokerage_assumption=True
    )
    april_sell = order_charges(
        price=D("110"), quantity=150, side="SELL", market_day="2026-04-01", allow_historical_brokerage_assumption=True
    )
    m1 = event(1, "BUY", 150, "100.00", march_buy["total"], day="2026-03-31")
    m1["charges"] = {
        **march_buy["components"], "slippage": "0.00", "total": march_buy["total"],
        "policy": march_buy["policy"], "policy_hash": march_buy["policy_hash"],
    }
    m2 = event(2, "SELL", 150, "110.00", april_sell["total"], before="OPEN", after="CLOSED", day="2026-04-01")
    m2["charges"] = {
        **april_sell["components"], "slippage": "0.00", "total": april_sell["total"],
        "policy": april_sell["policy"], "policy_hash": april_sell["policy_hash"],
    }
    with pytest.raises(pa.AccountingError, match="trade cost policy mismatch across fills"):
        project([m1, m2])

    # Event timestamp outside policy verified_from..verified_through must fail closed
    bad_day = deepcopy(m1)
    bad_day["occurred_at"] = "2026-04-01T10:01:00+05:30"
    with pytest.raises(pa.AccountingError, match="cost policy date mismatch"):
        project([bad_day])



def account(trades, day=DAY, marks=None):
    return pa.project_account(D("50000"), trades, market_day=day, net_liquidation_values=marks)


def test_entry_cash_is_not_double_counted_and_unknown_equity_is_not_zero():
    trade = project([entry()])
    state = account({"a": trade})
    assert (state.cash, state.committed, state.reserved) == (D("46975.00"), D("3025.00"), D("3050.00"))
    assert (state.realized, state.gross_daily_loss) == (D("0.00"), D("0.00"))
    assert state.equity is None and state.unrealized is None and state.unpriced == ("a",)
    assert state.capital_lower_bound == D("46950.00")
    assert state.spendable_cash == D("46950.00")  # Only ₹25 unpaid exit fees, not ₹3,025 paid basis.
    from dataclasses import replace
    assert replace(state, cash=D("24.99")).spendable_cash == D("0.00")
    assert state.stop_risk_budget == D("939.00")
    marked = account({"a": trade}, marks={"a": D("3175.00")})
    assert (marked.equity, marked.unrealized, marked.effective_capital) == (D("50150.00"), D("150.00"), D("50000.00"))
    assert marked.stop_risk_budget == D("1000.00")


def test_confirmed_partial_allocates_basis_and_keeps_gross_loss_after_runner_profit():
    events = [entry(), event(2, "SELL", 1, "1425.00", "25.00", before="OPEN", after="PARTIALLY_EXITED")]
    first = project(events)
    assert first.slices[0].allocated_basis == D("1512.50")
    assert (first.realized, first.remaining_basis, first.worst_case_reserved) == (D("-112.50"), D("1512.50"), D("1537.50"))
    events.append(event(3, "SELL", 1, "1900.00", "25.00", before="PARTIALLY_EXITED", after="CLOSED"))
    closed = project(events)
    assert (closed.quantity, closed.cashflow, closed.realized, closed.worst_case_reserved) == (0, D("250.00"), D("250.00"), D("0.00"))
    state = account({"a": closed})
    assert state.cash == state.equity == D("50250.00") and state.unrealized == D("0.00")
    assert state.gross_daily_loss == D("112.50")


def test_profits_never_replenish_gross_daily_loss_capacity():
    loss = project([entry(qty=1, price="2000.00"), event(2, "SELL", 1, "1050.00", "25.00", before="OPEN", after="CLOSED")])
    profit = project([entry(qty=1, price="2000.00"), event(2, "SELL", 1, "4050.00", "25.00", before="OPEN", after="CLOSED")])
    held = project([entry(qty=1, price="2450.00")])
    state = account({"loss": loss, "profit": profit, "held": held})
    assert state.realized == D("1000.00") and state.gross_daily_loss == D("1000.00")
    assert state.reserved == D("2500.00")
    assert state.gross_daily_loss + state.reserved + D("1500.00") == D("5000.00")
    assert state.gross_daily_loss + state.reserved + D("1500.01") > D("5000.00")


@pytest.mark.parametrize("state", ["OPEN", "EXIT_UNPRICED", "EXPIRED_UNRESOLVED"])
def test_neutral_events_and_next_day_restarts_never_release_unexecuted_risk(state):
    events = [entry()]
    neutral = event(2, None, None, None, None, before="OPEN", after=state, day="2026-11-09", slip=None)
    neutral["event_type"] = "PARTIAL_SKIPPED" if state == "OPEN" else state
    neutral["charges"] = None
    events.append(neutral)
    before = deepcopy(events)
    first, second = project(events), project(events)
    assert first == second and events == before
    assert (first.quantity, first.cashflow, first.realized, first.worst_case_reserved) == (2, D("-3025.00"), D("0.00"), D("3050.00"))
    state = account({"orphan": first}, day="2026-11-09")
    assert state.reserved == D("3050.00") and state.equity is None and state.gross_daily_loss == 0


def test_odd_paisa_basis_is_allocated_cumulatively_without_residual_loss():
    events = [entry(qty=3, price="100.00", fee="0.01")]
    for seq in (2, 3, 4):
        events.append(event(seq, "SELL", 1, "100.00", "0.00", before="OPEN" if seq == 2 else "PARTIALLY_EXITED",
                            after="CLOSED" if seq == 4 else "PARTIALLY_EXITED"))
    trade = project(events)
    assert [s.allocated_basis for s in trade.slices] == [D("100.00"), D("100.01"), D("100.00")]
    assert trade.realized == trade.cashflow == D("-0.01")
    assert trade.remaining_basis == trade.worst_case_reserved == 0


def test_slippage_is_reported_without_being_subtracted_twice():
    events = [entry(qty=1, price="100.05", fee="0.00"), event(2, "SELL", 1, "99.95", "0.00", before="OPEN", after="CLOSED", slip="0.05")]
    events[0]["slippage_inr"] = "0.05"
    trade = project(events)
    assert trade.slippage == D("0.10") and trade.realized == trade.cashflow == D("-0.10")


def test_daily_losses_follow_fill_day_in_ist_not_entry_day_or_utc_day():
    events = [entry(qty=1, price="2000.00"), event(2, "SELL", 1, "1050.00", "25.00", before="OPEN", after="CLOSED", day="2026-11-09")]
    events[-1]["occurred_at"] = "2026-11-08T23:59:00+00:00"
    trade = project(events)
    assert trade.gross_realized_loss(DAY) == 0
    assert trade.gross_realized_loss("2026-11-09") == D("1000.00")
    state = account({"a": trade}, day="2026-11-09")
    assert state.daily_realized == D("-1000.00") and state.daily_charges == D("25.00")
    assert state.total_charges == D("50.00")
    later = account({"a": trade}, day="2026-11-10")
    assert later.realized == D("-1000.00") and later.daily_realized == later.daily_charges == later.gross_daily_loss == 0


def test_rounding_does_not_depend_on_global_decimal_context():
    with localcontext() as context:
        context.prec, context.rounding = 7, ROUND_DOWN
        state = account({"a": project([entry()])})
        assert state.cash == D("46975.00") and state.reserved == D("3050.00")
    assert pa.stop_budget(D("49000"), D("50000")) == D("980.00")
    assert pa.stop_budget(D("52000"), D("50000")) == D("1000.00")
    assert pa.stop_budget(D("1.99"), D("50000")) == D("0.03")
    assert pa.stop_budget(D("-1"), D("50000")) == D("0.00")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1.001", 1.0, True, None])
def test_invalid_canonical_money_is_rejected(value):
    with pytest.raises(pa.AccountingError):
        pa.money(value)


@pytest.mark.parametrize("mutation", ["oversell", "duplicate", "missing", "neutral_money", "naive", "backwards", "negative_fee"])
def test_corrupt_financial_evidence_fails_closed(mutation):
    events = [entry(), event(2, "SELL", 2, "1525.00", "25.00", before="OPEN", after="CLOSED")]
    if mutation == "oversell":
        events[1]["qty"] = 3
    elif mutation == "duplicate":
        events[1]["seq"] = 1
    elif mutation == "missing":
        events = events[1:]
    elif mutation == "neutral_money":
        events[1]["side"] = None
    elif mutation == "naive":
        events[1]["occurred_at"] = f"{DAY}T10:02:00"
    elif mutation == "backwards":
        events[1]["occurred_at"] = f"{DAY}T09:02:00+05:30"
    else:
        events[1]["charges_inr"] = "-1.00"
    with pytest.raises(pa.AccountingError):
        project(events)


def test_legacy_charge_discrepancy_is_exposed_without_repricing_or_mutation():
    events = [entry()]
    events[0]["charges"]["gst"] = "0.01"
    before = deepcopy(events)
    trade = project(events)
    assert trade.charge_discrepancies == (1,) and trade.cashflow == D("-3025.00")
    assert events == before
    assert account({"legacy": trade}).charge_discrepancies == (("legacy", 1),)


def test_incomplete_portfolio_marks_do_not_hide_an_unpriced_hold_or_double_count_cash():
    first, second = project([entry()]), project([entry(qty=1, price="1000.00")])
    state = account({"a": first, "b": second}, marks={"a": D("3175.00")})
    assert state.unpriced == ("b",) and state.equity is None and state.unrealized is None
    assert state.cash == D("45950.00") and state.committed == D("4050.00")
    assert state.capital_lower_bound == D("49100.00") and state.stop_risk_budget == D("982.00")


def test_seeded_replay_reconciles_after_every_partial_to_one_paisa():
    rng = random.Random(637)
    for _ in range(200):
        qty = rng.randint(1, 8)
        events = [entry(qty=qty, price=str(D(rng.randint(1, 100000)) / 100), fee="23.61")]
        remaining = qty
        while remaining:
            state = account({"a": project(events)})
            assert state.cash + state.committed == D("50000") + state.realized
            sold = rng.randint(1, remaining)
            remaining -= sold
            events.append(event(len(events) + 1, "SELL", sold, str(D(rng.randint(1, 100000)) / 100), "23.61",
                                before=events[-1]["to_state"], after="PARTIALLY_EXITED" if remaining else "CLOSED"))
        closed = project(events)
        assert closed.realized == closed.cashflow and closed.remaining_basis == 0
