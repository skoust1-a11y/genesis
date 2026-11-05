"""S1-P6 research-only runtime: existing scanner setups through P3 admission into the P2 journal.

Explicitly invoked, one process per IST market day::

    python -m app.trading.research_runtime --journal /path/to/crew_research.db

Startup takes the P5 lease, runs P5 recovery (refused on any journal inconsistency) and reports UNKNOWN
publications, current-day orphans and admissions with no model entry. A heartbeat thread renews the lease well
inside its TTL. Each tick closes or adjusts this session's model trades with the existing ``ExitAgent`` and
``TradeManager`` milestone rules on the underlying index, under ``EXECUTION_POLICY`` (no stop ever loosens), then
admits each eligible scanner setup and opens a model trade for every RESEARCH_ONLY admission. Losing the lease
stops the runtime at once.

This is the scanner card (``scanner-v0``), not the frozen PDC Zone + 15 EMA V0 benchmark.

It publishes nothing, imports no outbox, Telegram or broker code, and never opens the legacy JSON, Excel
or ``data/trades.db`` ledgers. The daemon's ``HoldingAgent`` exits and its opposing-signal reversal are
disabled here: they depend on process state, unjournaled trade fields and the legacy risk file.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import Settings, get_settings
from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.liquidity_signals import DynamicLookoutWindow
from app.trading import admission as adm
from app.trading.crew.exit_agent import ExitAgent
from app.trading.option_quote import IST, ContractQuote, parse_chain_leg

logger = logging.getLogger("genesis.research_runtime")

# Frozen identity of the scanner card admitted here. Changing any rule source pinned in the P6 tests
# without bumping this would give new rules the old signal ids.
STRATEGY_VERSION = "scanner-v0"
# Journaled on every MODEL_ENTRY. v1 never loosens a protective stop; TradeManager's milestones can (see report).
EXECUTION_POLICY = "research-exec-v1-monotonic-stop"
# ponytail: TradeManager's frozen 3-lot baseline; its ADX-based 4-lot upgrade needs the legacy RiskAgent file.
MODEL_LOTS = 3
HEARTBEAT_SECONDS = 10.0
LOOKOUT_MULTIPLIER = 4.0
INTRADAY_CUTOFF = "15:10"
HOP = "LIQUIDITY_HOP_PROMOTED"
MILESTONE_BE = "MILESTONE_0.7R"
MILESTONE_2R = "MILESTONE_2R"

_REPO = Path(__file__).resolve().parents[3]
_PROTECTED_DIRS = tuple(p.resolve() for p in (
    _REPO / "data", _REPO / "trades", _REPO / "reports", _REPO / "backend" / "data", _REPO / "backend" / "trades"))


class StartupRefused(Exception):
    pass


@dataclass(frozen=True)
class MarketView:
    df_5m: pd.DataFrame  # closed 5-minute NIFTY bars only
    setups: list  # scanner CandidateSetups
    chain: dict | None  # {"contracts", "expiry", "fetched_at"}; fetched_at is timezone-aware
    key_levels: list[float]


def _now() -> datetime:
    return datetime.now(IST)


class ResearchRuntime:
    def __init__(self, journal_path: Path | str, feed: Callable[[datetime], MarketView], *, settings: Settings,
                 clock: Callable[[], datetime] = _now, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        if not 0 < heartbeat_seconds * 3 <= cs.LEASE_SECONDS:
            raise ValueError("heartbeat must renew at least three times per lease")
        self.path = Path(journal_path).expanduser().resolve()
        self.feed, self.settings, self.clock = feed, settings, clock
        self.heartbeat_seconds = heartbeat_seconds
        self.lease: cj.Lease | None = None
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._beater: threading.Thread | None = None
        self.exit_agent = ExitAgent(intraday_cutoff_time=INTRADAY_CUTOFF)

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> dict:
        """Lease, recover, start the heartbeat. Raises LeaseHeld, ConsistencyError or StartupRefused."""
        _check_journal_path(self.path)
        cj.migrate(self.path)
        self.lease = cs.acquire(self.path, now=self.clock())
        try:
            report = cs.recover(self.path, self.lease, now=self.clock())
        except BaseException:
            self._release("RECOVERY_REFUSED")
            raise
        for pid in report["unknown_publications"]:
            logger.warning("UNKNOWN publication %s is unresolved; it is never resent", pid)
        for trade in report["orphaned_open"]:
            logger.warning("current-day orphan %s (owner %s) is left untouched", trade["trade_id"],
                           trade["owner_session_id"])
        report["missing_model_entries"] = missing_model_entries(self.path)
        for row in report["missing_model_entries"]:
            logger.warning("admission %s (session %s) has no model entry; it is never opened later",
                           row["admission_id"], row["session_id"])
        self._beater = threading.Thread(target=self._beat, name="crew-lease-heartbeat", daemon=True)
        self._beater.start()
        return {"session_id": self.lease.session_id, "market_day": self.lease.market_day,
                "generation": self.lease.generation, **report}

    def stop(self, reason: str = "SHUTDOWN") -> None:
        self._stop.set()
        if self._beater is not None:
            self._beater.join()
        if self.lease is not None and not self.lost.is_set():
            self._release(reason)

    def run(self, stop: threading.Event, *, interval: float, once: bool = False) -> int:
        """Tick until ``stop`` is set. 0 clean, 3 lease lost, 1 journal or invariant failure."""
        code = 0
        try:
            while not stop.is_set():
                self.tick()
                if once:
                    break
                stop.wait(interval)
        except cs.LeaseLost as e:
            logger.error("lease lost, processing stopped: %s", e)
            self.lost.set()
            code = 3
        except Exception:
            logger.exception("research runtime stopped")
            code = 1
        finally:
            self.stop("SHUTDOWN" if code == 0 else f"STOPPED_{code}")
        return code

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                cs.heartbeat(self.path, self.lease, now=self.clock())
            except cs.LeaseLost as e:
                logger.error("heartbeat fenced: %s", e)
                self.lost.set()
                return
            except (sqlite3.Error, cj.JournalError) as e:
                # The next beat retries; writes stay fenced, so an expired lease cannot write meanwhile.
                logger.warning("heartbeat failed: %s", e)

    def _release(self, reason: str) -> None:
        try:
            cs.release(self.path, self.lease, reason=reason, now=self.clock())
        except cs.LeaseLost as e:
            logger.error("release fenced: %s", e)

    def _check(self) -> None:
        if self.lost.is_set():
            raise cs.LeaseLost(f"{self.lease.session_id}: heartbeat lost the lease")

    # --- one scan ----------------------------------------------------------------------------------

    def tick(self) -> dict:
        self._check()
        now = self.clock()
        try:
            view = self.feed(now)
        except Exception as e:  # noqa: BLE001 - a market-data failure skips the tick; journal errors still stop it
            logger.warning("market data unavailable: %s", e)
            return {"skipped": "FEED_ERROR"}
        if view.df_5m is None or view.df_5m.empty:
            return {"skipped": "NO_CLOSED_BAR"}
        return {"exits": self._monitor(view, now), "admissions": self._admit(view, now)}

    def _monitor(self, view: MarketView, now: datetime) -> list[dict]:
        bar = view.df_5m.iloc[-1]
        cmp = float(bar["close"])
        candle = _bar_close(bar)
        clock = now.astimezone(IST).strftime("%H:%M")
        out = []
        for trade in cj.load_open_trades(self.path, market_day=self.lease.market_day):
            if trade["owner_session_id"] != self.lease.session_id:
                continue
            self._check()
            out.append(self._manage(trade, cmp, candle, clock, view))
        return out

    def _manage(self, trade: dict, cmp: float, candle: str, clock: str, view: MarketView) -> dict:
        """One trade, one bar: TradeManager.on_market_tick step 1 without HoldingAgent or the opposing signal."""
        tid, version = trade["trade_id"], trade["version"]
        events = cj.get_trade_events(self.path, tid)
        state = _legacy_trade(trade, events, clock)
        hopped_partial = any(e["event_type"] == "MODEL_PARTIAL_EXIT" and e["payload"]["reason"] == HOP for e in events)
        write = {"lease": self.lease, "trade_id": tid, "signal_candle": candle}
        if state["has_hopped"] and not hopped_partial:
            lots = state["lots"]
            cj.partial_exit(self.path, **write, expected_version=version, reason=HOP, lots_after=lots - lots // 2,
                            index_levels={"target": state["target_price"]}, now=self.clock())
            return {"trade_id": tid, "action": "HOP_PARTIAL_COMPLETED"}

        decision = self.exit_agent.evaluate_exit(
            active_trade=state, cmp=cmp, df_5m=view.df_5m, opposing_signal_detected=False,
            current_time_str=clock, key_levels=view.key_levels, enable_liquidity_hopping=True,
            bars_in_trade=state["bars_in_trade"],
        )
        direction, current = state["direction"], state["stop_loss"]
        if decision.action == HOP:
            moved = cj.update_levels(self.path, **write, expected_version=version,
                                     stop_spot=_tighter(direction, current, decision.ratcheted_sl),
                                     target_spot=decision.next_target, reason=HOP, now=self.clock())
            cj.partial_exit(self.path, **write, expected_version=moved["trade"]["version"], reason=HOP,
                            lots_after=decision.remaining_lots, index_levels={"target": decision.exit_price},
                            now=self.clock())
            return {"trade_id": tid, "action": HOP, "lots_after": decision.remaining_lots}
        if decision.is_fully_closed:
            cj.exit_trade(self.path, **write, expected_version=version, reason=decision.action,
                          index_levels={"spot": cmp, "stop": state["stop_loss"], "target": state["target_price"]},
                          model_points=decision.realized_points, model_r=decision.realized_r, now=self.clock())
            return {"trade_id": tid, "action": decision.action, "model_points": decision.realized_points}

        r_now, entry, risk = decision.realized_r, state["entry_price"], state["risk_points"]
        milestones = state["milestones_hit"]
        if r_now >= 0.7 and MILESTONE_BE not in milestones:
            stop, reason = entry, MILESTONE_BE
        elif r_now >= 2.0 and MILESTONE_2R not in milestones:
            stop = round(entry + risk * 0.8, 2) if direction == "CALL" else round(entry - risk * 0.8, 2)
            reason = MILESTONE_2R
        else:
            return {"trade_id": tid, "action": "HOLD"}
        stop = _tighter(direction, current, stop)
        # A held stop can repeat the hop's (candle, stop, target) key, so each milestone gets its own once-only key.
        cj.update_levels(self.path, **write, expected_version=version, stop_spot=stop, reason=reason,
                         idempotency_key=f"{tid}:levels:{reason}", now=self.clock())
        return {"trade_id": tid, "action": reason, "stop": stop}

    def _admit(self, view: MarketView, now: datetime) -> list[dict]:
        clock = now.astimezone(IST).strftime("%H:%M")
        out = []
        for setup in view.setups:
            if not getattr(setup, "is_valid_for_trade_crew", False):
                continue
            window = DynamicLookoutWindow(setup_name=setup.strategy, base_tf=str(setup.timeframe),
                                          multiplier=LOOKOUT_MULTIPLIER, detected_time_str=str(setup.timestamp))
            if not window.is_valid_window(clock):
                continue
            self._check()
            out.append(self._admit_one(setup, view.chain))
        return out

    def _admit_one(self, setup, chain: dict | None) -> dict:
        candidate = adm.SignalCandidate(
            market_day=self.lease.market_day, instrument=setup.symbol, strategy_id=setup.strategy,
            strategy_version=STRATEGY_VERSION, signal_candle=_ist_candle(setup.timestamp),
            direction=setup.direction, setup_id=f"{setup.timeframe}|{setup.trigger_price}",
        )
        admission = adm.evaluate(candidate, setup_quote(chain, setup), now=self.clock(), settings=self.settings)
        if admission.admission_class == "ACTIONABLE":
            raise RuntimeError("ACTIONABLE admission in the research-only runtime")
        persisted = adm.persist(self.path, admission, lease=self.lease, now=self.clock())
        if persisted is None:
            return {"strategy": setup.strategy, "recorded": False, "reasons": list(admission.reasons)}
        stored = persisted["admission"]
        result = {"admission_id": stored["admission_id"], "class": stored["class"], "replayed": persisted["replayed"]}
        if stored["class"] != "RESEARCH_ONLY":
            return result
        if _has_trade(self.path, stored["signal_id"]):
            return {**result, "trade": "EXISTS" if stored["session_id"] == self.lease.session_id else "PRIOR_SESSION"}
        if persisted["replayed"]:
            # Admission and entry share a tick, so a stored admission without a trade was interrupted. Opening it
            # now would start the model late at the old trigger; it stays a reported missing entry.
            return {**result, "trade": "MISSING_ENTRY"}
        opened = adm.open_research_trade(
            self.path, persisted, lease=self.lease, model_lots=MODEL_LOTS, entry_spot=float(setup.trigger_price),
            stop_spot=float(setup.tentative_stop), target_spot=float(setup.tentative_target),
            execution_policy=EXECUTION_POLICY, now=self.clock(),
        )
        return {**result, "trade": opened["trade"]["trade_id"]}


# --- helpers -------------------------------------------------------------------------------------------

def _check_journal_path(path: Path) -> None:
    """A new file, or an existing crew-only journal, outside every legacy and production data directory."""
    if any(path.is_relative_to(d) for d in _PROTECTED_DIRS):
        raise StartupRefused(f"{path} is inside a protected data directory")
    if not path.exists():
        return
    try:
        conn = cj.connect_readonly(path)
        try:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise StartupRefused(f"{path} is not a SQLite crew journal: {e}") from e
    if "crew_meta" not in names or any(not n.startswith(("crew_", "sqlite_")) for n in names):
        raise StartupRefused(f"{path} exists and is not a crew-only journal")


def setup_quote(chain: dict | None, setup) -> ContractQuote | None:
    """The chain quote for the scanner's own suggested strike. None (QUOTE_MISSING) when it is absent."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(CE|PE)\s*$", str(getattr(setup, "suggested_strike", "") or "").upper())
    if not chain or not match:
        return None
    for row in chain.get("contracts") or []:
        quote = parse_chain_leg(row, match.group(2), chain.get("expiry"), chain.get("fetched_at"))
        if quote is not None and quote.strike == float(match.group(1)):
            return quote
    return None


