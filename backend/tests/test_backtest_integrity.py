"""P0 backtest integrity: historical expiry and lot size, staged-exit accounting, the chronological ledger."""

import copy
import hashlib
import inspect
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.candle_integrity_fixtures import declaration, selection_view, synthetic_calendar

pytestmark = pytest.mark.usefixtures("synthetic_calendar")

from app.config import get_settings
from app.liquidity_signals import staged_exit_of
from app.market.expiry import ExpiryUnresolved, resolve_expiry, scheduled_expiry
from app.market.nse import LotChange, contract_lot_size
from app.trading import walk_forward as wf
from app.trading.option_quote import ContractQuote
from scripts import backtest_liquidity as bl
from tests.test_causal_walk_forward import (
    _make_session_candles,
    _make_test_strategy_spec,
)

BACKEND = Path(__file__).resolve().parents[1]
# Invented transition scenarios, independent of the release's flat synthetic
# lot table and deliberately incomplete closure inventory; no exchange authority.
LOTS = [LotChange("NIFTY", day, size) for day, size in (
    ("2020-01-01", 75), ("2021-06-25", 50), ("2024-04-26", 25),
    ("2024-11-20", 75), ("2026-01-01", 65))]
HOLIDAYS = {day: "SYNTHETIC_CLOSED_DAY" for day in (
    "2021-01-26", "2024-01-26", "2025-08-27", "2025-10-22", "2026-11-10")}


@pytest.fixture(autouse=True)
def synthetic_backtest_metadata(monkeypatch):
    monkeypatch.setattr(bl, "load_lot_sizes", lambda path: list(LOTS))
    monkeypatch.setattr(bl, "holidays", lambda settings: dict(HOLIDAYS))


# 1. Historical expiry: transitions and holidays


def test_expiry_follows_the_schedule_in_force_on_the_trade_date():
    assert resolve_expiry(date(2025, 11, 10), holidays=HOLIDAYS) == date(2025, 11, 11)  # Tuesday weekly
    assert resolve_expiry(date(2024, 6, 3), holidays=HOLIDAYS) == date(2024, 6, 6)  # Thursday weekly
    assert resolve_expiry(date(2025, 8, 29), holidays=HOLIDAYS) == date(2025, 9, 2)  # Thursday -> Tuesday switch
    assert scheduled_expiry(date(2018, 6, 20)) == date(2018, 6, 28)  # monthly only: last Thursday
    assert scheduled_expiry(date(2018, 6, 29)) == date(2018, 7, 26)
    assert scheduled_expiry(date(2019, 2, 11)) == date(2019, 2, 14)  # first weekly week


def test_expiry_on_a_holiday_moves_to_the_trading_day_before():
    off = HOLIDAYS | {"2025-11-11": "test holiday"}
    assert resolve_expiry(date(2025, 11, 10), holidays=off) == date(2025, 11, 10)
    # the real 2025-08-28 Thursday expiry week: Ganesh Chaturthi on Wednesday 27th does not move Thursday
    assert resolve_expiry(date(2025, 8, 25), holidays=HOLIDAYS) == date(2025, 8, 28)
    assert resolve_expiry(date(2025, 8, 25), holidays=HOLIDAYS | {"2025-08-28": "x"}) == date(2025, 8, 26)


def test_expiry_day_policy_is_explicit():
    assert resolve_expiry(date(2025, 11, 11), holidays=HOLIDAYS, policy="same_day") == date(2025, 11, 11)
    assert resolve_expiry(date(2025, 11, 11), holidays=HOLIDAYS, policy="next") == date(2025, 11, 18)
    with pytest.raises(ExpiryUnresolved):
        resolve_expiry(date(2025, 11, 11), holidays=HOLIDAYS, policy="skip")


def test_listed_contracts_are_taken_as_fact_and_unknowns_fail_closed():
    listed = {"2018-06-28", "2018-07-26"}
    assert resolve_expiry(date(2018, 6, 20), holidays={}, listed=listed) == date(2018, 6, 28)
    assert resolve_expiry(date(2025, 11, 10), holidays=HOLIDAYS, listed={"2025-11-13"}) == date(2025, 11, 13)
    with pytest.raises(ExpiryUnresolved):  # nothing listed after the day
        resolve_expiry(date(2018, 8, 1), holidays={}, listed=listed)
    with pytest.raises(ExpiryUnresolved):  # no holiday calendar for 2018
        resolve_expiry(date(2018, 6, 20), holidays=HOLIDAYS)
    with pytest.raises(ExpiryUnresolved):  # no schedule for other underlyings
        resolve_expiry(date(2025, 11, 10), underlying="BANKNIFTY", holidays=HOLIDAYS)


