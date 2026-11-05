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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.strategies.rules import Check

if TYPE_CHECKING:
    from app.config import Settings
    from app.engine.costs import CostProfile
    from app.market.source import MarketSource
    from app.strategies.rules import StrategyRules
    from app.trading.policy import OddsGate

NIFTY_5M_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "nifty_5m.db"
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


def _nifty_5m_db(db_path: Any = None):
    return Path(db_path) if db_path is not None else NIFTY_5M_DB_PATH


def save_nifty_5m(rows: list[dict], db_path: Any = None) -> int:
    """Store 5-minute Nifty bars. Yahoo only keeps about 60 calendar days; save whatever was fetched."""
    import sqlite3

    path = _nifty_5m_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nifty_5m (ts TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        n = 0
        for row in rows:
            ts = str(row.get("date") or row.get("timestamp") or "")[:16]
            if len(ts) < 16 or any(row.get(k) is None for k in ("open", "high", "low", "close")):
                continue
            conn.execute(
                "INSERT INTO nifty_5m (ts, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                (ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row.get("volume") or 0)),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def load_nifty_5m_sessions(before_day: str, db_path: Any = None, limit_sessions: int = 60) -> list[list[dict]]:
    """Sessions strictly before `before_day`, at most the last `limit_sessions` that are actually stored."""
    import sqlite3
    from collections import defaultdict

    path = _nifty_5m_db(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        found = conn.execute(
            "SELECT ts, open, high, low, close FROM nifty_5m WHERE ts < ? ORDER BY ts",
            (before_day,),
        ).fetchall()
    finally:
        conn.close()
    by_day: dict[str, list[dict]] = defaultdict(list)
    for ts, o, h, l, c in found:
        by_day[ts[:10]].append({"date": ts, "open": o, "high": h, "low": l, "close": c})
    days = sorted(by_day)[-limit_sessions:]
    return [by_day[d] for d in days]


def fetch_and_store_nifty_5m(db_path: Any = None) -> int:
    """Pull Yahoo's 5-minute Nifty window and store however many sessions come back."""
    try:
        from app.api.market import _chart, parse_yahoo_chart

        rows = parse_yahoo_chart(_chart("^NSEI", "5m", "60d"))
    except Exception:
        return 0
    return save_nifty_5m(rows, db_path)


def _clock_hhmm(clock: str) -> str:
    text = str(clock)
    return text[-5:] if len(text) >= 5 else text


def _slot_list(start_hhmm: str) -> list[str]:
    from datetime import datetime as dt, timedelta

    cur = dt.strptime(start_hhmm, "%H:%M")
    end = dt.strptime("15:10", "%H:%M")
    out = []
    while cur <= end:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=5)
    return out


def path_check(
    entry: float,
    stop: float,
    target: float,
    direction: str,
    clock: str,
    as_of: str,
    vix: float | None = None,
    gate: Any = None,
    history: list[HistTrade] | None = None,
    sessions: list[list[dict]] | None = None,
    chain: dict | None = None,
    n_paths: int = 10_000,
    db_path: Any = None,
) -> dict[str, Any]:
    """Replay stitched 5-minute moves from the same time of day. Does not approve or block a trade.

    Each path ends at the stop, the target, or flat at 15:10. The verdict compares the target rate
    with the break-even win rate `odds_for` already computes. Only bars from before `as_of` are used,
    and only as many sessions as are stored, up to 60.
    """
    import random

    from app.trading.policy import OddsGate

    gate = gate or OddsGate()
    odds = odds_for(history or [], as_of, vix, gate)
    if sessions is None:
        sessions = load_nifty_5m_sessions(as_of, db_path)
    sessions = sessions[-60:]
    long = str(direction).upper() in {"CALL", "LONG", "CE"}
    slots = _slot_list(_clock_hhmm(clock))
    library: dict[str, list[tuple[float, float, float]]] = {s: [] for s in slots}
    for day in sessions:
        for bar in day:
            hh = str(bar.get("date") or "")[-5:]
            if hh not in library:
                continue
            o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            library[hh].append((h - o, l - o, c - o))
    usable = any(library[s] for s in slots)
    rng = random.Random(1)
    target_n = stop_n = neither_n = 0
    neither_spots: list[float] = []
    draws = n_paths if usable else 1
    for _ in range(draws):
        px = float(entry)
        outcome = "neither"
        if usable:
            for slot in slots:
                sample = library[slot]
                if not sample:
                    continue
                up, dn, cl = sample[rng.randrange(len(sample))]
                hi, lo, done = px + up, px + dn, px + cl
                hit_stop = lo <= stop if long else hi >= stop
                hit_target = hi >= target if long else lo <= target
                if hit_stop and hit_target:
                    outcome = "stop" if abs(px - stop) <= abs(px - target) else "target"
                    break
                if hit_stop:
                    outcome = "stop"
                    break
                if hit_target:
                    outcome = "target"
                    break
                px = done
        if outcome == "target":
            target_n += 1
        elif outcome == "stop":
            stop_n += 1
        else:
            neither_n += 1
            neither_spots.append(px)
    scale = 100.0 / draws
    target_pct = round(target_n * scale, 1)
    stop_pct = round(stop_n * scale, 1)
    neither_pct = round(neither_n * scale, 1)
    be = odds.breakeven_win_rate
    if be is None or abs(target_pct - be) <= 1.0:
        verdict = "break-even"
    elif target_pct > be:
        verdict = "leans to target"
    else:
        verdict = "leans to stop"
    neither_pnl = _neither_pnl(chain, entry, direction, clock, neither_spots)
    lot = None
    if chain and chain.get("lot_size"):
        lot = int(chain["lot_size"])
    return {
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "neither_pct": neither_pct,
        "verdict": verdict,
        "odds_line": odds_text(odds),
        "neither_pnl": neither_pnl,
        "market_lot": lot,
        "sessions": len(sessions),
    }


def _neither_pnl(chain: dict | None, entry: float, direction: str, clock: str, spots: list[float]) -> float | None:
    """Price a long option still open at 15:10 with plan_pnl. None when there is no quote."""
    if not chain or not spots or not chain.get("expiry") or not chain.get("contracts"):
        return None
    from datetime import datetime

    from app.config import get_settings
    from app.market.feed import IST, MarketSnapshot
    from app.trading.option_pnl import plan_pnl

    expiry = chain["expiry"]
    quotes: dict[tuple[float, str, str], float] = {}
    lot = chain.get("lot_size")
    for row in chain["contracts"]:
        raw = float(row.get("strike") or row.get("strikePrice") or 0)
        strike = raw / 100.0 if raw > 100000 else raw
        for side, opt in (("ce", "CE"), ("pe", "PE")):
            ltp = ((row.get(side) or {}).get("liveData") or {}).get("ltp")
            try:
                if ltp is not None and float(ltp) > 0:
                    quotes[(float(strike), opt, expiry)] = float(ltp)
            except (TypeError, ValueError):
                continue
            if (row.get(side) or {}).get("marketLot"):
                lot = int(row[side]["marketLot"])
    if not quotes or not lot:
        return None
    opt = "CE" if str(direction).upper() in {"CALL", "LONG", "CE"} else "PE"
    near = min(quotes, key=lambda k: abs(k[0] - entry) if k[1] == opt else 1e9)
    if near[1] != opt:
        return None
    now = datetime.now(IST)
    try:
        snap = MarketSnapshot(
            underlying="NIFTY", timestamp=now, as_of=now.date().isoformat(), spot=float(entry),
            iv_rank=None, dte=max((datetime.fromisoformat(expiry[:10]).date() - now.date()).days, 0),
            expiry=expiry[:10], quotes=quotes, lot_size=int(lot),
        )
        hh, mm = (int(x) for x in _clock_hhmm(clock).split(":"))
        hold_days = max((15 * 60 + 10) - (hh * 60 + mm), 0) / 1440
        plan = plan_pnl(snap, get_settings(), opt, near[0], float(entry), {"neither": sum(spots) / len(spots)}, hold_days=hold_days)
    except (ValueError, KeyError):
        return None
    return plan["exits"][0]["pnl"]
