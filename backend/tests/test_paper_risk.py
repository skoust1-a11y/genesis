from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from app.trading.paper_risk import DeltaEvidence, StopRiskUnverifiable, exposure_rejection, stop_risk

NOW = datetime.fromisoformat("2026-11-06T10:19:00+05:30")
EVIDENCE = DeltaEvidence(D("0.5"), NOW, "synthetic-option", "synthetic-replay")


def test_shipped_registry_uses_v2_limits():
    from app.trading import paper_registry as registry, paper_risk as risk
    assert registry.RISK_POLICY["risk_policy_id"] == risk.POLICY_ID
    assert D(registry.RISK_POLICY["max_loss_per_trade_inr"]) == risk.FULL_PREMIUM_LIMIT
    assert D(registry.RISK_POLICY["daily_loss_limit_inr"]) == risk.DAILY_EXPOSURE_LIMIT
    assert registry.RISK_POLICY["max_lots"] == risk.MAX_LOTS
    from tests.test_research_runtime import FROZEN_SOURCES
    assert registry.RULE_SOURCE_HASHES == FROZEN_SOURCES


def estimate(**overrides):
    args = dict(evidence=EVIDENCE, trusted_sources=frozenset({"synthetic-replay"}), contract="synthetic-option",
                option_type="CE", now=NOW, underlying_entry=D("24000"), underlying_stop=D("23980"),
                quantity=50, entry_price=D("40"), liquidation_bid=D("39.50"), entry_charges=D("25"),
                conservative_exit_charges=D("28"), full_premium_loss=D("2048.60"))
    return stop_risk(**(args | overrides))


def test_hand_calculated_call_and_put_stop_estimates_are_not_fills():
    assert estimate() == D("578.00")  # 50 * (.5 * 20 + .5) + 25 + 28
    assert estimate(option_type="PE", underlying_stop=D("24020"),
                    evidence=replace(EVIDENCE, value=D("-0.5"))) == D("578.00")
    assert estimate(underlying_stop=D("23000")) == D("2048.60")
    assert estimate(evidence=replace(EVIDENCE, value=D("0.500001"))) == D("578.01")


@pytest.mark.parametrize("evidence", [None, replace(EVIDENCE, source="untrusted"),
    replace(EVIDENCE, contract="different-option"), replace(EVIDENCE, value=D("NaN")),
    replace(EVIDENCE, value=D("0")), replace(EVIDENCE, value=D("-0.5")),
    replace(EVIDENCE, value=D("1.01")), replace(EVIDENCE, observed_at=NOW + timedelta(seconds=1)),
    replace(EVIDENCE, observed_at=NOW - timedelta(seconds=11)),
    replace(EVIDENCE, observed_at=NOW.replace(tzinfo=None))])
def test_missing_invalid_stale_or_untrusted_delta_rejected(evidence):
    with pytest.raises(StopRiskUnverifiable):
        estimate(evidence=evidence)


def test_exact_freshness_boundary_and_invalid_stop():
    assert estimate(evidence=replace(EVIDENCE, observed_at=NOW - timedelta(seconds=10))) == D("578.00")
    with pytest.raises(StopRiskUnverifiable):
        estimate(underlying_stop=D("24001"))


def gate(**overrides):
    args = dict(full_premium_loss=D("1500"), estimated_stop_risk=D("500"), stop_budget=D("1000"),
                gross_daily_loss=D("1000"), existing_reservations=D("2500"), available_cash=D("46000"))
    return exposure_rejection(**(args | overrides))


def test_daily_loss_capacity_is_exact_and_profits_cannot_replenish_it():
    assert gate() is None
    assert gate(full_premium_loss=D("1500.01")) == "DAILY_EXPOSURE_LIMIT"
    assert gate(available_cash=D("52000"), full_premium_loss=D("1500.01")) == "DAILY_EXPOSURE_LIMIT"
    assert gate(estimated_stop_risk=D("1000.01")) == "STOP_RISK_LIMIT"
    assert gate(full_premium_loss=D("5000.01")) == "FULL_PREMIUM_LIMIT"
    assert gate(available_cash=D("1499.99")) == "INSUFFICIENT_CAPITAL"
    with pytest.raises(ValueError):
        gate(stop_budget=D("1000.01"))