def _events(day, **gen_kwargs):
    gen_kwargs.setdefault("lot_changes", LOTS)
    gen_kwargs.setdefault("holidays", HOLIDAYS)
    gen = wf.RuleBasedCausalSignalGenerator(**gen_kwargs)
    return gen.generate_signals(_make_test_strategy_spec("integrity"), {}, _make_session_candles(day),
                                allowed_dates=[day])  # fmt: skip


def test_causal_generator_uses_the_historical_expiry_not_next_thursday():
    [ev, *_] = _events("2025-11-10")
    assert (ev.expiry, ev.dte_days) == ("2025-11-11", 1.0)  # was 2025-11-13, 3 days
    [ev, *_] = _events("2025-11-11", expiry_day_policy="next")
    assert ev.expiry == "2025-11-18"


def test_causal_events_carry_a_regime_v2_structure_label():
    from typing import get_args

    from app.trading.regime import StructureRegime

    events = _events("2025-11-10")
    assert events and all(ev.regime in get_args(StructureRegime) for ev in events)  # was the "CAUSAL_REGIME" placeholder


# 2-3. Historical lot size and invalid contract metadata


def test_lot_size_follows_the_trade_date():
    assert [contract_lot_size(LOTS, "NIFTY", d) for d in ("2021-01-04", "2024-06-03", "2025-02-03", "2026-11-10")] \
        == [75, 25, 75, 65]  # fmt: skip
    assert contract_lot_size(LOTS, "NIFTY", "2024-12-02", quoted=25) == 25  # old-lot contract still trading
    [ev, *_] = _events("2024-06-03")
    assert ev.lot_size == 25  # the spec says 75


def test_missing_or_inconsistent_contract_metadata_is_rejected():
    with pytest.raises(ValueError, match="no NIFTY lot size"):
        contract_lot_size(LOTS, "NIFTY", "2018-06-20")
    for bad in (75, 0, "65", True):
        with pytest.raises(ValueError):
            contract_lot_size(LOTS, "NIFTY", "2026-06-01", quoted=bad)
    with pytest.raises(wf.MissingOptionDataError, match="HISTORICAL_CONTRACT_UNRESOLVED"):
        _events("2018-06-20")  # before the dated lot table and the holiday calendar
    assert _events("2018-06-20", lot_changes=[LotChange("NIFTY", "2015-11-01", 75)],
                   listed_expiries={"2018-06-28"})[0].lot_size == 75  # injected metadata  # fmt: skip


def test_tier1_rejects_a_quote_whose_lot_contradicts_the_contract():
    def quote(underlying, strike, opt_type, expiry, ts, lot=75):
        return ContractQuote(underlying=underlying, expiry=expiry, strike=strike, option_type=opt_type,
                             provider_instrument_id="X", tradingsymbol="X", lot_size=lot, ltp=Decimal(50),
                             bid=Decimal("49.5"), ask=Decimal("50.5"), quote_time=ts, fetched_at=ts,
                             source="nse_historical", delta=0.5)  # fmt: skip

    gen = wf.RuleBasedCausalSignalGenerator()
    spec, bars = _make_test_strategy_spec("integrity"), _make_session_candles("2026-06-01")
    [ev, *_] = gen.generate_signals(spec, {}, bars, allowed_dates=["2026-06-01"], option_quote_provider=quote)
    assert ev.lot_size == 65 and ev.entry_quote.lot_size == 75
    with pytest.raises(wf.MissingOptionDataError, match="TIER_1_CONTRACT_MISMATCH"):
        wf.simulate_option_execution(ev, config=wf.WalkForwardConfig(fidelity_tier="TIER_1_VERIFIED"))


# 4-5. Staged exits settle each execution


