"""Independent strategy acceptance on explicitly synthetic, deterministic data."""
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from app.strategies.configuration import create_configuration, run_configuration
from app.strategies.research import run_development_research
from app.trading.regime import classify_research_regime
from tests.candle_integrity_fixtures import declaration, synthetic_calendar
from tests.test_causal_walk_forward import (
    _make_session_candles,
    _trading_calendar,
    _wf_cfg,
)
from tests.test_strategy_configuration import configuration

pytestmark = pytest.mark.usefixtures("synthetic_calendar")
FIXTURES = Path(__file__).parent / "fixtures" / "independent_strategies"


def onboard(rules, name="acceptance"):
    markdown = "---\n" + yaml.safe_dump({"name": name, "rules_v2": rules}) + "---\n"
    return create_configuration(markdown, original_source=markdown, source_type="markdown", source_ref=None,
                                strategy_id=name, strategy_version="v1", execution_mode="historical_synthetic")


@pytest.mark.parametrize("section", ["stop_loss", "risk", "position_sizing", "data_dependencies"])
def test_missing_explicit_rules_are_quarantined(section):
    rules = configuration().to_dict()["extracted_rules"]["rules_v2"]
    del rules[section]
    assert onboard(rules).to_dict()["execution_status"] == "NOT_EXECUTABLE_EXACTLY"


@pytest.mark.parametrize("mutation", [
    lambda r: r["timeframes"].update(structure="15m"),
    lambda r: r["data_dependencies"].append("unimplemented_feed"),
    lambda r: r["risk"].update(max_entries_per_day=2),
    lambda r: r["position_sizing"].update(lot_step_mode="risk_budget_lots"),
    lambda r: r["indicators"].append({"name": "atr", "period": 14, "timeframe": "5m"}),
    lambda r: r["intraday_entry"].append(copy.deepcopy(r["intraday_entry"][0])),
])
def test_unsupported_and_ambiguous_rules_fail_before_data(mutation):
    rules = configuration().to_dict()["extracted_rules"]["rules_v2"]
    mutation(rules)
    snapshot = onboard(rules)
    assert snapshot.to_dict()["execution_status"] == "NOT_EXECUTABLE_EXACTLY"
    with pytest.raises(ValueError, match="NOT_EXECUTABLE_EXACTLY"):
        run_configuration(snapshot, candles=[], trading_dates=[], config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"))


def test_research_regimes_ignore_future_candles_and_cover_requested_segments():
    now = datetime.fromisoformat("2026-11-06T12:00:00+05:30")
    bars = _make_session_candles("2026-11-06")
    prefix = [b for b in bars if b["timestamp"] < "2026-11-06T12:00"]
    altered = copy.deepcopy(bars)
    for row in altered[len(prefix):]:
        row.update(open=90000, high=90001, low=89999, close=90000)
    assert classify_research_regime(bars, now=now) == classify_research_regime(altered, now=now)
    structures, vols = set(), set()
    for delta_pattern, span in [([8]*14, 2), ([-8]*14, 2), ([8,-8]*7, 2), ([8]*10+[-8]*4, 40)]:
        rows = _make_session_candles("2026-11-06")[:15]
        price = 24000
        for i, row in enumerate(rows):
            delta = delta_pattern[i-1] if i else 0
            row.update(open=price, close=price+delta, low=min(price,price+delta)-span, high=max(price,price+delta)+span)
            price += delta
        label = classify_research_regime(rows, now=now)
        structures.add(label["structure"]); vols.add(label["volatility"])
        assert not label["trading_authority"]
    assert structures == {"BULLISH_TREND", "BEARISH_TREND", "SIDEWAYS", "TRANSITIONAL"}
    assert {"LOW_VOL", "HIGH_VOL"} <= vols


def test_two_strategy_vertical_slice_isolated_reproducible_and_costed(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network side effect"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network side effect"))
    days = _trading_calendar(90)
    rows = [r for i, day in enumerate(days) for r in _make_session_candles(day, pattern=("trending_up", "trending_down", "range")[i % 3])]
    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC", min_oos_trades=30, min_oos_folds=3)
    reports = {}
    snapshots = {}
    for path in sorted(FIXTURES.glob("*.md")):
        source = path.read_text()
        snapshot = create_configuration(source, original_source=source, source_type="markdown", source_ref=str(path.name),
                                        strategy_id=path.stem, strategy_version="v1", execution_mode="historical_synthetic")
        assert snapshot.to_dict()["execution_status"] == "SUPPORTED_STRUCTURED_RULES"
        args = dict(candles=rows, trading_dates=days, holdout_start=days[-5], config=cfg,
                    dataset_declaration=declaration(days, scope="full"))
        report = run_development_research((snapshot,), **args)
        repeat = run_development_research((snapshot,), **args)
        assert report == repeat
        result = report["results"][0]["report"]
        assert result["has_valid_oos_folds"]
        metrics = result["oos_performance"]
        assert metrics["trade_count"] > 0
        assert metrics["total_costs_inr"] > 0
        assert metrics["net_pnl_inr"] == pytest.approx(metrics["gross_pnl_inr"] - metrics["total_costs_inr"], abs=.02)
        assert result["research_segmentation"]["timeframe"]["5m"]["trade_count"] == metrics["trade_count"]
        assert not report["paper_approved"] and not result["paper_promotion_eligible"]
        snapshots[path.stem] = snapshot.to_dict()["extracted_rules"]["rules_v2"]
        reports[path.stem] = report
    assert len(reports) == 2
    assert snapshots["ema-cross"]["intraday_entry"] != snapshots["body-momentum"]["intraday_entry"]
    assert snapshots["ema-cross"]["target"] != snapshots["body-momentum"]["target"]
    assert "ema_period" not in snapshots["body-momentum"]["parameters"]
    assert reports["ema-cross"]["results"][0]["configuration_sha256"] != reports["body-momentum"]["results"][0]["configuration_sha256"]
    # Optional deterministic deliverable, confined to the caller's explicit output path.
    import os
    if output := os.environ.get("SPRINT2_REPORT_PATH"):
        Path(output).write_text(json.dumps(reports, indent=2) + "\n")