def sized(**overrides):
    from app.trading.paper_risk import size
    from tests.test_paper_trading import quote, SETTINGS
    book = quote(NOW)
    args = dict(quote=book, evidence=replace(EVIDENCE, contract=book.tradingsymbol),
                trusted_sources=frozenset({"synthetic-replay"}), now=NOW, settings=SETTINGS,
                underlying_entry=D("24000"), underlying_stop=D("23980"), stop_budget=D("1000"),
                gross_daily_loss=D("0"), existing_reservations=D("0"), available_cash=D("50000"))
    return size(**(args | overrides))


def test_lot_selection_uses_executable_book_fees_and_every_monetary_gate():
    choice = sized()
    assert choice.reason is None and choice.entry.lots == 1
    assert choice.entry.price == D("40.45")
    assert choice.full_premium_loss <= D("5000") and choice.estimated_stop_risk <= D("1000")
    assert choice.entry.charges["policy"]["policy_id"].startswith("nse-index-option")
    assert sized(stop_budget=D("100")).reason == "STOP_RISK_LIMIT"
    assert sized(existing_reservations=D("2500")).reason == "DAILY_EXPOSURE_LIMIT"
    assert sized(gross_daily_loss=D("2500")).reason == "DAILY_EXPOSURE_LIMIT"
    assert sized(available_cash=D("2600")).reason == "INSUFFICIENT_CAPITAL"  # one 65-lot costs Rs 2,677.63
    assert sized(evidence=None).reason == "STOP_RISK_UNVERIFIABLE"
    assert sized(trusted_sources=frozenset()).reason == "STOP_RISK_UNVERIFIABLE"
    assert sized(quote=None).entry is None


def test_lot_selection_never_falls_back_to_legacy_fees_for_an_unverified_date():
    from tests.test_paper_trading import quote
    tomorrow = NOW + timedelta(days=1)
    book = quote(tomorrow)
    result = sized(now=tomorrow, quote=book,
                   evidence=replace(EVIDENCE, contract=book.tradingsymbol, observed_at=tomorrow))
    assert result.entry is None and result.reason == "COST_POLICY_UNVERIFIED_DATE"


def test_largest_feasible_lot_count_and_exact_daily_boundary():
    from tests.test_paper_trading import quote
    book = quote(NOW, bid=10, ask=10.4)
    kwargs = dict(quote=book, underlying_stop=D("23995"))
    largest = sized(**kwargs)
    assert largest.entry.lots == 3
    reduced = sized(**kwargs, stop_budget=D("600"))
    assert reduced.entry.lots == 2
    assert sized(**kwargs, existing_reservations=D("5000") - largest.full_premium_loss).entry.lots == 3
    assert sized(**kwargs, existing_reservations=D("5000.01") - largest.full_premium_loss).entry.lots == 2


def test_strategy_lot_ceiling_only_lowers_the_policy_maximum():
    from tests.test_paper_trading import quote
    kwargs = dict(quote=quote(NOW, bid=10, ask=10.4), underlying_stop=D("23995"))
    assert sized(**kwargs).entry.lots == 3
    assert sized(**kwargs, max_lots=1).entry.lots == 1
    assert sized(**kwargs, max_lots=2).entry.lots == 2
    assert sized(**kwargs, max_lots=9).entry.lots == 3
    assert sized(**kwargs, max_lots=1, available_cash=D("700")).reason == "INSUFFICIENT_CAPITAL"
    for bad in (0, -1, True, 1.0, "1", None):
        result = sized(**kwargs, max_lots=bad)
        assert result.entry is None and result.reason == "STRATEGY_LOT_LIMIT_INVALID"


def test_every_shipped_family_spec_declares_one_lot_and_conflicts_fail_closed(monkeypatch):
    from app.strategies import normalizer
    from app.trading import paper_registry as registry
    assert {f: registry.strategy_max_lots(f) for f in registry.FAMILIES} == dict.fromkeys(registry.FAMILIES, 1)
    real = normalizer.normalize_scanner_v0_family

    def conflicting(family):
        spec = real(family)
        sizing = spec.rules_v2.position_sizing.model_copy(update={"max_lots": 3})
        return replace(spec, rules_v2=spec.rules_v2.model_copy(update={"position_sizing": sizing}))

    monkeypatch.setattr(normalizer, "normalize_scanner_v0_family", conflicting)
    assert registry.strategy_max_lots("RETEST_OR30") is None


def test_daily_exposure_boundary_is_independent_of_decimal_context():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 6
        assert gate(full_premium_loss=D("1500.01")) == "DAILY_EXPOSURE_LIMIT"
        assert gate() is None