def trade(date_, entry_t, exits, *, side="Long", pin=100.0, lot=50, risk=30.0, pstop=80.0, option="100 CE"):
    """`exits`: (time, index points, share, premium) per leg."""
    legs = [{"time": f"{date_} {t}", "points": p, "share": s, "premium": pr} for t, p, s, pr in exits]
    return {"Date": date_, "Entry time": entry_t, "Side": side, "Pool": "PDL", "Entry": 25000.0, "Option": option,
            "Risk pts": risk, "Premium in": pin, "Premium at stop": pstop, "Lot size": lot,
            "Premium out": legs[-1]["premium"], "Exit time": legs[-1]["time"][11:],
            "Points": sum(e["points"] * e["share"] for e in legs), "R": 0.0, "Exits": legs}  # fmt: skip


def charges(side, price, qty, day="2026-10-14"):
    return bl.order_charges(get_settings(), "CE", side, price, qty, day)["total"]


def settled(t):
    """Hand settlement: buy everything at entry, sell each leg's whole lots at its own premium."""
    qty, buy = t["Qty"], t["Premium in"] * t["Qty"] + charges("BUY", t["Premium in"], t["Qty"])
    sold, cash = 0, -buy
    for k, e in enumerate(t["Exits"]):
        q = qty - sold if k == len(t["Exits"]) - 1 else int(t["Lots"] * e["share"]) * t["Lot size"]
        cash += e["premium"] * q - charges("SELL", e["premium"], q) if q else 0
        sold += q
    return cash


def test_staged_partial_matches_staged_exit_of():
    assert bl.STAGED_PARTIAL == inspect.signature(staged_exit_of).parameters["partial_pct"].default


@pytest.mark.parametrize(("final_pts", "final_prem", "r"), [(30.0, 120.0, 1.5), (90.0, 170.0, 2.5)])
def test_partial_then_stop_or_target_is_settled_leg_by_leg(final_pts, final_prem, r):
    t = trade("2026-10-14", "10:00", [("10:10", 60.0, 0.5, 140.0), ("10:15", final_pts, 0.5, final_prem)])
    bl.ledger([t], ["2026-10-14"], get_settings(), 100_000, 2, book=False)
    assert [(e["Side"], e["Qty"], e["Open qty"]) for e in t["Executions"]] == [("BUY", 100, 100), ("SELL", 50, 50),
                                                                                 ("SELL", 50, 0)]  # fmt: skip
    assert t["Option Rs"] == round(settled(t))
    assert round(sum(e["Cash flow Rs"] for e in t["Executions"])) == t["Option Rs"]
    assert (t["Points"], t["R"], t["Futures Rs"]) == ((60 + final_pts) / 2, r, round((60 + final_pts) / 2 * 100))
    whole_at_final = (final_prem - 100) * 100 - charges("BUY", 100, 100) - charges("SELL", final_prem, 100)
    assert t["Option Rs"] != round(whole_at_final)  # the old method priced everything at the final exit


def test_one_lot_cannot_book_half_so_r_follows_what_executed():
    t = trade("2026-10-14", "10:00", [("10:10", 60.0, 0.5, 140.0), ("10:15", 30.0, 0.5, 120.0)])
    bl.ledger([t], ["2026-10-14"], get_settings(), 100_000, 1, book=False)
    assert [e["Qty"] for e in t["Executions"]] == [50, 50]
    assert (t["Points"], t["R"], t["Option Rs"]) == (30.0, 1.0, round(settled(t)))


def test_backtest_records_both_staged_legs(monkeypatch):
    day, s = "2025-11-12", {"side": "Long", "entry": 25000.0, "risk": 30.0, "stop": 24970.0, "target": 25090.0,
                            "time": "2025-11-12 10:00", "why": ["test"]}  # fmt: skip
    bars = [{"date": f"{day} {t}", "open": o, "high": h, "low": low, "close": c}
            for t, o, h, low, c in (("10:00", 25000, 25010, 24995, 25005), ("10:05", 25045, 25065, 25040, 25050),
                                    ("10:10", 25040, 25045, 25025, 25030))]  # fmt: skip
    monkeypatch.setattr(bl, "scan", lambda *a, **k: [dict(s)])
    trades, _ = bl.backtest(get_settings(), bars, {}, [{"date": "2025-11-11", "high": 1, "low": 0}],
                            {"2025-11-11": 12.0}, staged=True, include_retest=False,
                            include_trendline_reversal=False)  # fmt: skip
    [t] = trades
    assert [(e["time"][11:], e["points"], e["share"]) for e in t["Exits"]] == [("10:10", 60.0, 0.5),
                                                                               ("10:15", 30.0, 0.5)]  # fmt: skip
    assert (t["Points"], t["R"], t["Lot size"], t["Expiry"]) == (45.0, 1.5, 75, "2025-11-18")
    assert t["Exits"][0]["premium"] > t["Exits"][1]["premium"]


