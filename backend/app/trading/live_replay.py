"""Offline replay of the live scanner-v0 runtime over historical NIFTY 5-minute bars.

The unmodified ``ResearchRuntime`` (``scanner_feed`` -> ``ScannerManager`` -> ``DetectorAgent`` ->
``liquidity_signals``, then admission, ``ExitAgent`` and milestone management, and the paper ledger under
paper-risk-v2) is driven by a historical clock and a historical ``FeedAgent``, one tick per minute, into a
temporary crew journal. Nothing in this module decides a trade.

Lanes:

- ``LIVE``: point-in-time index bars only. No historical option chain exists, so admission sees an empty chain,
  exactly as the live runtime does when its chain source returns nothing.
- ``SYNTHETIC``: the chain is priced by Black-76 at the previous session's India VIX close (forward = spot) with a
  fixed relative spread, and its Black-76 delta is trusted only inside this lane. These premiums are model output,
  never fill evidence.

Not replayable, and reported as such: the 1m and 15m frames (live reads Yahoo's still-forming bar), option-chain
open-interest walls in the key levels (live chain OI is intraday and not archived), news headlines (the live
runtime wires none, so its event guard never fires), and days in years the NSE holiday file does not cover
(the runtime refuses to start).
"""

from __future__ import annotations

import functools
import math
import sqlite3
import types
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import pandas as pd

from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import paper_ledger as pl
from app.trading import research_runtime as rr
from app.trading.market_validation import closed_bars
from app.trading.option_pnl import _cdf, black76
from app.trading.option_quote import IST
from app.trading.paper_risk import DeltaEvidence
from app.trading.scanner_crew.consolidation_agent import ConsolidationAgent
from app.trading.scanner_crew.detector_agent import DetectorAgent
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.news_agent import NewsAgent
from app.trading.scanner_crew.scanner_manager import ScannerManager

REPLAY_VERSION = "live-replay-v1"
YAHOO_5M_SESSIONS = 5  # FeedAgent's 5m range is "5d"
YAHOO_1D_SESSIONS = 22  # and its 1d range is "1mo"
NOT_REPLAYABLE_TIMEFRAMES = ("1m", "15m")
SYNTHETIC_SOURCE = "SYNTHETIC_BLACK76"
TICK = Decimal("0.05")
LANES = ("LIVE", "SYNTHETIC")


class Clock:
    def __init__(self, t: datetime | None = None):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


# --- historical data ------------------------------------------------------------------------------------


def load_days(kaggle_csv: Path | None = None, db_5m: Path | None = None) -> dict[str, list[dict]]:
    """5m NIFTY bars by IST day, labelled by start time, volume 0 like Yahoo's index series."""
    bars: list[dict] = []
    if kaggle_csv:
        from scripts.backtest_liquidity import kaggle_5m

        bars += [b | {"volume": 0.0} for b in kaggle_5m(Path(kaggle_csv))]
    if db_5m:
        conn = sqlite3.connect(f"file:{Path(db_5m)}?mode=ro", uri=True)
        try:
            bars += [{"date": ts, "open": o, "high": h, "low": low, "close": c, "volume": float(v or 0.0)}
                     for ts, o, h, low, c, v in conn.execute(
                         "SELECT ts, open, high, low, close, volume FROM nifty_5m ORDER BY ts")]
        finally:
            conn.close()
    days: dict[str, list[dict]] = {}
    for b in sorted(bars, key=lambda x: x["date"]):
        day = days.setdefault(b["date"][:10], [])
        if day and day[-1]["date"] == b["date"]:
            raise ValueError(f"duplicate 5m bar {b['date']} across sources")
        day.append(b)
    return days


def load_vix(eod_db: Path) -> dict[str, float]:
    """India VIX close of each session from the NSE EOD table."""
    conn = sqlite3.connect(f"file:{Path(eod_db)}?mode=ro", uri=True)
    try:
        return {d: float(v) for d, v in conn.execute(
            "SELECT date, vix FROM index_bars WHERE underlying='NIFTY' AND vix IS NOT NULL ORDER BY date")}
    finally:
        conn.close()


