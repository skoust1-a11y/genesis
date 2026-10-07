from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agents.trade_crew.legs import build_legs, entry_economics, tradingsymbol
from app.extract.extractor import FIXTURE_MARKDOWN
from app.strategies.rules import (
    AccountState,
    PositionState,
    Proposal,
    RiskLimits,
    StrategyRules,
    check_entry,
    check_exit,
    check_risk,
    parse_rules,
    rules_sha256,
)
from app.strategies.schema import parse_strategy_markdown
from tests.conftest import APPROVABLE_MARKDOWN

RAW = parse_strategy_markdown(APPROVABLE_MARKDOWN).frontmatter["rules"]


def rules(**changes) -> StrategyRules:
    raw = copy.deepcopy(RAW)
    for path, value in changes.items():
        section, key = path.split("__")
        raw[section][key] = value
    return StrategyRules.model_validate(raw)


def errors_for(raw) -> list[str]:
    return parse_rules({"rules": raw})[1]


def market(**values):
    return SimpleNamespace(**({"iv_rank": 50, "dte": 14, "spot": 22000} | values))


# Parsing


def test_approvable_rules_parse():
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    assert doc.rules_errors == []
    assert doc.rules.instrument.structure == "iron_condor"
    assert doc.rules.exit_value("profit_target_pct_of_credit") == 50


def test_fixture_rules_parse_and_short_distance_is_required():
    assert parse_strategy_markdown(FIXTURE_MARKDOWN).rules_errors == []
    doc = parse_strategy_markdown(FIXTURE_MARKDOWN.replace("    short_distance: 300\n", ""))
    assert doc.rules is None
    assert doc.rules_errors == ["rules.instrument: short_distance is required for iron_condor"]


def test_missing_block_is_an_error():
    assert parse_rules({}) == (None, ["rules: block missing"])


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r["entry"].append({"field": "vix", "op": ">=", "value": 15}), "rules.entry.3.field"),
        (lambda r: r["entry"].append({"field": "dte", "op": "!=", "value": 15}), "rules.entry.3.op"),
        (lambda r: r["exit"].append({"kind": "trailing_stop", "value": 15}), "rules.exit.2.kind"),
        (lambda r: r["risk"].update(leverage=3), "rules.risk.leverage"),
        (lambda r: r.update(notes="free text"), "rules.notes"),
        (lambda r: r["instrument"].update(structure="calendar"), "rules.instrument.structure"),
        (lambda r: r["instrument"].pop("wing_width"), "wing_width is required for iron_condor"),
        (lambda r: r.update(exit=[r["exit"][0]]), "exit needs stop_loss_pct_of_credit"),
        (lambda r: r.update(entry=[]), "rules.entry"),
        (lambda r: r["risk"].update(max_lots=0), "rules.risk.max_lots"),
    ],
)
def test_invalid_rules_are_rejected_not_skipped(mutate, expected):
    raw = copy.deepcopy(RAW)
    mutate(raw)
    errors = errors_for(raw)
    assert any(expected in e for e in errors), errors


def test_rules_hash_ignores_key_order_but_not_values():
    reordered = {k: RAW[k] for k in reversed(list(RAW))}
    assert rules_sha256(reordered) == rules_sha256(RAW)
    changed = copy.deepcopy(RAW)
    changed["risk"]["max_lots"] = 3
    assert rules_sha256(changed) != rules_sha256(RAW)


# Checks


def test_entry_checks_pass_and_fail_per_condition():
    passing = check_entry(rules(), market())
    assert [c.passed for c in passing] == [True, True, True]
    assert (passing[0].field, passing[0].op, passing[0].threshold, passing[0].actual) == ("iv_rank", ">=", 40, 50)

    failing = check_entry(rules(), market(iv_rank=30, dte=25))
    assert [c.passed for c in failing] == [False, True, False]


def test_entry_check_fails_when_value_missing():
    assert not check_entry(rules(), SimpleNamespace(dte=14, spot=22000))[0].passed


@pytest.mark.parametrize(
    ("pnl", "dte", "fired"),
    [
        (600, 14, ["exit.profit_target"]),
        (499, 14, []),
        (-1000, 14, ["exit.stop_loss"]),
        (-999, 14, []),
    ],
)
def test_exit_checks(pnl, dte, fired):
    checks = check_exit(rules(), PositionState(credit=1000, pnl=pnl), market(dte=dte))
    assert [c.rule for c in checks if c.passed] == fired


