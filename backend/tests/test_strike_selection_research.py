"""Tests for Strike Selection Research (Prompt 04)."""

from datetime import datetime
from decimal import Decimal

from app.trading import paper_fees, paper_risk
from app.trading.option_quote import IST, ContractQuote
from app.trading.strike_selection_research import (
    StrikeSelector,
    tier_from_step,
)

# Register test fee policy
TEST_FEE_POLICY = "test-fee-policy"
paper_fees.POLICIES_BY_ID[TEST_FEE_POLICY] = paper_fees.FeePolicySpec(
    TEST_FEE_POLICY,
    "2026-01-01",
    "2026-12-31",
    dict(paper_fees.RATES),
    (),
    None,
)


def mock_quote_fetcher(chain, strike, option_type, now):
    """Generates synthetic quote based on distance from ATM (25300)."""
    atm = 25300.0
    dist = (strike - atm) if option_type == "CE" else (atm - strike)
    # Shallow ITM / ATM / OTM premium between 15 and 40 INR
    mid = Decimal(str(round(max(10.0, 30.0 - dist * 0.1), 2)))
    half_spread = Decimal("0.50")
    bid = max(Decimal("0.05"), mid - half_spread)
    ask = mid + half_spread
    q = ContractQuote(
        underlying="NIFTY",
        expiry="2026-11-19",
        strike=strike,
        option_type=option_type,
        provider_instrument_id=f"NSE_INDEX|NIFTY26NOV{int(strike)}{option_type}",
        tradingsymbol=f"NIFTY26NOV{int(strike)}{option_type}",
        lot_size=65,
        ltp=mid,
        bid=bid,
        ask=ask,
        quote_time=now,
        fetched_at=now,
        source="upstox",
        delta=0.50,
    )
    return q, None


def test_tier_from_step():
    assert tier_from_step(-2) == "ITM"
    assert tier_from_step(-1) == "ITM"
    assert tier_from_step(0) == "ATM"
    assert tier_from_step(1) == "OTM"
    assert tier_from_step(2) == "OTM"


def test_time_rotation_on_expiry():
    """On expiry day, time rotation selects ITM morning, ATM midday, OTM afternoon."""
    selector = StrikeSelector(
        stop_budget_ceiling=Decimal("2500.00"),
        max_lots_atm=2,
        max_lots_itm=2,
        max_lots_otm=4,
    )
    chain = {"contracts": []}

    # 1. Morning (09:45 IST) on expiry day -> ITM (step -1)
    now_morning = datetime(2026, 11, 19, 9, 45, tzinfo=IST)
    def make_evidence(clock):
        return {
            f"NIFTY26NOV{s}CE": paper_risk.DeltaEvidence(
                value=Decimal("0.55" if s < 25300 else "0.50" if s == 25300 else "0.35"),
                observed_at=clock,
                contract=f"NIFTY26NOV{s}CE",
                source="upstox",
            )
            for s in (25200, 25250, 25300, 25350, 25400)
        }

    res_morning = selector.select(
        policy="TIME_ROTATION",
        entry_spot=25300.0,
        stop_spot=25280.0,
        direction="CALL",
        now=now_morning,
        chain=chain,
        is_expiry_day=True,
        cash=Decimal("50000.00"),
        fee_policy_id=TEST_FEE_POLICY,
        quote_fetcher=mock_quote_fetcher,
        evidence_map=make_evidence(now_morning),
    )
    assert res_morning.chosen_tier == "ITM"
    assert res_morning.chosen_step == -1

    # 2. Midday (12:00 IST) on expiry day -> ATM (step 0)
    now_midday = datetime(2026, 11, 19, 12, 0, tzinfo=IST)
    res_midday = selector.select(
        policy="TIME_ROTATION",
        entry_spot=25300.0,
        stop_spot=25280.0,
        direction="CALL",
        now=now_midday,
        chain=chain,
        is_expiry_day=True,
        cash=Decimal("50000.00"),
        fee_policy_id=TEST_FEE_POLICY,
        quote_fetcher=mock_quote_fetcher,
        evidence_map=make_evidence(now_midday),
    )
    assert res_midday.chosen_tier == "ATM"
    assert res_midday.chosen_step == 0

    # 3. Afternoon (14:00 IST) on expiry day -> OTM (step 2)
    now_afternoon = datetime(2026, 11, 19, 14, 0, tzinfo=IST)
    res_afternoon = selector.select(
        policy="TIME_ROTATION",
        entry_spot=25300.0,
        stop_spot=25280.0,
        direction="CALL",
        now=now_afternoon,
        chain=chain,
        is_expiry_day=True,
        cash=Decimal("50000.00"),
        fee_policy_id=TEST_FEE_POLICY,
        quote_fetcher=mock_quote_fetcher,
        evidence_map=make_evidence(now_afternoon),
    )
    assert res_afternoon.chosen_tier == "OTM"
    assert res_afternoon.chosen_step == 2


