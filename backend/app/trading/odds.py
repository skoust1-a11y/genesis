"""The odds gate: enter only when the strategy's own past trades in today's India VIX band made money.

The history is a rules-only backtest of the same strategy (no trade policy, no drawdown ladder) on the
imported market data. On any day, only trades that closed before that day count, so a backtest never
sees the future and live trading uses only finished trades. Results are per lot, after costs.

The gate passes when the band has at least `min_trades` past trades and their average net per lot
is above `min_expectancy_inr`. With no VIX reading, or too few trades, it fails safe.

Pure functions shared by the Trade Crew and the simulator; `strategy_history` runs the simulator.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from app.strategies.rules import Check

if TYPE_CHECKING:
    from app.config import Settings
    from app.engine.costs import CostProfile
    from app.market.source import MarketSource
    from app.strategies.rules import StrategyRules
    from app.trading.policy import OddsGate

SAMPLE_CHECK = "odds.sample"
EXPECTANCY_CHECK = "odds.expectancy"


@dataclass(frozen=True)
class HistTrade:
    entry_date: str
    exit_date: str
    net_per_lot: float  # INR, after costs
    vix_at_entry: float | None


@dataclass
class Odds:
    band: str | None  # None without a VIX reading
    vix: float | None
    trades: int
    wins: int
    win_rate: float | None  # %
    avg_win: float | None  # INR per lot
    avg_loss: float | None  # INR per lot, as a positive number
    breakeven_win_rate: float | None  # % that would make the average net zero at these win and loss sizes
    expectancy: float | None  # average net per lot, INR

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def band_of(vix: float | None, edges: list[float]) -> int | None:
    """0 below the first edge, 1 from the first to the second, and so on; None without a reading."""
    return None if vix is None else bisect_right(edges, vix)


def band_label(index: int, edges: list[float]) -> str:
    if index == 0:
        return f"VIX < {edges[0]:g}"
    if index == len(edges):
        return f"VIX >= {edges[-1]:g}"
    return f"VIX {edges[index - 1]:g}-{edges[index]:g}"


def history_from_trades(trades: list[Any]) -> list[HistTrade]:
    """Closed trades of a rules-only `SimResult`, per lot. Partial closes and trades with no lots are left out."""
    return [
        HistTrade(t.date, t.exit_date, t.net / t.lots, (t.market_snapshot or {}).get("vix"))
        for t in trades
        if t.exit_date and t.lots and not t.partial
    ]


def _window(history: list[HistTrade], as_of: str, gate: OddsGate) -> list[HistTrade]:
    start = None
    if gate.lookback_years:
        start = (date.fromisoformat(as_of) - timedelta(days=365 * gate.lookback_years)).isoformat()
    return [t for t in history if t.exit_date < as_of and (start is None or t.entry_date >= start)]


def _stats(trades: list[HistTrade], band: str | None, vix: float | None) -> Odds:
    nets = [t.net_per_lot for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    breakeven = None
    if avg_win and avg_loss is not None and avg_win + avg_loss > 0:
        breakeven = round(avg_loss / (avg_win + avg_loss) * 100, 2)
    return Odds(
        band=band,
        vix=vix,
        trades=len(nets),
        wins=len(wins),
        win_rate=round(len(wins) / len(nets) * 100, 2) if nets else None,
        avg_win=round(avg_win, 2) if avg_win is not None else None,
        avg_loss=round(avg_loss, 2) if avg_loss is not None else None,
        breakeven_win_rate=breakeven,
        expectancy=round(sum(nets) / len(nets), 2) if nets else None,
    )


def odds_for(history: list[HistTrade], as_of: str, vix: float | None, gate: OddsGate) -> Odds:
    """Stats of the past trades in today's VIX band, from trades that closed before `as_of`."""
    band = band_of(vix, gate.vix_bands)
    if band is None:
        return _stats([], None, None)
    same = [t for t in _window(history, as_of, gate) if band_of(t.vix_at_entry, gate.vix_bands) == band]
    return _stats(same, band_label(band, gate.vix_bands), vix)


def odds_table(history: list[HistTrade], as_of: str, gate: OddsGate) -> list[Odds]:
    """One row per VIX band, from trades that closed before `as_of`."""
    past = _window(history, as_of, gate)
    return [
        _stats([t for t in past if band_of(t.vix_at_entry, gate.vix_bands) == i], band_label(i, gate.vix_bands), None)
        for i in range(len(gate.vix_bands) + 1)
    ]


def odds_checks(odds: Odds, gate: OddsGate) -> list[Check]:
    """Both must pass: enough past trades in the band, and their average net per lot above the minimum."""
    n = odds.trades if odds.band is not None else None
    sample = Check(SAMPLE_CHECK, "band_trades", ">=", gate.min_trades, n, n is not None and n >= gate.min_trades)
    exp = odds.expectancy
    edge = Check(
        EXPECTANCY_CHECK, "avg_net_per_lot_inr", ">", gate.min_expectancy_inr, exp,
        exp is not None and exp > gate.min_expectancy_inr,
    )  # fmt: skip
    return [sample, edge]


def odds_text(odds: Odds) -> str:
    """One line for an entry's reason, such as 'VIX < 13: 24 past trades, win rate 41% vs 33% needed, ...'."""
    if odds.band is None:
        return "no VIX reading, so no odds"
    if not odds.trades:
        return f"{odds.band}: no past trades"
    parts = [f"{odds.band}: {odds.trades} past trades", f"win rate {odds.win_rate:.0f}%"]
    if odds.breakeven_win_rate is not None:
        parts[-1] += f" vs {odds.breakeven_win_rate:.0f}% needed"
    parts.append(f"average {odds.expectancy:+,.0f} INR per lot")
    return ", ".join(parts)


def strategy_history(
    rules: StrategyRules, source: MarketSource, profile: CostProfile, settings: Settings, until: str | None = None
) -> list[HistTrade]:
    """A rules-only backtest of `rules` over every date in `source` up to `until` (inclusive)."""
    from app.engine.sim import Scenario, dte_window, simulate

    dates = [d for d in source.dates() if until is None or d <= until]
    if not dates:
        return []
    raw = rules.model_dump(mode="json", exclude_none=True)
    scenario = Scenario(label="odds_history", rules=raw, lots=rules.risk.max_lots)
    feed = source.feed(dates, dte_window(rules))
    result = simulate(
        rules, feed, len(dates), profile, settings,
        scenario=scenario, mode="odds_history", data_source=source.data_source,
    )  # fmt: skip
    return history_from_trades(result.trades)