def test_dte_exit_rule():
    raw = copy.deepcopy(RAW)
    raw["exit"].append({"kind": "dte_at_or_below", "value": 2})
    r = StrategyRules.model_validate(raw)
    checks = check_exit(r, PositionState(credit=1000, pnl=0), market(dte=2))
    assert [c.rule for c in checks if c.passed] == ["exit.dte"]


def run_risk(r=None, limits=None, account=None, proposal=None):
    checks = check_risk(
        r or rules(),
        limits or RiskLimits(capital_inr=500_000, portfolio_max_open_positions=3, portfolio_max_daily_loss_inr=10_000),
        account or AccountState(open_positions=0, pnl_today=0, portfolio_open_positions=0, portfolio_pnl_today=0),
        proposal or Proposal(lots=2, risk_reward=0.5, max_loss=3000),
    )
    return {c.rule: c.passed for c in checks}


def test_risk_checks_all_pass():
    assert run_risk() == {
        "risk.max_lots": True,
        "risk.max_open_positions": True,
        "risk.max_daily_loss_pct": True,
        "risk.min_risk_reward": True,
        "portfolio.max_open_positions": True,
        "portfolio.max_daily_loss_inr": True,
    }


@pytest.mark.parametrize(
    ("kwargs", "failed"),
    [
        ({"proposal": Proposal(lots=3, risk_reward=0.5, max_loss=3000)}, "risk.max_lots"),
        ({"limits": RiskLimits(capital_inr=500_000, max_lots=1)}, "risk.max_lots"),
        ({"proposal": Proposal(lots=2, risk_reward=0.3, max_loss=3000)}, "risk.min_risk_reward"),
        ({"account": AccountState(1, 0, 1, 0)}, "risk.max_open_positions"),
        ({"account": AccountState(0, -10_000, 0, 0)}, "risk.max_daily_loss_pct"),
        ({"limits": RiskLimits(capital_inr=500_000, max_daily_loss_pct=0.5), "account": AccountState(0, -3000, 0, 0)},
         "risk.max_daily_loss_pct"),
        ({"account": AccountState(0, 0, 3, 0)}, "portfolio.max_open_positions"),
        ({"account": AccountState(0, 0, 0, -10_000)}, "portfolio.max_daily_loss_inr"),
    ],
)  # fmt: skip
def test_each_risk_rule_can_fail(kwargs, failed):
    if "limits" in kwargs:
        kwargs["limits"].portfolio_max_open_positions = 3
        kwargs["limits"].portfolio_max_daily_loss_inr = 10_000
    results = run_risk(**kwargs)
    assert results[failed] is False
    assert [rule for rule, ok in results.items() if not ok] == [failed]


# Legs


def test_iron_condor_legs_snap_to_strikes():
    legs = build_legs(rules().instrument, spot=22013, strike_step=50)
    assert [(leg.side, leg.option_type, leg.strike) for leg in legs] == [
        ("SELL", "CE", 23000),
        ("SELL", "PE", 21000),
        ("BUY", "CE", 23200),
        ("BUY", "PE", 20800),
    ]


def test_straddle_and_strangle_legs():
    straddle_instrument = {"underlying": "NIFTY", "structure": "short_straddle"}
    straddle = StrategyRules.model_validate(RAW | {"instrument": straddle_instrument})
    assert [leg.strike for leg in build_legs(straddle.instrument, 22013, 50)] == [22000, 22000]
    strangle = rules(instrument__structure="short_strangle")
    assert len(build_legs(strangle.instrument, 22013, 50)) == 2


def test_condor_economics_cap_loss_at_wing():
    r = rules()
    legs = build_legs(r.instrument, spot=22000, strike_step=50)
    econ = entry_economics(r, legs, [100, 90, 25, 15], quantity=150)
    assert econ.credit == 150 * 150
    assert econ.target_pnl == 75 * 150
    assert econ.stop_loss_pnl == -150 * 150
    assert econ.max_loss == 50 * 150  # wing 200 minus credit 150, below the 100% stop
    assert econ.risk_reward == 1.5

    wide = entry_economics(r, legs, [40, 35, 10, 5], quantity=150)
    assert wide.max_loss == 60 * 150  # the stop binds before the wing
    assert wide.risk_reward == 0.5


def test_tradingsymbol():
    assert tradingsymbol("NIFTY", "2026-01-29", 22500.0, "CE") == "NIFTY26JAN2922500CE"
