"""S1-P6 research-only runtime: existing scanner setups through P3 admission into the P2 journal.

Explicitly invoked, one process per IST market day::

    python -m app.trading.research_runtime --journal /path/to/crew_research.db

Startup takes the P5 lease, runs P5 recovery (refused on any journal inconsistency) and reports UNKNOWN
publications, current-day orphans and admissions with no model entry. A heartbeat thread renews the lease well
inside its TTL. Each tick closes or adjusts this session's model trades with the existing ``ExitAgent`` and
``TradeManager`` milestone rules on the underlying index, under ``EXECUTION_POLICY`` (no stop ever loosens), then
admits each eligible scanner setup and opens a model trade for every RESEARCH_ONLY admission. Losing the lease
stops the runtime at once.

This is the scanner card (``scanner-v0.1``), not the frozen PDC Zone + 15 EMA V0 benchmark.

Admissions run only inside the NSE session (``market_status``), and startup is refused when the holiday file
has no dates for the current year. With ``PAPER_CAPITAL_INR`` set, every model entry, half-exit and exit is
mirrored in the paper ledger (``paper_ledger``: simulated bid/ask fills, never an order) and paper notifications
are enqueued as OPS rows and delivered when Telegram delivery is explicitly enabled. Without it the runtime stays research-only.

It places no broker orders, and never opens the legacy JSON, Excel or ``data/trades.db`` ledgers.
The daemon's ``HoldingAgent`` exits and its opposing-signal reversal are disabled here: they depend on process
state, unjournaled trade fields and the legacy risk file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.state_authority import (
    AuthorityError, Environment, Resource, StateAuthorityContract, preflight,
)
from app.config import Settings, get_settings
from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import paper_ledger as pl
from app.liquidity_signals import DynamicLookoutWindow
from app.market.hours import holidays, market_status
from app.trading import admission as adm
from app.trading import market_validation as mv
from app.trading import paper_exec as px
from app.trading import paper_delivery
from app.trading import paper_notify as pn
from app.trading import paper_registry as reg
from app.trading.crew.exit_agent import ExitAgent
from app.trading.market_validation import closed_bars as _closed_bars
from app.trading.option_quote import IST, ContractQuote
from app.trading.paper_risk import DeltaEvidence

logger = logging.getLogger("genesis.research_runtime")

# Frozen identity of the scanner card admitted here. Changing any rule source pinned in the P6 tests
# without bumping this would give new rules the old signal ids. v0.1: every timeframe sees only the closed bars
# of the current session and PDH/PDL come from the previous NSE session's daily bar; a timeframe whose session or
# levels cannot be verified complete gets no bars (``market_validation``). scanner-v0 journals stay frozen.
STRATEGY_VERSION = "scanner-v0.1"
# Journaled on every MODEL_ENTRY. v1 never loosens a protective stop; TradeManager's milestones can (see report).
EXECUTION_POLICY = "research-exec-v1-monotonic-stop"
# ponytail: TradeManager's frozen 3-lot baseline; its ADX-based 4-lot upgrade needs the legacy RiskAgent file.
MODEL_LOTS = 3
HEARTBEAT_SECONDS = 10.0
LOOKOUT_MULTIPLIER = 4.0
INTRADAY_CUTOFF = "15:10"
HOP = "LIQUIDITY_HOP_PROMOTED"
ARM_ENV = "GENESIS_TELEGRAM_ARMED"  # telegram_transport.ARM_ENV; this module must not import the publisher
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
    delta_evidence: dict[str, DeltaEvidence] = field(default_factory=dict)
    rejections: dict[str, list[str]] = field(default_factory=dict)  # timeframe -> why its detector saw no bars


def _now() -> datetime:
    return datetime.now(IST)


class ResearchRuntime:
    def __init__(self, journal_path: Path | str, feed: Callable[[datetime], MarketView], *, settings: Settings,
                 clock: Callable[[], datetime] = _now, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 trusted_delta_sources: frozenset[str] = frozenset(),
                 state_contract: StateAuthorityContract | None = None,
                 deployment_id: str | None = None, synthetic_research: bool = False,
                 account_id: str | None = None, authorize_synthetic_init: bool = False) -> None:
        if not 0 < heartbeat_seconds * 3 <= cs.LEASE_SECONDS:
            raise ValueError("heartbeat must renew at least three times per lease")
        self.state_contract, self.deployment_id = state_contract, deployment_id
        _check_state_binding(state_contract, deployment_id, journal_path, settings)
        self.synthetic_research = synthetic_research
        self.account_id = account_id
        self.authorize_synthetic_init = authorize_synthetic_init
        if synthetic_research:
            from app.synthetic_research_state import binding
            binding(state_contract, deployment_id, account_id, journal_path, settings)
        self.path = Path(journal_path) if synthetic_research else Path(journal_path).expanduser().resolve()
        self.feed, self.settings, self.clock = feed, settings, clock
        self.trusted_delta_sources = frozenset(trusted_delta_sources)
        self.heartbeat_seconds = heartbeat_seconds
        self.paper = settings.paper_capital_inr is not None
        self.lease: cj.Lease | None = None
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._beater: threading.Thread | None = None
        self.exit_agent = ExitAgent(intraday_cutoff_time=INTRADAY_CUTOFF)

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> dict:
        """Lease, recover, start the heartbeat. Raises LeaseHeld, ConsistencyError or StartupRefused."""
        _check_state_binding(self.state_contract, self.deployment_id, self.path, self.settings)
        if self.synthetic_research:
            from app.synthetic_research_state import binding, validate, initialize
            identity, root = binding(self.state_contract, self.deployment_id, self.account_id,
                                     self.path, self.settings)
            missing = validate(self.path, root, identity, self.authorize_synthetic_init)
        else:
            _check_journal_path(self.path)
        _check_calendar(self.clock(), self.settings)
        if self.synthetic_research:
            if missing:
                initialize(self.path, root, identity)
        else:
            cj.migrate(self.path)
        self.lease = cs.acquire(self.path, now=self.clock(), code_rev=code_rev(), provenance={
            "strategy_version": STRATEGY_VERSION, "registry_hash": reg.REGISTRY_HASH,
            "execution_policy": EXECUTION_POLICY, "data_sources": getattr(self.feed, "sources", None)})
        try:
            report = cs.recover(self.path, self.lease, now=self.clock())
            if self.paper:
                pl.ensure_accounts(self.path, lease=self.lease, capital=self.settings.paper_capital_inr,
                                   now=self.clock())
                report["paper"] = pl.recover(self.path, lease=self.lease, now=self.clock())
                report["paper_notifications"] = self._notifications(deliver=False)
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

    def stop(self, reason: str = "SHUTDOWN") -> dict:
        """Release the lease. Paper exits still unresolved are reported, never priced, closed or released."""
        self._stop.set()
        if self._beater is not None:
            self._beater.join()
        report = self._pending_at_stop() if self.paper and self.lease is not None else {}
        if self.lease is not None and not self.lost.is_set():
            self._release(reason)
        return report

    def _pending_at_stop(self) -> dict:
        now, window = self.clock(), self.settings.paper_exit_requote_minutes
        try:
            conn = cj.connect_readonly(self.path)
            try:
                pending = [p for p in pl.pending_exits(conn) if p["market_day"] == self.lease.market_day]
            finally:
                conn.close()
        except (sqlite3.Error, cj.JournalError) as e:
            logger.error("paper position state unknown at stop: %s", e)
            return {"paper_state": "UNKNOWN", "error": str(e)}
        for p in pending:
            deadline = datetime.fromisoformat(p["model_exit_at"]) + timedelta(minutes=window)
            p["status"] = "STOPPED_BEFORE_RETRY_DEADLINE" if window and now < deadline else "UNRESOLVED_AT_STOP"
            p["retry_deadline"] = deadline.isoformat() if window else None
            logger.warning("paper exit %s %s: %d lots held, worst case still reserved; restart records it unresolved",
                           p["paper_trade_id"], p["status"], p["lots_open"])
        return {"paper_pending_exits": pending}

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

    def _notifications(self, *, deliver: bool = True) -> list[str]:
        chat_id = pn.CHAT_ID
        if self.settings.telegram_delivery_enabled:
            chat_id = self.settings.signals_telegram_chat_id or self.settings.alert_telegram_chat_id or chat_id
        new = pn.sync(self.path, lease=self.lease, now=self.clock(), chat_id=chat_id,
                      closed=holidays(self.settings))
        if deliver:
            paper_delivery.dispatch(self.path, lease=self.lease, settings=self.settings, clock=self.clock)
        return new

    def tick(self) -> dict:
        self._check()
        now = self.clock()
        if self.paper:
            paper_delivery.dispatch(self.path, lease=self.lease, settings=self.settings, clock=self.clock)
        try:
            view = self.feed(now)
        except Exception as e:  # noqa: BLE001 - a market-data failure skips the tick; journal errors still stop it
            if self.paper:
                self._notifications()
            logger.warning("market data unavailable: %s", e)
            return {"skipped": "FEED_ERROR"}
        if view.df_5m is None or view.df_5m.empty:
            if self.paper:
                self._notifications()
            return {"skipped": "NO_CLOSED_BAR"}
        if self.paper and reg.RISK_POLICY["risk_policy_id"] == "paper-risk-v2":
            validated = _closed_bars(view.df_5m, now)
            if validated.empty:
                self._notifications()
                return {"skipped": "INVALID_OR_STALE_CANDLES"}
            view = replace(view, df_5m=validated)
        result = {"exits": self._monitor(view, now)}
        if self.paper and (retried := pl.retry_exits(self.path, lease=self.lease, chain=view.chain,
                                                     index_spot=float(view.df_5m.iloc[-1]["close"]),
                                                     now=self.clock(), settings=self.settings)):
            result["paper_exit_retries"] = retried
        if view.rejections:
            result["rejected_timeframes"] = view.rejections
        if view.rejections.get("5m"):
            # Exits still run on the closed 5m bars; the gap or missing levels behind them is reported, not hidden.
            result["monitoring_data"] = view.rejections["5m"]
            if result["exits"]:
                logger.warning("exits monitored on insufficient data: %s", view.rejections["5m"])
        is_open, why = market_status(now, self.settings)
        result["admissions"] = self._admit(view, now) if is_open else []
        if not is_open:
            result["market_closed"] = why
        if self.paper:
            result["notifications"] = self._notifications()
        return result

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
            self._paper(pl.partial, tid, view, cmp, HOP)
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
            self._paper(pl.partial, tid, view, cmp, HOP)
            return {"trade_id": tid, "action": HOP, "lots_after": decision.remaining_lots}
        if decision.is_fully_closed:
            cj.exit_trade(self.path, **write, expected_version=version, reason=decision.action,
                          index_levels={"spot": cmp, "stop": state["stop_loss"], "target": state["target_price"]},
                          model_points=decision.realized_points, model_r=decision.realized_r, now=self.clock())
            self._paper(pl.close, tid, view, cmp, decision.action)
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
        if clock >= INTRADAY_CUTOFF:
            return []
        out = []
        for setup in sorted(view.setups, key=reg.order_key):  # the shared paper account is first come, first served
            if not getattr(setup, "is_valid_for_trade_crew", False):
                continue
            candle = _ist_candle(setup.timestamp)
            if candle and candle[:10] != self.lease.market_day:
                # The lookout window compares HH:MM only, so a prior day's candle would otherwise pass it.
                out.append({"strategy": setup.strategy, "recorded": False, "reasons": ["SIGNAL_OFF_SESSION"]})
                continue
            window = DynamicLookoutWindow(setup_name=setup.strategy, base_tf=str(setup.timeframe),
                                          multiplier=LOOKOUT_MULTIPLIER, detected_time_str=str(setup.timestamp))
            if not window.is_valid_window(clock):
                continue
            self._check()
            out.append(self._admit_one(setup, view))
        return out

    def _admit_one(self, setup, view: MarketView) -> dict:
        # Recheck at the mutation boundary: monitoring or earlier candidates may cross the cutoff.
        if self.clock().astimezone(IST).strftime("%H:%M") >= INTRADAY_CUTOFF:
            return {"strategy": setup.strategy, "recorded": False, "reasons": ["ENTRY_AFTER_CUTOFF"]}
        candidate = adm.SignalCandidate(
            market_day=self.lease.market_day, instrument=setup.symbol, strategy_id=setup.strategy,
            strategy_version=STRATEGY_VERSION, signal_candle=_ist_candle(setup.timestamp),
            direction=setup.direction, setup_id=f"{setup.timeframe}|{setup.trigger_price}",
        )
        quote = setup_quote(view.chain, setup)
        admission = adm.evaluate(candidate, quote, now=self.clock(), settings=self.settings)
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
        entry_now = self.clock()
        if entry_now.astimezone(IST).strftime("%H:%M") >= INTRADAY_CUTOFF:
            return {**result, "trade": "ENTRY_AFTER_CUTOFF"}
        opened = adm.open_research_trade(
            self.path, persisted, lease=self.lease, model_lots=MODEL_LOTS, entry_spot=float(setup.trigger_price),
            stop_spot=float(setup.tentative_stop), target_spot=float(setup.tentative_target),
            execution_policy=EXECUTION_POLICY, now=entry_now,
        )
        if self.paper and not opened["replayed"]:
            result["paper"] = pl.enter(self.path, lease=self.lease, admission=stored, model_trade=opened["trade"],
                                       timeframe=str(setup.timeframe), quote=quote, df_5m=view.df_5m,
                                       now=self.clock(), settings=self.settings, cutoff=INTRADAY_CUTOFF,
                                       delta_evidence=view.delta_evidence.get(quote.tradingsymbol) if quote else None,
                                       trusted_delta_sources=self.trusted_delta_sources)
        return {**result, "trade": opened["trade"]["trade_id"]}

    def _paper(self, step, trade_id: str, view: MarketView, cmp: float, reason: str) -> None:
        if self.paper:
            step(self.path, lease=self.lease, model_trade_id=trade_id, chain=view.chain, index_spot=cmp,
                 reason=reason, now=self.clock(), settings=self.settings)


# --- helpers -------------------------------------------------------------------------------------------

def _check_state_binding(contract, deployment_id, journal_path, settings) -> None:
    """Opt-in declaration check; no discovery, resource reads or provisioning."""
    if contract is None:
        if deployment_id is not None:
            raise AuthorityError("Deployment selection requires a complete contract")
        return
    preflight(contract)
    if any(d.environment == Environment.LIVE for d in contract.deployments):
        raise AuthorityError("Live deployments are outside this integration")
    selected = [d for d in contract.deployments if d.deployment_id == deployment_id]
    if len(selected) != 1:
        raise AuthorityError("Select one explicit deployment")
    deployment = selected[0]
    expected = Environment.PAPER if settings.paper_capital_inr is not None else Environment.RESEARCH
    if deployment.environment != expected:
        raise AuthorityError("Deployment environment conflicts with runtime capital mode")
    if settings.live_trading_enabled or settings.telegram_delivery_enabled:
        raise AuthorityError("State-bound runtime requires live trading and delivery disabled")
    # Existing repository state has no approved environment ownership.
    for d in contract.deployments:
        root = Path(d.permitted_root)
        if root.is_relative_to(_REPO) or _REPO.is_relative_to(root):
            raise AuthorityError("Repository state cannot be assigned by this integration")
    location = next(a.location for a in deployment.authorities
                    if a.resource == Resource.CREW_RESEARCH_JOURNAL)
    if str(journal_path) != location:
        raise AuthorityError("Explicit journal path conflicts with its authority")


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


def _check_calendar(now: datetime, settings: Settings) -> None:
    """A year missing from the holiday file would make every holiday a trading day."""
    year = str(now.astimezone(IST).year)
    if not any(day.startswith(year) for day in holidays(settings)):
        raise StartupRefused(f"CALENDAR_MISSING: the NSE holiday file has no dates for {year}")


def setup_quote(chain: dict | None, setup) -> ContractQuote | None:
    """The chain quote for the scanner's own suggested strike. None (QUOTE_MISSING) when it is absent."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(CE|PE)\s*$", str(getattr(setup, "suggested_strike", "") or "").upper())
    if not chain or not match:
        return None
    underlying = getattr(setup, "symbol", None)
    if not underlying:
        return None
    return px.contract_quote(chain, strike=float(match.group(1)), option_type=match.group(2),
                             expiry=chain.get("expiry"), underlying=underlying)


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
    """Index-point and R totals per strategy_id over CLOSED STRATEGY_VERSION model trades of one execution policy.

    The scanner card spans several scanner strategies, so there is no combined total: each strategy_id is its own
    group. No premium P&L. Open, orphaned, expired, quarantined, other-strategy-version and other-policy trades,
    and admissions with no model entry, are listed and never counted. There is no reconciliation path yet.
    """
    conn = cj.connect_readonly(path)
    try:
        trades = [dict(r) for r in conn.execute(
            "SELECT trade_id, state, strategy FROM crew_trades ORDER BY trade_id")]
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
            finalized.append({"trade_id": tid, "strategy_id": trade["strategy"],
                              "model_points": exit_["model_points"], "model_r": exit_["model_r"]})
    groups = {}
    for t in finalized:
        groups.setdefault(t["strategy_id"], []).append(t)
    return {
        "strategy_version": STRATEGY_VERSION, "execution_policy": execution_policy,
        "eligibility": "CLOSED with model_points and model_r; MODEL_ENTRY strategy_version and execution_policy match",
        "units": "index points and R only",
        "groups": {sid: {"trades": len(rows), "model_points": round(sum(t["model_points"] for t in rows), 2),
                         "model_r": round(sum(t["model_r"] for t in rows), 2), "finalized": rows}
                   for sid, rows in sorted(groups.items())},
        "excluded": excluded, "missing_model_entries": missing_model_entries(path),
    }


