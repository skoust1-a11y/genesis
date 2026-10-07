"""Losing-streak cooldown, entries per day, loss within capital and VIX sizing: rules, simulator and Trade Crew."""

from __future__ import annotations

import pytest

from app.agents.trade_crew.graph import run_tick
from app.config import Settings, get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import Management, Scenario, dte_window, simulate
from app.live.store import LiveStore
from app.market.historical import HistoricalFeed
from app.market.hours import trading_days_between
from app.strategies.rules import (
    AccountState,
    Proposal,
    RiskLimits,
    StrategyRules,
    check_risk,
    losing_streak_cooldown,
    with_common_risk,
)
from app.strategies.schema import parse_strategy_markdown
from app.trading.policy import TradePolicy, VixSizing, vix_size_factor
from tests.conftest import APPROVABLE_MARKDOWN, approve_and_start
from tests.test_simulator import ENTRY as ENTRY_CHAIN
from tests.test_simulator import chain, seed, store  # noqa: F401
from tests.test_trade_crew import CALM, ENTRY, RALLY, Clock, make_ctx  # noqa: F401

BASE_RISK = "risk: {max_lots: 2, max_daily_loss_pct: 2, min_risk_reward: 0.4}"
RAW = parse_strategy_markdown(APPROVABLE_MARKDOWN).frontmatter["rules"]


def markdown(**risk) -> str:
    extra = "".join(f", {k}: {v}" for k, v in risk.items())
    return APPROVABLE_MARKDOWN.replace(BASE_RISK, BASE_RISK[:-1] + extra + "}")


def rules(**risk) -> StrategyRules:
    return StrategyRules.model_validate(RAW | {"risk": RAW["risk"] | risk})


def days_since(dates: list[str]):
    index = {d: i for i, d in enumerate(dates)}
    return lambda today: lambda day: index[today] - index[day]


# Rules


def test_streak_and_cooldown_go_together():
    with pytest.raises(ValueError, match="go together"):
        rules(max_consecutive_losses=2)
    with pytest.raises(ValueError, match="go together"):
        rules(cooldown_days=1)


def test_losing_streak_cooldown_counts_trading_days_and_resets():
    r = rules(max_consecutive_losses=2, cooldown_days=1).risk
    since = days_since(["d1", "d2", "d3", "d4", "d5"])
    two_losses = [("d1", -100.0), ("d2", -50.0)]
    assert losing_streak_cooldown(r, two_losses, since("d2")) == 2  # the last loss's day and the next
    assert losing_streak_cooldown(r, two_losses, since("d3")) == 1
    assert losing_streak_cooldown(r, two_losses, since("d4")) == 0
    assert losing_streak_cooldown(r, [("d1", -100.0), ("d2", 10.0), ("d3", -50.0)], since("d3")) == 0  # a win resets
    assert losing_streak_cooldown(r, [*two_losses, ("d4", -10.0)], since("d4")) == 0  # the streak was used up
    assert losing_streak_cooldown(rules().risk, two_losses, since("d2")) == 0  # no rule, no cooldown


def run_risk(r: StrategyRules, account: AccountState | None = None, max_loss: float = 2000) -> dict[str, bool]:
    checks = check_risk(
        r,
        RiskLimits(capital_inr=50_000),
        account or AccountState(open_positions=0, pnl_today=0, portfolio_open_positions=0, portfolio_pnl_today=0),
        Proposal(lots=1, risk_reward=0.5, max_loss=max_loss),
    )
    return {c.rule: c.passed for c in checks}


def test_new_risk_checks():
    r = rules(max_entries_per_day=2, max_consecutive_losses=3, cooldown_days=1, max_loss_pct_of_capital=5)
    assert all(run_risk(r).values())
    assert run_risk(r, AccountState(0, 0, 0, 0, entries_today=2))["risk.max_entries_per_day"] is False
    assert run_risk(r, AccountState(0, 0, 0, 0, cooldown_days_left=1))["risk.losing_streak_cooldown"] is False
    assert run_risk(r, max_loss=2_500)["risk.max_loss_pct_of_capital"] is True  # 5% of 50,000
    assert run_risk(r, max_loss=2_501)["risk.max_loss_pct_of_capital"] is False
    assert not {"risk.max_entries_per_day", "risk.losing_streak_cooldown"} & set(run_risk(rules()))