# 6-10. Chronological ledger


def test_simultaneous_entries_are_ordered_deterministically():
    a = trade("2026-10-14", "10:00", [("11:00", 10.0, 1.0, 110.0)], side="Short", pin=600.0, option="100 PE")
    b = trade("2026-10-14", "10:00", [("11:00", 10.0, 1.0, 610.0)], side="Long", pin=600.0)
    for order in ([a, b], [b, a]):
        ts = copy.deepcopy(order)
        bl.ledger(ts, ["2026-10-14"], get_settings(), 40_000, 1, book=False)
        assert [(t["Side"], t["Lots"], t["Skipped"]) for t in ts] == [("Long", 1, ""), ("Short", 0, "capital too low")]


def test_overlapping_positions_share_the_free_cash():
    a = trade("2026-10-14", "10:00", [("14:00", 50.0, 1.0, 1500.0)], pin=1200.0)  # 60,000 a lot
    b = trade("2026-10-14", "10:30", [("11:00", -10.0, 1.0, 1100.0)], side="Short", pin=1200.0, option="100 PE")
    bl.ledger([a, b], ["2026-10-14"], get_settings(), 100_000, 1, book=False)
    assert (a["Lots"], b["Lots"], b["Skipped"]) == (1, 0, "capital too low")  # old ledger: both, Rs 1.2 lakh deployed
    a2 = trade("2026-10-14", "10:00", [("10:30", 50.0, 1.0, 1500.0)], pin=1200.0)
    b2 = trade("2026-10-14", "10:30", [("11:00", -10.0, 1.0, 1100.0)], side="Short", pin=1200.0, option="100 PE")
    bl.ledger([a2, b2], ["2026-10-14"], get_settings(), 100_000, 1, book=False)
    assert b2["Lots"] == 1  # a2's exit at 10:30 settles before b2's 10:30 entry


def test_position_and_daily_limits_bind_only_when_given():
    ts = [trade("2026-10-14", f"10:0{i}", [("11:00", 5.0, 1.0, 105.0)]) for i in range(3)]
    bl.ledger(ts, ["2026-10-14"], get_settings(), 100_000, 1, book=False, max_positions=2)
    assert [t["Skipped"] for t in ts] == ["", "", "max open positions"]
    ts = [trade("2026-10-14", f"1{i}:00", [(f"1{i}:30", 5.0, 1.0, 105.0)]) for i in range(3)]
    bl.ledger(ts, ["2026-10-14"], get_settings(), 100_000, 1, book=False, max_trades_per_day=2)
    assert [t["Skipped"] for t in ts] == ["", "", "daily trade limit"]
    bl.ledger(ts, ["2026-10-14"], get_settings(), 100_000, 1, book=False)
    assert [t["Lots"] for t in ts] == [1, 1, 1]


def test_pnl_is_realised_at_exit_not_entry():
    a = trade("2026-10-14", "10:00", [("14:00", 300.0, 1.0, 900.0)])  # a big win, unknown until 14:00
    b = trade("2026-10-14", "11:00", [("11:30", 5.0, 1.0, 105.0)], pstop=80.0)
    days = ["2026-10-14"]
    solo = copy.deepcopy(b)
    bl.ledger([a, b], days, get_settings(), 100_000, 10, book=False, risk_pct=5)
    bl.ledger([trade("2026-10-14", "10:00", [("14:00", 0.0, 1.0, 100.0)]), solo], days, get_settings(),
              100_000, 10, book=False, risk_pct=5)  # fmt: skip
    assert b["Lots"] == solo["Lots"] == 4  # b's size cannot see a's outcome (old ledger: sized on Rs 2.6 lakh)
    assert b["Trading capital Rs"] < 102_000 and a["Trading capital Rs"] > 250_000  # a's win lands at 14:00 only