def load_listed_expiries(eod_db: Path) -> dict[str, list[str]]:
    """NIFTY option expiries listed in each session's bhavcopy."""
    conn = sqlite3.connect(f"file:{Path(eod_db)}?mode=ro", uri=True)
    try:
        out: dict[str, list[str]] = {}
        for d, e in conn.execute(
                "SELECT DISTINCT date, expiry FROM option_eod WHERE underlying='NIFTY' ORDER BY date, expiry"):
            out.setdefault(d, []).append(e)
        return out
    finally:
        conn.close()


def prior_session(series: dict[str, object], day: str, max_gap_days: int = 6):
    """The value of the last session before `day`: what was known at its open. None if that is too old."""
    keys = sorted(series)
    i = bisect_left(keys, day)
    if not i or (date.fromisoformat(day) - date.fromisoformat(keys[i - 1])).days > max_gap_days:
        return None
    return series[keys[i - 1]]


class HistoricalFeedAgent(FeedAgent):
    """FeedAgent over stored bars at the replay clock: what Yahoo would serve at that instant, never later bars."""

    def __init__(self, days: dict[str, list[dict]], clock: Clock):
        super().__init__()
        self.days, self.order, self.clock = days, sorted(days), clock

    def fetch_timeframe_candles(self, timeframe: str = "5m", limit_bars: int = 50, mock_df=None) -> pd.DataFrame:
        now = self.clock().astimezone(IST).strftime("%Y-%m-%d %H:%M")
        i = bisect_right(self.order, now[:10])
        if timeframe == "5m":
            # Yahoo serves the bar in progress too; the runtime's closed_bars() drops it.
            sessions = self.order[max(0, i - YAHOO_5M_SESSIONS):i]
            return pd.DataFrame([b for d in sessions for b in self.days[d] if b["date"] <= now]).tail(limit_bars)
        if timeframe == "1d":
            rows = []
            for d in self.order[max(0, i - YAHOO_1D_SESSIONS):i]:
                seen = [b for b in self.days[d] if b["date"] <= now]
                if seen:
                    rows.append({"date": d, "open": seen[0]["open"], "high": max(b["high"] for b in seen),
                                 "low": min(b["low"] for b in seen), "close": seen[-1]["close"], "volume": 0.0})
            return pd.DataFrame(rows).tail(limit_bars)
        return pd.DataFrame()  # 1m / 15m: not replayable


class MemoDetector(DetectorAgent):
    """The live DetectorAgent, memoized on its complete input: the frames, the levels, the news verdict and the
    compression verdict for the clock (its only other use of the time)."""

    _key = _value = None

    def scan_multi_timeframe_strategies(self, tf_bars_map, levels, news_sentiment=None, current_time_str=None,
                                        strategy_filter="auto", allow_reentry=True):
        frames = {tf: raw.to_dict(orient="records") if isinstance(raw, pd.DataFrame) else list(raw)
                  for tf, raw in tf_bars_map.items()}
        key = (repr(frames), repr(levels), repr(news_sentiment), strategy_filter, allow_reentry,
               None if news_sentiment is not None else current_time_str,
               tuple(repr(self.consolidation_agent.evaluate_compression(pd.DataFrame(b), current_time_str=current_time_str))
                     for b in frames.values() if len(b) >= 5))
        if key != self._key:
            self._key = key
            self._value = super().scan_multi_timeframe_strategies(
                tf_bars_map, levels, news_sentiment=news_sentiment, current_time_str=current_time_str,
                strategy_filter=strategy_filter, allow_reentry=allow_reentry)
        return list(self._value)


class RecordingScanner(ScannerManager):
    """ScannerManager that keeps every candidate, vetoed or not, of every snapshot."""

    def __init__(self, feed_agent: FeedAgent, clock: Clock):
        consolidation, news = ConsolidationAgent(), NewsAgent()
        super().__init__(feed_agent=feed_agent, consolidation_agent=consolidation, news_agent=news,
                         detector_agent=MemoDetector(consolidation_agent=consolidation, news_agent=news))
        self.clock = clock
        self.candidates: dict[tuple, dict] = {}
        self.news_gated_ticks = 0

    def produce_scan_snapshot(self, *args, **kwargs):
        snap = super().produce_scan_snapshot(*args, **kwargs)
        if snap.news_sentiment.high_impact_event_active:
            self.news_gated_ticks += 1
        now = self.clock().isoformat()
        for c in snap.candidate_setups:
            row = self.candidates.setdefault(identity(c), {
                "strategy": c.strategy, "timeframe": c.timeframe, "timestamp": c.timestamp,
                "direction": c.direction, "trigger_price": c.trigger_price, "tentative_stop": c.tentative_stop,
                "tentative_target": c.tentative_target, "risk_points": c.risk_points,
                "suggested_strike": c.suggested_strike, "rationale": c.rationale,
                "first_seen": now, "first_valid": None, "veto_reasons": []})
            if c.is_valid_for_trade_crew:
                row["first_valid"] = row["first_valid"] or now
            elif c.veto_reason and c.veto_reason not in row["veto_reasons"]:
                row["veto_reasons"].append(c.veto_reason)
        return snap