def test_reentry_checks():
    r = rules(max_losses_per_day=1, min_minutes_between_trades=30)
    assert all(run_risk(r).values())  # no trade yet: nothing to wait for
    assert run_risk(r, AccountState(0, 0, 0, 0, losses_today=1))["risk.max_losses_per_day"] is False
    assert run_risk(r, AccountState(0, 0, 0, 0, minutes_since_last_trade=29.9))["risk.min_minutes_between_trades"] is False
    assert run_risk(r, AccountState(0, 0, 0, 0, minutes_since_last_trade=30))["risk.min_minutes_between_trades"] is True
    assert not {"risk.max_losses_per_day", "risk.min_minutes_between_trades"} & set(run_risk(rules()))
    common = {"max_losses_per_day": 2, "min_minutes_between_trades": 60}
    merged = with_common_risk(r, common).risk
    assert (merged.max_losses_per_day, merged.min_minutes_between_trades) == (1, 60)
    s = Settings(_env_file=None, MAX_LOSSES_PER_DAY=1, MIN_MINUTES_BETWEEN_TRADES=45)
    assert s.common_risk == {"max_losses_per_day": 1, "min_minutes_between_trades": 45}


def test_common_risk_applies_to_every_strategy_and_the_stricter_value_wins():
    common = {"max_entries_per_day": 1, "max_consecutive_losses": 3, "cooldown_days": 3, "max_loss_pct_of_capital": 100}
    plain = with_common_risk(rules(), common).risk
    assert (plain.max_entries_per_day, plain.max_consecutive_losses, plain.cooldown_days) == (1, 3, 3)
    assert plain.max_loss_pct_of_capital == 100

    own = rules(max_entries_per_day=3, max_consecutive_losses=2, cooldown_days=1, max_loss_pct_of_capital=5)
    merged = with_common_risk(own, common).risk
    assert (merged.max_entries_per_day, merged.max_consecutive_losses, merged.cooldown_days) == (1, 2, 3)
    assert merged.max_loss_pct_of_capital == 5
    assert with_common_risk(own, {}) is own


def test_common_risk_settings():
    s = Settings(_env_file=None, MAX_ENTRIES_PER_DAY=1, MAX_CONSECUTIVE_LOSSES=3, LOSS_COOLDOWN_DAYS=3)
    assert s.common_risk == {"max_entries_per_day": 1, "max_consecutive_losses": 3, "cooldown_days": 3}
    with pytest.raises(ValueError, match="go together"):
        Settings(_env_file=None, MAX_CONSECUTIVE_LOSSES=3)


def test_trading_days_skip_weekends_and_holidays(tmp_path):
    (tmp_path / "holidays").mkdir()
    (tmp_path / "holidays" / "nse.yaml").write_text("holidays:\n  - {date: 2026-01-26, name: Republic Day}\n")
    settings = get_settings().model_copy(update={"data_dir": str(tmp_path)})
    assert trading_days_between("2026-01-09", "2026-01-12", settings) == 1  # Friday to Monday
    assert trading_days_between("2026-01-23", "2026-01-27", settings) == 1  # Republic Day on the 26th
    assert trading_days_between("2026-01-12", "2026-01-12", settings) == 0


# VIX sizing


def test_vix_size_factor_picks_the_highest_band_reached():
    bands = [{"at_or_above": 18, "size_factor": 0.5}, {"at_or_above": 25, "size_factor": 0.25}]
    vs = VixSizing.model_validate({"bands": bands})
    assert [vix_size_factor(vs, v) for v in (12, 18, 24.9, 30)] == [1.0, 0.5, 0.5, 0.25]
    assert vix_size_factor(vs, None) == 0.25  # no reading: the most careful band
    assert vix_size_factor(None, 30) == 1.0