def _legacy_trade(trade: dict, events: list[dict], clock: str) -> dict:
    """The JournalAgent row ExitAgent reads, rebuilt from the journal so a replayed bar decides the same way."""
    reasons = {e["payload"].get("reason") for e in events if e["event_type"] == "MODEL_LEVELS_UPDATED"}
    entry = trade["entry_spot"]
    opened = datetime.fromisoformat(trade["opened_at"]).astimezone(IST)
    elapsed = (datetime.strptime(clock, "%H:%M") - datetime.strptime(opened.strftime("%H:%M"), "%H:%M"))
    return {
        "direction": trade["direction"], "entry_price": entry, "stop_loss": trade["stop_spot"],
        "target_price": trade["target_spot"], "lots": trade["model_lots_open"],
        "risk_points": abs(entry - events[0]["payload"]["stop_spot"]),
        "has_hopped": HOP in reasons, "be_locked": MILESTONE_BE in reasons, "milestones_hit": reasons,
        "bars_in_trade": max(0, int(elapsed.total_seconds() / 60.0 // 5)),
    }


def _ist_candle(text: Any) -> str:
    """Scanner bar times are naive IST ``YYYY-MM-DD HH:MM``. Unparseable text fails the signal identity."""
    try:
        parsed = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return ""
    return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)).isoformat()