def _has_trade(path: Path, signal_id: str) -> bool:
    conn = cj.connect_readonly(path)
    try:
        return conn.execute("SELECT 1 FROM crew_trades WHERE signal_id=?", (signal_id,)).fetchone() is not None
    finally:
        conn.close()


def scanner_feed(scanner=None, chain_source: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] = _now) -> Callable[[datetime], MarketView]:
    """The live scanner's market step (closed bars of this session on every timeframe, Groww NIFTY chain,
    ScannerManager snapshot), read-only."""
    custom = scanner is not None or chain_source is not None
    if scanner is None:
        from app.trading.scanner_crew.scanner_manager import ScannerManager
        scanner = ScannerManager()
    if chain_source is None:
        def chain_source():
            from app.brokers.groww import GrowwQuotes
            return GrowwQuotes()._page("NIFTY")

    def feed(now: datetime) -> MarketView:
        detector = getattr(scanner, "detector_agent", None)
        if getattr(detector, "otm_steps", None) != reg.STRATEGY_PARAMETERS["otm_steps"]:
            raise ValueError("scanner parameters do not match registered strategy")
        page = chain_source() or {}
        fetched_at = clock()
        oc = page.get("optionChain") or {}
        chain = {"contracts": oc.get("optionContracts") or [],
                 "expiry": (oc.get("aggregatedDetails") or {}).get("currentExpiry"), "fetched_at": fetched_at}
        timeframes = list(reg.STRATEGY_PARAMETERS["timeframes"])
        # A whole 1m session plus its forming bar; the 50-bar default loses 09:15 (OR-30, day open) by 13:25 on 5m.
        raw = {tf: scanner.feed_agent.fetch_timeframe_candles(tf, limit_bars=400) for tf in timeframes}
        df = _closed_bars(raw["5m"].tail(50), now)  # earlier sessions kept for the regime label only
        closed = holidays(get_settings())
        frames, rejections = {}, {}
        for tf in timeframes:
            frames[tf], why = mv.session_frame(raw[tf], now, int(tf[:-1]), closed)
            if why:
                rejections[tf] = [why]
        daily = scanner.feed_agent.fetch_timeframe_candles("1d")
        problem = mv.previous_session_problem(daily, now, closed)
        if problem:
            # PDH/PDL feed every timeframe's levels; exits keep the 5m bars but get no key levels to hop to.
            logger.warning("%s: %s", mv.PREVIOUS_SESSION_LEVELS_MISSING, problem)
            return MarketView(df_5m=df, setups=[], chain=chain, key_levels=[], rejections={
                tf: [*rejections.get(tf, []), mv.PREVIOUS_SESSION_LEVELS_MISSING] for tf in timeframes})
        snap = scanner.produce_scan_snapshot(frames=frames, df_daily=daily,
                                             session_date=now.astimezone(IST).date().isoformat(),
                                             current_time_str=now.astimezone(IST).strftime("%H:%M"),
                                             timeframes=timeframes,
                                             strategy_filter=reg.STRATEGY_PARAMETERS["strategy_filter"],
                                             option_contracts=chain["contracts"] or None)
        return MarketView(df_5m=df, setups=list(snap.actionable_setups), chain=chain,
                          key_levels=list(snap.levels.all_key_levels), rejections=rejections)

    if custom:
        feed.sources = None
    else:
        feed.sources = {"option_chain": "groww_web:groww.in/options/nifty", "index_bars": "yahoo_chart:5m",
                        "delta": None}
    return feed


