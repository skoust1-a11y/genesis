"""RiskAgent: Institutional Sizing, Safety Wall Defense & Loss Circuit Breakers.

Responsibilities:
1. Enforces the 4-Gate Scale-Up Law & Safety Wall defined in app.trading.scaling_law.
2. Daily Loss Circuit Breaker: Enforces hard daily loss cap (e.g., 2 consecutive losses
   or max daily drawdown) to prevent tilt/revenge trading.
3. Position Sizing: Dynamically sizes lots based on available trading capital, Safety Wall
   reserves, and per-trade stop-loss risk.
4. Risk Cap Enforcement: Rejects any setup where stop-loss distance exceeds 55 points,
   enforcing tight 25-35 point micro-wick execution.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import threading
import uuid
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# No method name is trusted until a real option premium is booked.
PREMIUM_PNL_METHODS: frozenset[str] = frozenset()


from app.trading.scaling_law import LOT_CAPITAL_MILESTONES


logger = logging.getLogger(__name__)

# flock is per-process on macOS, so it does not stop a second thread. The RLock does.
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def risk_lock_path(config_path: Path) -> Path:
    """Lock file beside the ledger. with_suffix('.lock') would drop '.json'."""
    return config_path.parent / (config_path.name + ".lock")


def _process_lock(config_path: Path) -> threading.RLock:
    key = str(config_path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _iso_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_risk_document(data: Any) -> dict[str, Any] | None:
    """Return a validated document, or None when the bytes must not be trusted."""
    if not isinstance(data, dict):
        return None
    parsed: dict[str, Any] = {}
    if "base_capital" in data:
        number = _finite_number(data["base_capital"])
        if number is None:
            return None
        parsed["base_capital"] = number
    if "safety_wall_reserve" in data:
        number = _finite_number(data["safety_wall_reserve"])
        if number is None:
            return None
        parsed["safety_wall_reserve"] = number
    if "daily_losses_count" in data:
        count = data["daily_losses_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        parsed["daily_losses_count"] = count
    else:
        parsed["daily_losses_count"] = 0
    if "daily_cumulative_r" in data:
        number = _finite_number(data["daily_cumulative_r"])
        if number is None:
            return None
        parsed["daily_cumulative_r"] = number
    else:
        parsed["daily_cumulative_r"] = 0.0
    if "is_safety_wall_locked" in data:
        if not isinstance(data["is_safety_wall_locked"], bool):
            return None
        parsed["is_safety_wall_locked"] = data["is_safety_wall_locked"]
    else:
        parsed["is_safety_wall_locked"] = False
    if "manual_lock" in data:
        if not isinstance(data["manual_lock"], bool):
            return None
        parsed["manual_lock"] = data["manual_lock"]
    if "session_date" in data and data["session_date"] is not None:
        if not _iso_date(data["session_date"]):
            return None
        parsed["session_date"] = data["session_date"]
    else:
        parsed["session_date"] = None
    if data.get("reserve_basis"):
        if not isinstance(data["reserve_basis"], str):
            return None
        parsed["reserve_basis"] = data["reserve_basis"]
    if "default_lots" in data:
        lots = data["default_lots"]
        if isinstance(lots, bool) or not isinstance(lots, int) or lots < 0:
            return None
        parsed["default_lots"] = lots
    for key in ("loss_counted_trade_ids", "reserve_posted_for"):
        if key not in data:
            continue
        items = data[key]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            return None
        parsed[key] = list(items)
    return parsed


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class RiskApproval:
    approved: bool
    lots: int
    quantity: int
    risk_points: float
    max_loss_rupees: float
    safety_wall_balance: float
    rejection_reason: str | None = None


class RiskAgent:
    """Institutional Risk Controller & Sizing Engine."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        max_daily_losses: int = 2,
        max_stop_points: float = 55.0,
        lot_qty: int = 65,
        default_lots: int | None = None,
        max_super_lots: int = 4,
        config_path: Path | None = None,
        order_flow_agent: Any | None = None,
        frozen_baseline: bool = True,
    ) -> None:
        self.base_capital = base_capital
        self.max_daily_losses = max_daily_losses
        self.max_stop_points = max_stop_points
        self.lot_qty = lot_qty
        self.default_lots = default_lots
        self.max_super_lots = max_super_lots
        self.config_path = config_path or Path("trades/daily_risk_config.json")
        self.order_flow_agent = order_flow_agent
        self.frozen_baseline = frozen_baseline
        self.safety_wall_reserve = 0.0
        self.daily_losses_count = 0
        self.daily_cumulative_r = 0.0
        self.is_safety_wall_locked = False
        self.session_date: str | None = None
        self.session_blocked = False
        self.manual_lock = False
        self._persist_manual_lock = False
        self.reserve_basis = "index_points_x_0.5_frozen"
        self.config_unreadable = False
        self.directory_durability_unconfirmed = False
        self._loss_counted_trade_ids: list[str] | None = None
        self._reserve_posted_for: list[str] | None = None
        self._lock_fd: int | None = None
        self._lock_depth = 0
        self._hold_adopted = False
        self._adopted_bytes: bytes | None = None
        self._adopted_existed = False
        self._stale_refused = False
        self._load_config()

    def _assign_parsed(self, parsed: dict[str, Any]) -> None:
        if "base_capital" in parsed:
            self.base_capital = parsed["base_capital"]
        if "safety_wall_reserve" in parsed:
            self.safety_wall_reserve = parsed["safety_wall_reserve"]
        self.daily_losses_count = parsed["daily_losses_count"]
        self.daily_cumulative_r = parsed["daily_cumulative_r"]
        self.is_safety_wall_locked = parsed["is_safety_wall_locked"]
        self.session_date = parsed["session_date"]
        if "manual_lock" in parsed:
            self.manual_lock = parsed["manual_lock"]
            self._persist_manual_lock = True
        else:
            self.manual_lock = False
            self._persist_manual_lock = False
        if "reserve_basis" in parsed:
            self.reserve_basis = parsed["reserve_basis"]
        if "default_lots" in parsed and self.default_lots is None:
            self.default_lots = parsed["default_lots"]
        self._loss_counted_trade_ids = parsed.get("loss_counted_trade_ids")
        self._reserve_posted_for = parsed.get("reserve_posted_for")

    def _snapshot(self) -> dict[str, Any]:
        return {
            "base_capital": self.base_capital,
            "safety_wall_reserve": self.safety_wall_reserve,
            "daily_losses_count": self.daily_losses_count,
            "daily_cumulative_r": self.daily_cumulative_r,
            "is_safety_wall_locked": self.is_safety_wall_locked,
            "session_date": self.session_date,
            "manual_lock": self.manual_lock,
            "persist_manual_lock": self._persist_manual_lock,
            "reserve_basis": self.reserve_basis,
            "default_lots": self.default_lots,
            "loss_counted_trade_ids": (
                None if self._loss_counted_trade_ids is None else list(self._loss_counted_trade_ids)
            ),
            "reserve_posted_for": (
                None if self._reserve_posted_for is None else list(self._reserve_posted_for)
            ),
        }

    def _restore(self, snap: dict[str, Any]) -> None:
        self.base_capital = snap["base_capital"]
        self.safety_wall_reserve = snap["safety_wall_reserve"]
        self.daily_losses_count = snap["daily_losses_count"]
        self.daily_cumulative_r = snap["daily_cumulative_r"]
        self.is_safety_wall_locked = snap["is_safety_wall_locked"]
        self.session_date = snap["session_date"]
        self.manual_lock = snap["manual_lock"]
        self._persist_manual_lock = snap["persist_manual_lock"]
        self.reserve_basis = snap["reserve_basis"]
        self.default_lots = snap["default_lots"]
        copied_losses = snap["loss_counted_trade_ids"]
        copied_reserve = snap["reserve_posted_for"]
        self._loss_counted_trade_ids = None if copied_losses is None else list(copied_losses)
        self._reserve_posted_for = None if copied_reserve is None else list(copied_reserve)

    def _load_config(self) -> None:
        """Load persistent risk configuration and safety wall state."""
        if not self.config_path.exists():
            return
        if not self.config_path.is_file():
            self.config_unreadable = True
            self.session_blocked = True
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            self.config_unreadable = True
            self.session_blocked = True
            return
        parsed = _parse_risk_document(data)
        if parsed is None:
            self.config_unreadable = True
            self.session_blocked = True
            return
        self._assign_parsed(parsed)

    def _acquire_lock(self) -> bool:
        thread_lock = _process_lock(self.config_path)
        thread_lock.acquire()
        try:
            if self._lock_depth == 0:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(risk_lock_path(self.config_path), os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError:
                    os.close(fd)
                    raise
                self._lock_fd = fd
            self._lock_depth += 1
            return True
        except OSError:
            thread_lock.release()
            return False

    def _release_lock(self) -> None:
        thread_lock = _process_lock(self.config_path)
        try:
            self._lock_depth -= 1
            if self._lock_depth <= 0:
                self._lock_depth = 0
                self._hold_adopted = False
                self._adopted_bytes = None
                fd = self._lock_fd
                self._lock_fd = None
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
        finally:
            thread_lock.release()

    def _adopt_locked(self) -> bool:
        """Read the ledger under the lock. A missing file keeps the in-memory reserve."""
        if not self.config_path.exists():
            self._adopted_bytes = None
            self._hold_adopted = True
            self._adopted_existed = False
            return True
        if not self.config_path.is_file():
            self.config_unreadable = True
            self.session_blocked = True
            return False
        try:
            raw = self.config_path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self.config_unreadable = True
            self.session_blocked = True
            return False
        parsed = _parse_risk_document(data)
        if parsed is None:
            self.config_unreadable = True
            self.session_blocked = True
            return False
        self._assign_parsed(parsed)
        self._adopted_bytes = raw
        self._hold_adopted = True
        self._adopted_existed = True
        return True

    def _refuse_stale(self, current: bytes | None) -> bool:
        """Load the bytes now on disk and do not replace them."""
        if current is None:
            self._stale_refused = True
            return False
        try:
            data = json.loads(current.decode("utf-8"))
        except Exception:
            self.config_unreadable = True
            self.session_blocked = True
            return False
        parsed = _parse_risk_document(data)
        if parsed is None:
            self.config_unreadable = True
            self.session_blocked = True
            return False
        self._assign_parsed(parsed)
        self._adopted_bytes = current
        self._hold_adopted = True
        self._adopted_existed = True
        self._stale_refused = True
        return False

    def _sweep_temps(self) -> None:
        parent = self.config_path.parent
        prefix = f".{self.config_path.name}."
        if not parent.is_dir():
            return
        for child in parent.iterdir():
            if child.name.startswith(prefix) and child.name.endswith(".tmp"):
                try:
                    child.unlink()
                except OSError:
                    pass

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "base_capital": self.base_capital,
            "safety_wall_reserve": self.safety_wall_reserve,
            "effective_capital": self.base_capital + self.safety_wall_reserve,
            "tomorrow_approved_lots": self.max_super_lots if self.default_lots is not None else 2,
            "default_lots": self.default_lots or 3,
            "daily_losses_count": self.daily_losses_count,
            "daily_cumulative_r": round(self.daily_cumulative_r, 2),
            "is_safety_wall_locked": self.is_safety_wall_locked,
            "session_date": self.session_date,
            "reserve_basis": self.reserve_basis,
        }
        if self._persist_manual_lock:
            payload["manual_lock"] = self.manual_lock
        if self._loss_counted_trade_ids is not None:
            payload["loss_counted_trade_ids"] = list(self._loss_counted_trade_ids)
        if self._reserve_posted_for is not None:
            payload["reserve_posted_for"] = list(self._reserve_posted_for)
        return payload

    def _save_under_lock(self) -> bool:
        self._stale_refused = False
        if self.config_unreadable or self.directory_durability_unconfirmed:
            return False
        try:
            current = self.config_path.read_bytes() if self.config_path.is_file() else None
        except OSError:
            self.config_unreadable = True
            self.session_blocked = True
            return False
        if self._hold_adopted:
            if current != self._adopted_bytes:
                return self._refuse_stale(current)
        elif current is not None:
            return self._refuse_stale(current)

        tmp = self.config_path.parent / (
            f".{self.config_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            self._sweep_temps()
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._payload(), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.config_path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            _fsync_directory(self.config_path.parent)
        except OSError:
            logger.error("directory fsync failed after replace for %s", self.config_path)
            self.directory_durability_unconfirmed = True
            return False
        try:
            self._adopted_bytes = self.config_path.read_bytes()
            self._hold_adopted = True
            self._adopted_existed = True
        except OSError:
            pass
        return True

    def save_config(self) -> bool:
        """Replace the ledger. True only after replace and directory fsync both return."""
        if self.config_unreadable or self.directory_durability_unconfirmed:
            return False
        if not self._acquire_lock():
            return False
        try:
            return self._save_under_lock()
        finally:
            self._release_lock()

    def _persist_mutation(
        self,
        *,
        file_existed: bool,
        adopted: dict[str, Any],
        pre_call: dict[str, Any],
    ) -> bool:
        """Commit only when save_config is True. Do not roll back a finished replace."""
        self._stale_refused = False
        saved: Any = None
        try:
            saved = self.save_config()
        except Exception:
            saved = None
        if saved is True:
            self.directory_durability_unconfirmed = False
            self.session_blocked = False
            return True
        if self.directory_durability_unconfirmed:
            self.session_blocked = True
            return False
        if not self._stale_refused:
            self._restore(adopted if file_existed else pre_call)
        self.session_blocked = True
        return False

    def record_trade_outcome(
        self,
        is_win: bool,
        pnl_rupees: float | None = None,
        realized_r: float = 0.0,
        pnl_method: str | None = None,
    ) -> bool:
        """Update daily R counters. The reserve moves only on a priced option premium."""
        if self.config_unreadable or self.directory_durability_unconfirmed:
            self.session_blocked = True
            return False
        if not self._acquire_lock():
            self.session_blocked = True
            return False
        try:
            pre_call = self._snapshot()
            if not self._adopt_locked():
                self.session_blocked = True
                return False
            adopted = self._snapshot()
            file_existed = self._adopted_existed
            priced = pnl_method in PREMIUM_PNL_METHODS and pnl_rupees is not None
            if is_win and priced and pnl_rupees > 0:
                self.safety_wall_reserve += round(pnl_rupees * 0.50, 2)

            if is_win or realized_r > 0:
                self.daily_losses_count = 0
            elif realized_r <= -0.5 or (priced and pnl_rupees is not None and pnl_rupees <= -500.0):
                self.daily_losses_count += 1

            self.daily_cumulative_r += realized_r

            # Daily Circuit Breaker: Halt if 2 consecutive losses or cumulative drawdown <= -2.0R
            if self.daily_losses_count >= self.max_daily_losses or self.daily_cumulative_r <= -2.0:
                self.is_safety_wall_locked = True

            return self._persist_mutation(
                file_existed=file_existed,
                adopted=adopted,
                pre_call=pre_call,
            )
        finally:
            self._release_lock()

    def begin_session(self, today: date) -> bool:
        """Reset daily counters when the Asia/Kolkata date changes. A failed save blocks entries."""
        if self.config_unreadable or self.directory_durability_unconfirmed:
            self.session_blocked = True
            return False
        key = today.isoformat()
        if self.session_date == key:
            if self.session_blocked:
                return False
            return True
        if not self._acquire_lock():
            self.session_blocked = True
            return False
        try:
            pre_call = self._snapshot()
            if not self._adopt_locked():
                self.session_blocked = True
                return False
            if self.session_date == key:
                if self.directory_durability_unconfirmed:
                    self.session_blocked = True
                    return False
                self.session_blocked = False
                return True
            adopted = self._snapshot()
            file_existed = self._adopted_existed
            self.daily_losses_count = 0
            self.daily_cumulative_r = 0.0
            if not self.manual_lock:
                self.is_safety_wall_locked = False
            self.session_date = key
            return self._persist_mutation(
                file_existed=file_existed,
                adopted=adopted,
                pre_call=pre_call,
            )
        finally:
            self._release_lock()

    def reset_daily_counters(self, today: date | None = None) -> bool:
        """Reset intraday loss counters at beginning of trading day."""
        warnings.warn(
            "reset_daily_counters delegates to begin_session and no longer zeros a same-day count",
            DeprecationWarning,
            stacklevel=2,
        )
        if today is None:
            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        return self.begin_session(today)

    def evaluate_order(
        self,
        entry_price: float,
        stop_loss: float,
        safety_wall_balance: float = 0.0,
        df_5m: Any | None = None,
        direction: str | None = None,
        is_super_trend: bool = False,
    ) -> RiskApproval:
        """Determine if order satisfies institutional risk rules and calculate sizing with Order Flow."""
        risk_points = abs(entry_price - stop_loss)
        eff_safety = self.safety_wall_reserve or safety_wall_balance

        # 1. Check daily circuit breaker (consecutive losses >= 2 or cumulative drawdown <= -2.0R)
        if self.session_blocked or self.manual_lock or self.is_safety_wall_locked or self.daily_losses_count >= self.max_daily_losses or self.daily_cumulative_r <= -2.0:
            if self.session_blocked:
                reason = "Session reset failed. Entries are blocked until the risk file can be saved."
            elif self.manual_lock:
                reason = "Manual lock is set. Entries are blocked."
            else:
                reason = f"Circuit Breaker Triggered: {self.daily_losses_count} losses or {self.daily_cumulative_r:.1f}R daily drawdown. Safety wall locked."
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=eff_safety,
                rejection_reason=reason,
            )

        # 2. Check risk ceiling (55-pt trap prevention)
        if risk_points > self.max_stop_points:
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=eff_safety,
                rejection_reason=f"Stop Loss {risk_points:.1f} pts exceeds institutional ceiling of {self.max_stop_points} pts.",
            )

        # 3. Lot Sizing Policy
        # Under Architecture C frozen baseline: STRICTLY FIXED at 3 Lots (standard) or 4 Lots (super-trend)
        if self.frozen_baseline and self.default_lots is not None:
            base_lots = self.max_super_lots if is_super_trend else self.default_lots
            lots = base_lots
        else:
            effective_capital = self.base_capital + eff_safety
            base_lots = 1
            for tier_lots, cap_req in sorted(LOT_CAPITAL_MILESTONES.items()):
                if effective_capital >= cap_req:
                    base_lots = tier_lots
            if self.default_lots is not None:
                base_lots = max(self.default_lots, base_lots)

            if is_super_trend:
                base_lots = min(self.max_super_lots, max(base_lots, 3) + 1)

            lots = base_lots
            if not is_super_trend:
                if risk_points > 40.0:
                    lots = min(lots, 2)
                elif risk_points <= 40.0 and self.default_lots is not None:
                    lots = max(lots, self.default_lots)

        # 4. Apply Order Flow Sizing Cap & Absorption Trap Veto
        if self.order_flow_agent is not None and df_5m is not None and direction is not None:
            of_signal = self.order_flow_agent.evaluate_sizing_cap(
                df_5m=df_5m,
                direction=direction,
                base_permitted_lots=base_lots,
            )
            if of_signal.is_absorption_trap:
                return RiskApproval(
                    approved=False,
                    lots=0,
                    quantity=0,
                    risk_points=risk_points,
                    max_loss_rupees=0.0,
                    safety_wall_balance=eff_safety,
                    rejection_reason=of_signal.rationale,
                )
            if not (self.frozen_baseline and self.default_lots is not None):
                lots = of_signal.approved_lots

        qty = lots * self.lot_qty
        approx_option_risk = risk_points * 0.5 * qty

        return RiskApproval(
            approved=True,
            lots=lots,
            quantity=qty,
            risk_points=round(risk_points, 2),
            max_loss_rupees=round(approx_option_risk, 2),
            safety_wall_balance=eff_safety,
            rejection_reason=None,
        )