def test_loss_streak_counts_trades_in_exit_order():
    day, nxt = "2026-10-14", "2026-10-15"
    ts = [trade(day, "09:30", [("10:00", -10.0, 1.0, 90.0)]),
          trade(day, "09:35", [("10:05", -10.0, 1.0, 90.0)]),
          trade(day, "09:40", [("14:00", 20.0, 1.0, 120.0)]),  # opened 3rd, closes last
          trade(day, "10:30", [("11:00", -10.0, 1.0, 90.0)]),
          trade(day, "11:30", [("12:00", 5.0, 1.0, 105.0)]),
          trade(nxt, "10:00", [("10:30", 5.0, 1.0, 105.0)])]  # fmt: skip
    bl.ledger(ts, [day, nxt, "2026-10-16", "2026-10-19", "2026-10-20"], get_settings(), 100_000, 1, book=False)
    assert [t["Skipped"] for t in ts] == ["", "", "", "", "3-loss break", "3-loss break"]  # entry order: no break


def test_repeated_runs_are_identical():
    rng = random.Random(7)
    base = [trade("2026-10-14", f"1{i % 5}:{i:02d}", [(f"15:{i:02d}", rng.uniform(-30, 60), 1.0, rng.uniform(60, 160))],
                  side=rng.choice(["Long", "Short"]), pin=rng.uniform(80, 400)) for i in range(25)]  # fmt: skip
    outs = []
    for seed in (1, 2, 3):
        ts = copy.deepcopy(base)
        random.Random(seed).shuffle(ts)
        rows = bl.ledger(ts, ["2026-10-14"], get_settings(), 150_000, 2, book=True, risk_pct=3)
        outs.append((repr(ts), repr(rows)))
    assert outs[0] == outs[1] == outs[2]


def test_exit_before_entry_is_rejected():
    with pytest.raises(ValueError, match="exits at or before"):
        bl.ledger([trade("2026-10-14", "10:00", [("10:00", 5.0, 1.0, 105.0)])], ["2026-10-14"], get_settings(),
                  100_000, 1, book=False)  # fmt: skip


# 13. Profit booking: realised P&L only, never more than the free cash, every booking a ledger event


def replay(trades, capital):
    """Independent replay of every execution in ledger order (exits before entries at a minute, then trade order):
    free cash after each event matches and never goes negative, and cash + open cost + wallet = capital + realised
    P&L throughout, matching each trade's own snapshot. Returns (cash, wallet)."""
    rows = sorted((e["Time"], e["Side"] == "BUY", i, k, e) for i, t in enumerate(trades)
                  for k, e in enumerate(t["Executions"]))  # fmt: skip
    cash, wallet, realised, cost, qty = float(capital), 0.0, 0.0, {}, {}
    for n, (_, _, i, k, e) in enumerate(rows):
        cash += e["Cash flow Rs"]
        if e["Side"] == "BUY":
            cost[i], qty[i] = -e["Cash flow Rs"], e["Qty"]
        elif e["Side"] == "SELL":
            released = -trades[i]["Executions"][0]["Cash flow Rs"] * e["Qty"] / trades[i]["Qty"]
            cost[i], realised = cost[i] - released, realised + e["Cash flow Rs"] - released
        else:
            wallet -= e["Cash flow Rs"]
        assert e["Free cash Rs"] == pytest.approx(cash, abs=1e-6) and e["Free cash Rs"] >= -1e-6, e
        assert cash + sum(cost.values()) + wallet == pytest.approx(capital + realised, abs=1e-6)
        if k == len(trades[i]["Executions"]) - 1 and e["Side"] != "BUY":  # the trade's closing snapshot
            assert (trades[i]["Trading capital Rs"], trades[i]["Wallet Rs"]) == (round(cash + sum(cost.values())),
                                                                                 round(wallet))  # fmt: skip
    return cash, wallet