@pytest.mark.parametrize(
    "bands",
    [
        [{"at_or_above": 25, "size_factor": 0.5}, {"at_or_above": 18, "size_factor": 0.25}],
        [{"at_or_above": 18, "size_factor": 0.25}, {"at_or_above": 25, "size_factor": 0.5}],
    ],
)
def test_vix_bands_must_rise_in_vix_and_fall_in_size(bands):
    with pytest.raises(ValueError, match="must"):
        VixSizing.model_validate({"bands": bands})


# Simulator


def simulate_md(store, md: str, management: Management | None = None, settings=None):  # noqa: F811
    settings = settings or get_settings()
    doc = parse_strategy_markdown(md)
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules))
    return simulate(
        doc.rules, feed, len(feed.dates), resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", doc.frontmatter["rules"], lots=2), mode="backtest", management=management,
    )  # fmt: skip


def test_sim_waits_out_a_losing_streak_cooldown(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22900, 10, chain(220, 90, 5, 2)),  # stop loss
            ("2026-01-07", 22000, 60, ENTRY_CHAIN),
            ("2026-01-08", 22000, 60, ENTRY_CHAIN),
        ],
    )
    plain = simulate_md(store, APPROVABLE_MARKDOWN)
    assert [t.date for t in plain.trades] == ["2026-01-05", "2026-01-07"]
    cooled = simulate_md(store, markdown(max_consecutive_losses=1, cooldown_days=1))
    assert [t.date for t in cooled.trades] == ["2026-01-05", "2026-01-08"]
    assert cooled.skipped["risk.losing_streak_cooldown"] == 1

    common = get_settings().model_copy(update={"max_consecutive_losses": 1, "loss_cooldown_days": 1})
    assert [t.date for t in simulate_md(store, APPROVABLE_MARKDOWN, settings=common).trades] == [
        "2026-01-05", "2026-01-08",
    ]  # fmt: skip


def test_sim_vix_sizing_and_snapshot_vix(store):  # noqa: F811
    seed(store, [("2026-01-05", 22000, 60, ENTRY_CHAIN), ("2026-01-06", 22000, 60, ENTRY_CHAIN)])  # VIX 15
    assert HistoricalFeed(store, "NIFTY").snapshot("NIFTY").vix == 15.0

    def lots(at_or_above: float) -> int:
        band = {"at_or_above": at_or_above, "size_factor": 0.5}
        policy = TradePolicy.model_validate({"vix_sizing": {"bands": [band]}})
        [trade] = simulate_md(store, APPROVABLE_MARKDOWN, Management(policy=policy)).trades
        return trade.lots

    assert lots(14) == 1 and lots(20) == 2


# Trade Crew


@pytest.fixture
def approve_md(client):
    def approve(md: str) -> dict:
        ingest = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": md})
        assert ingest.status_code == 200, ingest.text
        strategy_id = ingest.json()["strategy_id"]
        assert client.post("/api/review/run", json={"strategy_id": strategy_id}).status_code == 200
        preview = client.get("/api/live/approvals/preview", params={"strategy_id": strategy_id, "version": 1}).json()
        assert preview["blockers"] == [], preview
        return {"strategy_id": strategy_id, "preview": preview}

    return approve


def test_approval_preview_shows_the_risk_rules_in_force(monkeypatch, approve_md):
    monkeypatch.setenv("MAX_ENTRIES_PER_DAY", "1")
    monkeypatch.setenv("MAX_CONSECUTIVE_LOSSES", "3")
    monkeypatch.setenv("LOSS_COOLDOWN_DAYS", "2")
    get_settings.cache_clear()
    preview = approve_md(markdown(max_entries_per_day=3, max_consecutive_losses=2, cooldown_days=1))["preview"]
    assert preview["common_risk"] == {"max_entries_per_day": 1, "max_consecutive_losses": 3, "cooldown_days": 2}
    in_force = preview["risk_in_force"]
    assert (in_force["max_entries_per_day"], in_force["max_consecutive_losses"], in_force["cooldown_days"]) == (1, 2, 2)
    assert preview["rules"]["risk"]["max_entries_per_day"] == 3  # the strategy's own rules are unchanged


