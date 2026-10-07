from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.engine.costs import CostProfile, apply_trade_costs, resolve_cost_profile
from app.engine.market import Bar, load_sample_bars
from app.strategies.schema import StrategyDocument


@dataclass
class Scenario:
    iv_rank_min: float
    dte_min: int
    dte_max: int
    credit: float
    max_loss: float
    lots: int


@dataclass
class TradeResult:
    date: str
    gross: float
    costs: float
    net: float


@dataclass
class SimResult:
    scenario: Scenario
    mode: str
    trades: list[TradeResult]
    gross_pnl: float
    total_costs: float
    net_pnl: float
    win_rate: float
    max_drawdown: float


def build_scenarios(strategy: StrategyDocument, settings: Settings) -> list[Scenario]:
    fm = strategy.frontmatter
    max_lots = int(fm.get("max_lots", settings.default_max_lots))
    combos = [
        Scenario(35, 7, 21, 1200, 2400, max_lots),
        Scenario(40, 7, 14, 1000, 2000, max(1, max_lots - 1)),
        Scenario(45, 10, 21, 1400, 2800, max_lots),
        Scenario(50, 14, 28, 900, 1800, 1),
    ]
    return combos[: settings.max_scenario_combos]


def _entry_signal(bar: Bar, scenario: Scenario) -> bool:
    return bar.iv_rank >= scenario.iv_rank_min and scenario.dte_min <= bar.dte <= scenario.dte_max


def _simulate_window(bars: list[Bar], scenario: Scenario, profile: CostProfile, mode: str) -> SimResult:
    trades: list[TradeResult] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    wins = 0
    for i, bar in enumerate(bars):
        if not _entry_signal(bar, scenario):
            continue
        # Synthetic premium strategy outcome from IV mean reversion proxy
        edge = (bar.iv_rank - scenario.iv_rank_min) / 100
        gross = (scenario.credit - scenario.max_loss * 0.35 * (1 - edge)) * scenario.lots
        if mode == "forward" and i < len(bars) * 0.7:
            continue
        costs = apply_trade_costs(scenario.credit * scenario.lots, orders=4, profile=profile)
        net = gross - costs
        trades.append(TradeResult(date=bar.date, gross=gross, costs=costs, net=net))
        if net > 0:
            wins += 1
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_pnl = sum(t.gross for t in trades)
    total_costs = sum(t.costs for t in trades)
    net_pnl = sum(t.net for t in trades)
    win_rate = (wins / len(trades)) if trades else 0.0
    return SimResult(
        scenario=scenario,
        mode=mode,
        trades=trades,
        gross_pnl=gross_pnl,
        total_costs=total_costs,
        net_pnl=net_pnl,
        win_rate=win_rate,
        max_drawdown=max_dd,
    )


def run_backtest(strategy: StrategyDocument, scenario: Scenario) -> SimResult:
    settings = get_settings()
    profile = resolve_cost_profile(settings, strategy)
    bars = load_sample_bars()
    split = int(len(bars) * 0.7)
    return _simulate_window(bars[:split], scenario, profile, mode="backtest")


def run_forward(strategy: StrategyDocument, scenario: Scenario) -> SimResult:
    settings = get_settings()
    profile = resolve_cost_profile(settings, strategy)
    bars = load_sample_bars()
    split = int(len(bars) * 0.7)
    return _simulate_window(bars[split:], scenario, profile, mode="forward")