def test_booking_cannot_spend_cash_funding_another_position():
    """The reported case: A's partial leg funds B; at A's final leg the old rule booked Rs 20,077 more than free cash."""
    a = trade("2026-10-14", "10:00", [("10:10", 60.0, 0.5, 1500.0), ("10:30", 10.0, 0.5, 600.0)], pin=500.0)
    b = trade("2026-10-14", "10:15", [("14:00", 5.0, 1.0, 2500.0)], side="Short", pin=2400.0, option="100 PE")
    bl.ledger([a, b], ["2026-10-14"], get_settings(), 100_000, 2, book=True)
    *_, last_sell, booking = a["Executions"]
    pnl_a = sum(e["Cash flow Rs"] for e in a["Executions"][:-1])
    old = min(pnl_a, last_sell["Free cash Rs"] - b["Executions"][0]["Cash flow Rs"] - 100_000)
    assert round(last_sell["Free cash Rs"] - old) == -20_077  # what the old booking rule left as free cash
    assert booking["Side"] == "BOOK" and booking["Time"] == "2026-10-14 10:30"
    assert -booking["Cash flow Rs"] == pytest.approx(last_sell["Free cash Rs"], abs=0.01)
    assert 0 <= booking["Free cash Rs"] < 0.01
    cash, wallet = replay([a, b], 100_000)
    # the unbooked rest of A's profit is booked once B's exit frees cash: same end state as booking it all at once
    assert cash == pytest.approx(100_000, abs=0.01) and cash + wallet == pytest.approx(100_000 + pnl_a + b["Option Rs"], abs=1)
    assert b["Executions"][-1]["Side"] == "BOOK" and b["Booked Rs"] > b["Option Rs"]


def test_open_gains_and_principal_are_never_booked():
    a = trade("2026-10-14", "10:00", [("15:00", 300.0, 1.0, 400.0)], pin=200.0)  # 4x by 15:00, open until then
    b = trade("2026-10-14", "10:30", [("11:00", 10.0, 1.0, 130.0)])
    c = trade("2026-10-14", "11:30", [("12:00", 10.0, 1.0, 120.0)])
    bl.ledger([a, b, c], ["2026-10-14"], get_settings(), 100_000, 1, book=True)
    assert (b["Booked Rs"], c["Booked Rs"]) == (b["Option Rs"], c["Option Rs"])  # each realised win, net of charges
    assert abs(c["Wallet Rs"] - b["Option Rs"] - c["Option Rs"]) <= 1  # net of b's earlier booking, rounding aside
    assert c["Trading capital Rs"] == 100_000  # a's open gain is not in it; a's premium stays at cost
    replay([a, b, c], 100_000)


def test_three_lot_staged_exits_settle_whole_lots():
    t = trade("2026-10-14", "10:00", [("10:10", 60.0, 0.5, 140.0), ("10:20", 30.0, 0.5, 120.0)])
    bl.ledger([t], ["2026-10-14"], get_settings(), 100_000, 3, book=False)
    assert [(e["Side"], e["Qty"], e["Open qty"]) for e in t["Executions"]] == [("BUY", 150, 150), ("SELL", 50, 100),
                                                                                 ("SELL", 100, 0)]  # fmt: skip
    assert t["Option Rs"] == round(settled(t)) and t["Points"] == round((60 * 50 + 30 * 100) / 150, 2)
    third = 1 / 3
    t = trade("2026-10-14", "10:00", [("10:10", 30.0, third, 130.0), ("10:20", 60.0, third, 160.0),
                                      ("10:30", 0.0, third, 100.0)])  # fmt: skip
    bl.ledger([t], ["2026-10-14"], get_settings(), 100_000, 3, book=True)
    assert [e["Qty"] for e in t["Executions"] if e["Side"] == "SELL"] == [50, 50, 50]
    assert t["Option Rs"] == round(settled(t))
    replay([t], 100_000)


def test_affordability_boundary_includes_charges():
    exact = 100.0 * 50 + charges("BUY", 100.0, 50)
    for capital, lots in ((exact, 1), (exact - 0.01, 0)):
        t = trade("2026-10-14", "10:00", [("11:00", 5.0, 1.0, 105.0)])
        bl.ledger([t], ["2026-10-14"], get_settings(), capital, 1, book=False)
        assert (t["Lots"], t["Skipped"]) == (lots, "" if lots else "capital too low")
        if lots:
            assert t["Executions"][0]["Free cash Rs"] == pytest.approx(0, abs=1e-9)