def code_rev(repo: Path = _REPO) -> str | None:
    """HEAD, with ``+dirty`` when any tracked file differs from it, as the observation store records it. None when
    git cannot say."""
    import subprocess

    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{head}+dirty" if dirty else head or None


def signal_only_settings(settings: Settings, journal: Path, environ=os.environ) -> Settings:
    """Supervised observation: refuse live flags, an armed Telegram or a journal inside the repo; never deliver.

    Paper notifications stay as local outbox rows to the mock chat. No delta source is trusted, so every priced
    paper admission is vetoed STOP_RISK_UNVERIFIABLE and no paper fill is created.
    """
    if settings.live_trading_enabled or settings.sandbox_allow_live:
        raise StartupRefused("signal-only refuses LIVE_TRADING_ENABLED or SANDBOX_ALLOW_LIVE")
    if environ.get(ARM_ENV):
        raise StartupRefused(f"signal-only refuses {ARM_ENV}")
    if Path(journal).expanduser().resolve().is_relative_to(_REPO):
        raise StartupRefused("signal-only journals must be outside the repository")
    return settings.model_copy(update={"telegram_delivery_enabled": False})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1 research-only scanner runtime (no Telegram, no broker)")
    parser.add_argument("--journal", required=True, type=Path, help="crew research journal (new or crew-only)")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="supervised observation: no Telegram delivery, journal outside the repository")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = get_settings()
    try:
        if args.signal_only:
            settings = signal_only_settings(settings, args.journal)
        runtime = ResearchRuntime(args.journal, scanner_feed(), settings=settings)
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