def _bar_close(bar) -> str:
    start = datetime.fromisoformat(str(bar.get("date") or bar.get("timestamp")).strip()[:16])
    return (start + timedelta(minutes=5)).replace(tzinfo=IST).isoformat()


def _tighter(direction: str, current: float, proposed: float) -> float:
    """A protective stop only moves toward price: up for a CALL, down for a PUT."""
    return max(current, proposed) if direction == "CALL" else min(current, proposed)


def missing_model_entries(path: Path | str) -> list[dict]:
    """Recorded non-rejected admissions with no model trade: the process stopped between admission and entry.

    They keep their original session and are never opened by a later session, which could only invent the entry.
    """
    conn = cj.connect_readonly(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT a.admission_id, a.signal_id, a.market_day, a.session_id, a.class, a.decided_at"
            " FROM crew_admissions a LEFT JOIN crew_trades t ON t.signal_id = a.signal_id"
            " WHERE a.class <> 'REJECTED' AND t.trade_id IS NULL ORDER BY a.decided_at, a.admission_id")]
    finally:
        conn.close()


def finalized_results(path: Path | str, *, execution_policy: str = EXECUTION_POLICY) -> dict:
    """Index-point and R totals over CLOSED scanner-v0 model trades of one execution policy, with no premium P&L.

    Open, orphaned, expired, quarantined, other-strategy-version and other-policy trades, and admissions with no
    model entry, are listed and never counted. There is no reconciliation path yet, so they stay excluded.
    """
    conn = cj.connect_readonly(path)
    try:
        trades = [dict(r) for r in conn.execute("SELECT trade_id, state FROM crew_trades ORDER BY trade_id")]
        payloads = {(r["trade_id"], r["event_type"]): json.loads(r["payload"]) for r in conn.execute(
            "SELECT trade_id, event_type, payload FROM crew_events WHERE event_type IN ('MODEL_ENTRY', 'MODEL_EXIT')")}
    finally:
        conn.close()
    finalized, excluded = [], {}
    for trade in trades:
        tid = trade["trade_id"]
        exit_ = payloads.get((tid, "MODEL_EXIT"), {})
        if trade["state"] != "CLOSED" or "model_points" not in exit_ or "model_r" not in exit_:
            excluded[tid] = trade["state"]
        elif payloads.get((tid, "MODEL_ENTRY"), {}).get("strategy_version") != STRATEGY_VERSION:
            excluded[tid] = "OTHER_STRATEGY_VERSION"
        elif payloads.get((tid, "MODEL_ENTRY"), {}).get("execution_policy") != execution_policy:
            excluded[tid] = "OTHER_EXECUTION_POLICY"
        else:
            finalized.append({"trade_id": tid, "model_points": exit_["model_points"], "model_r": exit_["model_r"]})
    return {
        "strategy_version": STRATEGY_VERSION, "execution_policy": execution_policy,
        "eligibility": "CLOSED with model_points and model_r; MODEL_ENTRY strategy_version and execution_policy match",
        "units": "index points and R only", "trades": len(finalized),
        "model_points": round(sum(t["model_points"] for t in finalized), 2),
        "model_r": round(sum(t["model_r"] for t in finalized), 2),
        "finalized": finalized, "excluded": excluded, "missing_model_entries": missing_model_entries(path),
    }