def test_identical_sort_keys_replay_alike_in_any_input_order():
    a = trade("2026-10-14", "10:00", [("11:00", 10.0, 1.0, 610.0)], pin=600.0)
    b = trade("2026-10-14", "10:00", [("11:00", 10.0, 1.0, 620.0)], pin=610.0)  # same date/time/side/pool/entry
    for order in ([a, b], [b, a]):
        ts = copy.deepcopy(order)
        bl.ledger(ts, ["2026-10-14"], get_settings(), 40_000, 1, book=False)
        assert [(t["Premium in"], t["Lots"]) for t in ts] == [(600.0, 1), (610.0, 0)]


def test_staged_overlapping_booking_reconciles_and_replays_deterministically():
    rng = random.Random(11)
    base = []
    for i in range(40):
        pin = rng.uniform(60, 600)
        legs = [(f"1{4 + i % 2}:{i:02d}", rng.uniform(-40, 80), 1.0, max(pin * rng.uniform(0.3, 2.5), 0.05))]
        if rng.random() < 0.6:
            legs.insert(0, (f"1{2 + i % 2}:{i:02d}", rng.uniform(40, 90), 0.5, pin * rng.uniform(1.2, 2.5)))
        base.append(trade(f"2026-10-1{4 + i % 2}", f"1{i % 2}:{i % 30:02d}", legs, pin=pin,
                          side=rng.choice(["Long", "Short"])))  # fmt: skip
    base.append(copy.deepcopy(base[0]))  # an exact twin
    outs = []
    for seed in (1, 2, 3):
        ts = copy.deepcopy(base)
        random.Random(seed).shuffle(ts)
        rows = bl.ledger(ts, ["2026-10-14", "2026-10-15"], get_settings(), 150_000, 3, book=True, risk_pct=4)
        replay(ts, 150_000)
        outs.append((repr(ts), repr(rows)))
    assert outs[0] == outs[1] == outs[2]
    assert any(e["Side"] == "BOOK" for t in ts for e in t["Executions"])
    assert sum(len(t["Executions"]) > 2 and t["Lots"] > 1 for t in ts)  # staged legs actually split


def test_lot_size_transition_boundaries():
    table = bl.PRE_TABLE_LOTS + LOTS
    for before, on, old, new in (("2021-06-24", "2021-06-25", 75, 50), ("2024-04-25", "2024-04-26", 50, 25),
                                 ("2024-11-19", "2024-11-20", 25, 75), ("2025-12-31", "2026-01-01", 75, 65)):  # fmt: skip
        assert (contract_lot_size(table, "NIFTY", before), contract_lot_size(table, "NIFTY", on)) == (old, new)
    assert contract_lot_size(table, "NIFTY", "2015-11-02") == 75
    with pytest.raises(ValueError):
        contract_lot_size(table, "NIFTY", "2015-10-30")


# 14. Synthetic closures exercise rollback around built-in irregular sessions


@pytest.mark.parametrize(("day", "policy", "expiry"), [
    ("2025-10-15", "same_day", "2025-10-20"), ("2025-10-17", "same_day", "2025-10-20"),
    ("2025-10-20", "same_day", "2025-10-20"), ("2025-10-20", "next", "2025-10-28"),
    ("2025-10-21", "same_day", "2025-10-28"), ("2025-10-23", "same_day", "2025-10-28"),
    ("2021-11-01", "same_day", "2021-11-03"), ("2021-11-03", "same_day", "2021-11-03"),
    ("2021-11-03", "next", "2021-11-11"), ("2021-11-04", "same_day", "2021-11-11"),
])  # fmt: skip
def test_expiry_rolls_back_under_synthetic_closures(day, policy, expiry):
    assert resolve_expiry(date.fromisoformat(day), holidays=HOLIDAYS, policy=policy).isoformat() == expiry