def test_no_forced_minimum_lot_affordability():
    """If 1 lot exceeds full premium limit (5000), must REJECT, not force 1 lot."""
    selector = StrikeSelector(
        stop_budget_ceiling=Decimal("1000.00"),
        full_premium_limit=Decimal("5000.00"),
    )
    now = datetime(2026, 11, 17, 10, 0, tzinfo=IST)

    # Expensive ITM option: ask = 120.0, 1 lot = 65 * 120 = 7800 > 5000 cap
    def expensive_quote_fetcher(chain, strike, option_type, now):
        q = ContractQuote(
            underlying="NIFTY",
            expiry="2026-11-19",
            strike=strike,
            option_type=option_type,
            provider_instrument_id=f"NSE_INDEX|NIFTY26NOV{int(strike)}{option_type}",
            tradingsymbol=f"NIFTY26NOV{int(strike)}{option_type}",
            lot_size=65,
            ltp=Decimal("120.00"),
            bid=Decimal("119.00"),
            ask=Decimal("120.00"),
            quote_time=now,
            fetched_at=now,
            source="upstox",
            delta=0.70,
        )
        return q, None

    sym = "NIFTY26NOV25250CE"
    evidence_map = {
        sym: paper_risk.DeltaEvidence(
            value=Decimal("0.70"),
            observed_at=now,
            contract=sym,
            source="upstox",
        )
    }

    res = selector.select(
        policy="FIXED_ITM",
        entry_spot=25300.0,
        stop_spot=25280.0,
        direction="CALL",
        now=now,
        chain={},
        is_expiry_day=False,
        cash=Decimal("50000.00"),
        fee_policy_id=TEST_FEE_POLICY,
        quote_fetcher=expensive_quote_fetcher,
        evidence_map=evidence_map,
    )
    # Must be rejected because 7800 > 5000 full premium limit
    assert res.chosen_strike is None
    assert res.rejection_reason is not None
    assert any("FULL_PREMIUM_LIMIT" in str(att.rejection_reason) for att in res.attempts)


def test_ranked_fallback_when_preferred_tier_rejected():
    """If ITM exceeds premium limit, ranked fallback successfully selects ATM or OTM."""
    selector = StrikeSelector(
        stop_budget_ceiling=Decimal("1000.00"),
        full_premium_limit=Decimal("5000.00"),
    )
    now = datetime(2026, 11, 17, 10, 0, tzinfo=IST)

    # ITM (25250) is expensive (>5000), ATM (25300) and OTM (25400) are affordable (<5000)
    def tiered_quote_fetcher(chain, strike, option_type, now):
        mid = Decimal("100.00" if strike < 25300 else "30.00" if strike == 25300 else "15.00")
        q = ContractQuote(
            underlying="NIFTY",
            expiry="2026-11-19",
            strike=strike,
            option_type=option_type,
            provider_instrument_id=f"NSE_INDEX|NIFTY26NOV{int(strike)}{option_type}",
            tradingsymbol=f"NIFTY26NOV{int(strike)}{option_type}",
            lot_size=65,
            ltp=mid,
            bid=mid - Decimal("0.50"),
            ask=mid + Decimal("0.50"),
            quote_time=now,
            fetched_at=now,
            source="upstox",
            delta=0.50,
        )
        return q, None

    evidence_map = {
        f"NIFTY26NOV{s}CE": paper_risk.DeltaEvidence(
            value=Decimal("0.50"),
            observed_at=now,
            contract=f"NIFTY26NOV{s}CE",
            source="upstox",
        )
        for s in (25200, 25250, 25300, 25350, 25400)
    }

    res = selector.select(
        policy="RANKED_FALLBACK",
        entry_spot=25300.0,
        stop_spot=25280.0,
        direction="CALL",
        now=now,
        chain={},
        is_expiry_day=False,
        cash=Decimal("50000.00"),
        fee_policy_id=TEST_FEE_POLICY,
        quote_fetcher=tiered_quote_fetcher,
        evidence_map=evidence_map,
    )
    # Preferred ITM was rejected due to premium cap; ATM was selected as fallback!
    assert res.chosen_tier == "ATM"
    assert res.chosen_step == 0
    assert res.chosen_strike == 25300.0