def _has_trade(path: Path, signal_id: str) -> bool:
    conn = cj.connect_readonly(path)
    try:
        return conn.execute("SELECT 1 FROM crew_trades WHERE signal_id=?", (signal_id,)).fetchone() is not None
    finally:
        conn.close()


def scanner_feed(scanner=None, chain_source: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] = _now) -> Callable[[datetime], MarketView]:
    """The live scanner's market step (5m closed bars, Groww NIFTY chain, ScannerManager snapshot), read-only."""
    if scanner is None:
        from app.trading.scanner_crew.scanner_manager import ScannerManager
        scanner = ScannerManager()
    if chain_source is None:
        def chain_source():
            from app.brokers.groww import GrowwQuotes
            return GrowwQuotes()._page("NIFTY")

    def feed(now: datetime) -> MarketView:
        page = chain_source() or {}
        fetched_at = clock()
        oc = page.get("optionChain") or {}
        chain = {"contracts": oc.get("optionContracts") or [],
                 "expiry": (oc.get("aggregatedDetails") or {}).get("currentExpiry"), "fetched_at": fetched_at}
        df = _closed_bars(scanner.feed_agent.fetch_timeframe_candles("5m"), now)
        snap = scanner.produce_scan_snapshot(df_5m=df, current_time_str=now.astimezone(IST).strftime("%H:%M"),
                                             timeframes=["1m", "5m", "15m"], option_contracts=chain["contracts"] or None)
        return MarketView(df_5m=df, setups=list(snap.actionable_setups), chain=chain,
                          key_levels=list(snap.levels.all_key_levels))

    return feed


def _closed_bars(df: pd.DataFrame | None, now: datetime) -> pd.DataFrame:
    """A 5-minute bar is not a signal until it has closed."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    last = df.iloc[-1]
    try:
        start = datetime.strptime(str(last.get("date") or last.get("timestamp") or "")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return pd.DataFrame()
    return df.iloc[:-1].copy() if now.astimezone(IST).replace(tzinfo=None) < start + timedelta(minutes=5) else df


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1 research-only scanner runtime (no Telegram, no broker)")
    parser.add_argument("--journal", required=True, type=Path, help="crew research journal (new or crew-only)")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    runtime = ResearchRuntime(args.journal, scanner_feed(), settings=get_settings())
    try:
        report = runtime.start()
    except (StartupRefused, cs.LeaseHeld, cj.ConsistencyError) as e:
        logger.error("startup refused: %s", e)
        return 2
    print(json.dumps(report, indent=2, default=str))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return runtime.run(stop, interval=args.interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
