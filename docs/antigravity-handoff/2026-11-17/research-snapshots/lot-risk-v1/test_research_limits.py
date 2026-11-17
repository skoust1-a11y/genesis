from decimal import Decimal as D

import pytest
from app.trading import paper_risk as risk
from research_limits import install, projection


def values(**overrides):
    return (
        {
            "full_premium_loss": D(4000),
            "estimated_stop_risk": D(2000),
            "stop_budget": D(2500),
            "gross_daily_loss": D(0),
            "existing_reservations": D(0),
            "available_cash": D(50000),
        }
        | overrides
    )


def test_budget_is_shared_and_actual_stop_risk_still_enforced():
    assert projection(**values()) is None
    assert projection(**values(estimated_stop_risk=D(2500))) is None
    assert projection(**values(estimated_stop_risk=D("2500.01"))) == "STOP_RISK_LIMIT"
    assert (
        projection(**values(stop_budget=D(3000), estimated_stop_risk=D(2800))) is None
    )


def test_premium_and_daily_exposure_are_not_relaxed():
    assert projection(**values(full_premium_loss=D(5001))) == "FULL_PREMIUM_LIMIT"
    assert projection(**values(gross_daily_loss=D(1001))) == "DAILY_EXPOSURE_LIMIT"
    assert projection(**values(available_cash=D(3999))) == "INSUFFICIENT_CAPITAL"


@pytest.mark.parametrize("value", [D(3001), D(-1), D("NaN")])
def test_unsupported_budgets_fail(value):
    with pytest.raises(ValueError):
        projection(**values(stop_budget=value))


def test_install_is_explicit_and_test_process_restores_defaults(monkeypatch):
    assert risk.MAX_LOTS == 3
    monkeypatch.setattr(risk, "MAX_LOTS", risk.MAX_LOTS)
    monkeypatch.setattr(risk, "exposure_rejection", risk.exposure_rejection)
    install()
    assert risk.MAX_LOTS == 4 and risk.exposure_rejection is projection