def identity(setup) -> tuple:
    """The runtime's signal identity fields (admission: strategy, candle, direction, timeframe|trigger)."""
    return (str(setup.strategy), str(setup.timestamp), str(setup.direction), f"{setup.timeframe}|{setup.trigger_price}")


# --- synthetic chain ------------------------------------------------------------------------------------


def b76_delta(forward: float, strike: float, years: float, iv: float, option_type: str) -> float:
    if years <= 0 or iv <= 0:
        itm = forward > strike if option_type == "CE" else forward < strike
        return (1.0 if option_type == "CE" else -1.0) if itm else 0.0
    sd = iv * math.sqrt(years)
    d1 = math.log(forward / strike) / sd + sd / 2
    return _cdf(d1) if option_type == "CE" else _cdf(d1) - 1.0


def _ticks(value: float, up: bool) -> Decimal:
    n = Decimal(str(value)) / TICK
    return max(TICK, n.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * TICK)


def synthetic_chain(feed_agent: HistoricalFeedAgent, clock: Clock, *, vix: dict[str, float],
                    expiry_for, lot_for, spread_pct: float, width: int = 20, step: int = 50):
    """A Groww-shaped NIFTY chain priced by Black-76 at the last closed 5m close; `.deltas` holds its deltas."""
    deltas: dict[str, DeltaEvidence] = {}

    def source() -> dict:
        deltas.clear()
        now = clock()
        day = now.astimezone(IST).date().isoformat()
        iv = prior_session(vix, day)
        df = closed_bars(feed_agent.fetch_timeframe_candles("5m"), now)
        expiry, lot = expiry_for(day), lot_for(day)
        if iv is None or df.empty or expiry is None or lot is None:
            return {}
        spot = float(df.iloc[-1]["close"])
        close = datetime.combine(date.fromisoformat(expiry), datetime.strptime("15:30", "%H:%M").time(), IST)
        years = max((close - now).total_seconds(), 0.0) / (365 * 86400)
        atm, rows = round(spot / step) * step, []
        for k in range(-width, width + 1):
            strike, row = atm + k * step, {"strike": atm + k * step}
            for kind in ("CE", "PE"):
                premium = black76(spot, strike, years, iv / 100, kind)
                bid = _ticks(premium * (1 - spread_pct / 200), up=False)
                ask = max(bid + TICK, _ticks(premium * (1 + spread_pct / 200), up=True))
                sym = f"SYN-NIFTY-{expiry}-{strike:g}{kind}"
                row[kind.lower()] = {"liveData": {"ltp": round(premium, 2), "bid": str(bid), "ask": str(ask),
                                                  "quote_time": now.isoformat()},
                                     "marketLot": lot, "growwContractId": sym, "tradingSymbol": sym}
                deltas[sym] = DeltaEvidence(Decimal(str(round(b76_delta(spot, strike, years, iv / 100, kind), 6))),
                                            now, sym, SYNTHETIC_SOURCE)
            rows.append(row)
        return {"optionChain": {"optionContracts": rows, "aggregatedDetails": {"currentExpiry": expiry}}}

    source.deltas = deltas
    return source


@contextmanager
def historical_fee_assumption():
    """Lets the paper ledger price pre-verified dates with the repository's historical fee specs (an assumption
    the ledger API already supports and records on each fill). Only for this process, restored on exit."""
    original = rr.pl
    proxy = types.SimpleNamespace(**vars(pl))
    proxy.enter = functools.partial(pl.enter, allow_historical_brokerage_assumption=True)
    rr.pl = proxy
    try:
        yield
    finally:
        rr.pl = original