def test_crew_cooldown_after_a_loss(client, live_session, make_ctx, approve_md, paper_account):
    strategy = approve_md(markdown(max_consecutive_losses=1, cooldown_days=1))
    deployment_id = approve_and_start(client, strategy, paper_account)
    clock = Clock()  # Monday 2026-01-05, 09:30 IST
    ctx = make_ctx(deployment_id, [ENTRY, RALLY, ENTRY, ENTRY], clock=clock)
    outcomes = []
    for _ in range(4):
        outcomes.append(run_tick(ctx))
        clock.advance(days=1)
    assert [r["outcome"] for r in outcomes] == ["entered", "exited", "blocked", "entered"]
    assert outcomes[2]["detail"] == "risk.losing_streak_cooldown"
    [trade] = LiveStore(live_session).live_trades(deployment_id)
    assert trade.net < 0


def test_crew_max_entries_per_day(client, live_session, make_ctx, approve_md, paper_account):
    strategy = approve_md(markdown(max_entries_per_day=1))
    deployment_id = approve_and_start(client, strategy, paper_account)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, ENTRY, ENTRY], clock=clock)
    outcomes = []
    for _ in range(3):
        outcomes.append(run_tick(ctx))
        clock.advance(hours=1)
    assert [r["outcome"] for r in outcomes] == ["entered", "exited", "blocked"]
    assert outcomes[2]["detail"] == "risk.max_entries_per_day"
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "entered"


def test_crew_no_reentry_after_a_loss_that_day(client, live_session, make_ctx, approve_md, paper_account):
    strategy = approve_md(markdown(max_losses_per_day=1))
    deployment_id = approve_and_start(client, strategy, paper_account)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, RALLY, ENTRY, ENTRY], clock=clock)
    outcomes = []
    for _ in range(3):
        outcomes.append(run_tick(ctx))
        clock.advance(hours=1)
    assert [r["outcome"] for r in outcomes] == ["entered", "exited", "blocked"]
    assert "risk.max_losses_per_day" in outcomes[2]["detail"].split(", ")  # the loss also trips the daily caps
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "entered"


def test_crew_waits_between_trades(client, live_session, make_ctx, approve_md, paper_account):
    strategy = approve_md(markdown(min_minutes_between_trades=90))
    deployment_id = approve_and_start(client, strategy, paper_account)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, ENTRY, ENTRY], clock=clock)
    outcomes = []
    for _ in range(3):
        outcomes.append(run_tick(ctx))
        clock.advance(hours=1)
    assert [r["outcome"] for r in outcomes] == ["entered", "exited", "blocked"]
    assert outcomes[2]["detail"] == "risk.min_minutes_between_trades"
    assert run_tick(ctx)["outcome"] == "entered"  # two hours after the exit


def test_crew_applies_common_risk_rules(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = approve_and_start(client, approvable, paper_account)
    settings = get_settings().model_copy(update={"max_entries_per_day": 1})
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, ENTRY], settings=settings, clock=clock)
    outcomes = []
    for _ in range(3):
        outcomes.append(run_tick(ctx))
        clock.advance(hours=1)
    assert [r["outcome"] for r in outcomes] == ["entered", "exited", "blocked"]
    assert outcomes[2]["detail"] == "risk.max_entries_per_day"


def test_crew_vix_sizing_cuts_lots(client, live_session, make_ctx, approvable, paper_account):
    trade_policy = {"vix_sizing": {"bands": [{"at_or_above": 18, "size_factor": 0.5}]}}
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits={"policy": trade_policy})
    clock = Clock()
    assert run_tick(make_ctx(deployment_id, [ENTRY], clock=clock))["outcome"] == "entered"
    [intent] = LiveStore(live_session).intents(deployment_id)
    assert {leg["quantity"] for leg in intent.legs} == {75}  # the replay feed has no VIX: the last band applies
    assert "VIX sizing (no VIX reading) cut 2 to 1 lots." in intent.reason_text