def test_liquidity_backtest_skips_the_muhurat_session_and_uses_the_listed_expiry(monkeypatch):
    days = ("2025-10-16", "2025-10-17", "2025-10-20", "2025-10-21", "2025-10-23")
    bars = [{"date": f"{d} {t}", "open": 25000, "high": 25010, "low": 24990, "close": 25000}
            for d in days for t in ("10:00", "10:05", "10:10")]  # fmt: skip
    monkeypatch.setattr(bl, "scan", lambda bars_, *a, **k: [{"side": "Long", "entry": 25000.0, "risk": 30.0,
                        "stop": 24970.0, "target": 25090.0, "time": bars_[0]["date"], "why": ["test"]}])  # fmt: skip
    trades, _ = bl.backtest(get_settings(), bars, {}, [{"date": d, "high": 1, "low": 0} for d in days],
                            {d: 12.0 for d in days}, include_retest=False, include_trendline_reversal=False)  # fmt: skip
    assert [(t["Date"], t["Expiry"]) for t in trades] == [("2025-10-17", "2025-10-20"), ("2025-10-23", "2025-10-28")]


# 15. The expiry-day policy is part of every causal run's metadata and fingerprint


def test_expiry_day_policy_is_recorded_and_fingerprinted():
    spec, day = _make_test_strategy_spec("integrity"), "2025-11-10"
    reps = {p: wf.run_walk_forward_validation(spec, trading_dates=[day], candles=_make_session_candles(day),
                                               signal_generator=wf.RuleBasedCausalSignalGenerator(expiry_day_policy=p), dataset_declaration=declaration([day]),)
            for p in ("same_day", "next")}  # fmt: skip
    assert {p: r.data_fidelity_and_provenance["expiry_day_policy"] for p, r in reps.items()} == {
        "same_day": "same_day", "next": "next"}
    assert reps["same_day"].report_hash != reps["next"].report_hash
    assert reps["same_day"].config == reps["next"].config  # the policy alone tells the runs apart
    again = wf.run_walk_forward_validation(spec, trading_dates=[day], candles=_make_session_candles(day),
                                           signal_generator=wf.RuleBasedCausalSignalGenerator(), dataset_declaration=declaration([day]),)
    assert again.report_hash == reps["same_day"].report_hash  # default unchanged and reproducible


# 11-12. Frozen PDC V0 and scanner/Telegram configuration untouched


def _old_expiry_for(day, trading, off):
    """expiry_for() as it was before the shared resolver: the PDC V0 benchmark depends on it."""
    if day < date(2019, 2, 11):
        exp = day
        for _ in range(2):
            nxt = date(exp.year + exp.month // 12, exp.month % 12 + 1, 1)
            exp = nxt - timedelta(days=(nxt.weekday() - 3) % 7 or 7)
            if exp >= day:
                break
            exp = nxt
    else:
        thu = day + timedelta(days=(3 - day.weekday()) % 7)
        exp = thu if thu < date(2025, 9, 1) else day + timedelta(days=(1 - day.weekday()) % 7)
    last = max(trading)
    while exp > day and (exp.weekday() >= 5 or exp.isoformat() in off
                         or exp.isoformat() <= last and exp.isoformat() not in trading):  # fmt: skip
        exp -= timedelta(days=1)
    return exp


def test_pdc_v0_expiry_calendar_is_unchanged():
    days = [date(2015, 1, 1) + timedelta(days=i) for i in range(4400)]
    trading = {d.isoformat() for d in days if d.weekday() < 5 and d.isoformat() not in HOLIDAYS and d.day != 13}
    settings = get_settings()
    for d in days:
        assert bl.expiry_for(d, trading, settings) == _old_expiry_for(d, trading, HOLIDAYS), d


def test_frozen_rule_and_scanner_sources_are_untouched():
    from tests.test_research_runtime import FROZEN_SOURCES

    for rel in ("app/liquidity_signals.py", "app/trading/scanner_crew/detector_agent.py",
                "app/trading/crew/exit_agent.py", "scripts/backtest_pdc_15ema.py"):  # fmt: skip
        assert hashlib.sha256((BACKEND / rel).read_bytes()).hexdigest() == FROZEN_SOURCES[rel], rel
    import scripts.backtest_pdc_15ema as pdc

    assert pdc.expiry_for is bl.expiry_for and (pdc.WEEKLY_FROM, pdc.TUESDAY_FROM) == (bl.WEEKLY_FROM, bl.TUESDAY_FROM)