def test_capital_and_synthetic_iv_must_match_explicit_configuration():
    snapshot = configuration()
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_MISMATCH"):
        run_configuration(snapshot, candles=[], trading_dates=[], config=_wf_cfg(initial_capital_inr=60000))
    rules = snapshot.to_dict()["extracted_rules"]["rules_v2"]
    del rules["parameters"]["iv"]
    with pytest.raises(ValueError, match="MISSING_EXPLICIT_SYNTHETIC_IV"):
        run_configuration(onboard(rules), candles=[], trading_dates=[], config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"))


def test_strategy_daily_loss_limit_is_independent_of_account_exposure_ceiling():
    from decimal import Decimal

    from app.trading.walk_forward import ReplayRiskTracker
    # The gate acts before monetary projections or admission side effects.
    tracker = ReplayRiskTracker(max_daily_loss_pct=1)
    tracker.daily_losses["2026-11-06"] = Decimal("500")
    from app.trading.walk_forward import CandidateSignalEvent
    event = CandidateSignalEvent(event_id="next", market_day="2026-11-06",
        decision_time="2026-11-06T10:00:00+05:30", label_end_time="2026-11-06T10:30:00+05:30",
        underlying="NIFTY", direction="CALL", entry_spot=24000, stop_spot=23975,
        target_spot=24050, exit_spot=24050, strike=24000, option_type="CE", expiry="2026-11-10")
    from dataclasses import fields

    from app.trading.walk_forward import SimulatedOptionExecution
    values = {f.name: None for f in fields(SimulatedOptionExecution)}
    values.update(event_id="next", market_day="2026-11-06", priced=True, entry_executable=True, admitted=False)
    result = tracker.check_and_apply(event, SimulatedOptionExecution(**values))
    assert result.rejection_reason == "STRATEGY_DAILY_LOSS_LIMIT"
    assert not result.admitted and not tracker.open_positions


@pytest.mark.parametrize("field,value", [("tier2_spread_bps_each_way", -1), ("tier2_slippage_ticks", -1),
                                         ("tier2_iv_stress_pct", 1), ("tier2_iv_stress_pct", float("nan"))])
def test_invalid_cost_assumptions_rejected(field, value):
    with pytest.raises(ValueError):
        _wf_cfg(**{field: value})


def test_low_iv_stress_never_improves_net_performance():
    from dataclasses import replace

    from app.trading.walk_forward import CandidateSignalEvent, simulate_option_execution
    event = CandidateSignalEvent(event_id="low-iv", market_day="2026-11-06",
        decision_time="2026-11-06T10:00:00+05:30", label_end_time="2026-11-06T10:30:00+05:30",
        underlying="NIFTY", direction="CALL", entry_spot=24000, stop_spot=23975,
        target_spot=24050, exit_spot=24050, strike=24000, option_type="CE", expiry="2026-11-10",
        entry_iv=.02, exit_iv=.02, dte_days=4)
    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC", tier2_iv_stress_pct=0)
    base = simulate_option_execution(event, config=cfg)
    stressed = simulate_option_execution(event, config=replace(cfg, tier2_iv_stress_pct=.05))
    assert stressed.net_pnl_inr <= base.net_pnl_inr
    assert stressed.total_costs_inr >= base.total_costs_inr > 0


def test_regime_research_hypotheses_keep_distinct_immutable_versions():
    from app.strategies.configuration import create_variant
    baseline = configuration()
    original = baseline.canonical
    bullish = create_variant(baseline, strategy_version="bullish-target-v2", parameters={"target_r_multiple": 3.0})
    sideways = create_variant(baseline, strategy_version="sideways-target-v2", parameters={"target_r_multiple": 1.2})
    assert len({baseline.sha256, bullish.sha256, sideways.sha256}) == 3
    assert baseline.canonical == original
    for candidate in (bullish, sideways):
        payload = candidate.to_dict()
        assert payload["parent_configuration_sha256"] == baseline.sha256
        assert not payload["paper_approved"] and not payload["live_enabled"]


@pytest.mark.parametrize("field", ["direction", "timeframe", "params", "requires_closed_bar"])
def test_predicate_assumptions_must_be_explicit(field):
    rules = configuration().to_dict()["extracted_rules"]["rules_v2"]
    del rules["intraday_entry"][0][field]
    assert onboard(rules).to_dict()["execution_status"] == "NOT_EXECUTABLE_EXACTLY"


@pytest.mark.parametrize("name", ["stop_loss_points", "target_r_multiple"])
def test_conflicting_exit_parameters_cannot_replace_declared_exit(name):
    rules = configuration().to_dict()["extracted_rules"]["rules_v2"]
    rules["parameters"][name]["value"] += 1
    snapshot = onboard(rules).to_dict()
    assert snapshot["execution_status"] == "NOT_EXECUTABLE_EXACTLY"
    assert any(r["code"] == "CONFLICTING_PARAMETER_DECLARATION" for r in snapshot["rejection_reasons"])