# --- runtime driver -------------------------------------------------------------------------------------


class ReplayRuntime(rr.ResearchRuntime):
    """The live runtime with its heartbeat thread replaced by the replay loop's explicit heartbeats."""

    def _beat(self) -> None:
        return


@dataclass
class DayResult:
    day: str
    status: str
    detail: str | None = None
    ticks: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)  # "timeframe:reason" -> ticks


def replay_feed(days, clock: Clock, *, lane: str, vix=None, expiry_for=None, lot_for=None, spread_pct=1.0):
    """The live ``scanner_feed`` over historical data, plus (SYNTHETIC) the chain's deltas on each view."""
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")
    agent = HistoricalFeedAgent(days, clock)
    scanner = RecordingScanner(agent, clock)
    chain = (synthetic_chain(agent, clock, vix=vix, expiry_for=expiry_for, lot_for=lot_for, spread_pct=spread_pct)
             if lane == "SYNTHETIC" else (lambda: {}))
    inner = rr.scanner_feed(scanner=scanner, chain_source=chain, clock=clock)

    def feed(now: datetime) -> rr.MarketView:
        view = inner(now)
        return replace(view, delta_evidence=dict(chain.deltas)) if lane == "SYNTHETIC" else view

    feed.scanner, feed.sources = scanner, {"index_bars": "historical_5m", "option_chain": lane, "delta": lane}
    return feed


def replay(days: dict[str, list[dict]], journal: Path, *, settings, lane: str = "LIVE", start: str | None = None,
           end: str | None = None, vix=None, expiry_for=None, lot_for=None, spread_pct: float = 1.0,
           first: str = "09:15", last: str = "15:30") -> tuple[list[DayResult], object]:
    """Replay each stored session in [start, end] minute by minute through the live runtime into `journal`."""
    clock = Clock()
    feed = replay_feed(days, clock, lane=lane, vix=vix, expiry_for=expiry_for, lot_for=lot_for,
                       spread_pct=spread_pct)
    trusted = frozenset({SYNTHETIC_SOURCE}) if lane == "SYNTHETIC" else frozenset()
    results = []
    for day in sorted(d for d in days if (start or "") <= d <= (end or "9999")):
        clock.t = datetime.fromisoformat(f"{day}T{first}:05+05:30")
        runtime = ReplayRuntime(journal, feed, settings=settings, clock=clock, trusted_delta_sources=trusted)
        try:
            runtime.start()
        except rr.StartupRefused as e:
            results.append(DayResult(day, "NOT_REPLAYABLE", str(e)))
            continue
        result = DayResult(day, "REPLAYED")
        try:
            stop = datetime.fromisoformat(f"{day}T{last}:05+05:30")
            while clock.t <= stop:
                out = runtime.tick()
                result.ticks += 1
                if "skipped" in out:
                    result.skipped[out["skipped"]] = result.skipped.get(out["skipped"], 0) + 1
                for tf, reasons in out.get("rejected_timeframes", {}).items():
                    for why in reasons:
                        result.rejected[f"{tf}:{why}"] = result.rejected.get(f"{tf}:{why}", 0) + 1
                for _ in range(3):  # the 30 s lease is renewed every 20 s, as the heartbeat thread would
                    clock.t += timedelta(seconds=20)
                    cs.heartbeat(runtime.path, runtime.lease, now=clock.t)
        finally:
            runtime.stop("REPLAY_DAY_END")
        results.append(result)
    return results, feed


def replay_settings(settings, *, paper: bool):
    """No delivery, no live flags; paper only with the approved ₹50,000 research capital."""
    return settings.model_copy(update={
        "telegram_delivery_enabled": False, "alert_telegram_bot_token": None, "live_trading_enabled": False,
        "sandbox_allow_live": False, "paper_capital_inr": Decimal("50000") if paper else None})


def check_output(path: Path, repo: Path = rr._REPO) -> Path:
    """Replay journals and reports live outside the repository, like signal-only journals."""
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(repo):
        raise rr.StartupRefused("replay outputs must be outside the repository")
    return path


def journal_rows(path: Path, sql: str, args=()) -> list[dict]:
    conn = cj.connect_readonly(path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
