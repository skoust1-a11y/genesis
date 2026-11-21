"""Isolated shadow paper-forward account for `dual-agent-ema-st-ratchet-v2-conditional`.

Runs side-by-side inside `forward_comparison.py` on live Upstox option bid/ask quotes,
persisting state and closed trades to its own `dual_agent_shadow.db` without touching
the frozen baseline (`scanner-v0.1`) or primary candidate (`zone10-5m-1m-v1`) ledgers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any

from app.trading.dual_agent_ema_policy import (
    DualAgentArbiter,
    DualAgentConfig,
    IST,
    LotEntry,
    MarketContext,
    PendingCross,
    PositionState,
    RULE_HASH,
    VERSION,
    compute_ema,
    compute_supertrend,
    detect_confirmed_eqh_reversal,
    find_equal_high_levels,
    select_dynamic_strike,
)

log = logging.getLogger(__name__)


def _calc_upstox_fee(side: str, price: float, qty: int) -> float:
    turnover = price * qty
    brokerage = 20.0
    exchange = turnover * 0.0003503
    sebi = turnover * 0.000001
    gst = (brokerage + exchange + sebi) * 0.18
    stt = turnover * 0.001 if side == "SELL" else 0.0
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    return round(brokerage + exchange + sebi + gst + stt + stamp, 2)


def _extract_rows_1m(raw_1m: Any) -> list[dict[str, Any]]:
    if raw_1m is None:
        return []
    if hasattr(raw_1m, "empty") and raw_1m.empty:
        return []
    rows: list[dict[str, Any]] = []
    iterator = raw_1m.iterrows() if hasattr(raw_1m, "iterrows") else enumerate(raw_1m)
    for _, r in iterator:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        stamp_raw = d.get("date")
        if not stamp_raw:
            continue
        stamp = datetime.fromisoformat(str(stamp_raw))
        stamp = stamp.replace(tzinfo=IST) if stamp.utcoffset() is None else stamp.astimezone(IST)
        rows.append({
            "date": stamp.isoformat(),
            "day": stamp.date().isoformat(),
            "time": stamp.strftime("%H:%M"),
            "open": float(d["open"]),
            "high": float(d["high"]),
            "low": float(d["low"]),
            "close": float(d["close"]),
        })
    unique = {r["date"]: r for r in rows}
    return [unique[k] for k in sorted(unique)]


def _aggregate_5m(rows_1m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    g5: dict[str, dict[str, Any]] = {}
    for b in rows_1m:
        dt = datetime.fromisoformat(b["date"])
        slot = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0).isoformat()
        if slot not in g5:
            g5[slot] = {
                "date": slot, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
            }
        else:
            g5[slot]["high"] = max(g5[slot]["high"], b["high"])
            g5[slot]["low"] = min(g5[slot]["low"], b["low"])
            g5[slot]["close"] = b["close"]
    return [g5[k] for k in sorted(g5)]


def _detect_recent_sweep(recent_bars: list[dict[str, Any]], levels: list[float]) -> bool:
    if not recent_bars or not levels:
        return False
    curr_close = recent_bars[-1]["close"]
    for lvl in levels:
        if any(b["low"] < lvl for b in recent_bars) and curr_close > lvl:
            return True
        if any(b["high"] > lvl for b in recent_bars) and curr_close < lvl:
            return True
    return False


def _lookup_live_quote(chain: dict[str, Any] | None, strike: int, kind: str) -> tuple[float, float, str] | None:
    """Return (bid, ask, trading_symbol) from live Upstox chain if fresh & valid."""
    if not chain:
        return None
    contracts = chain.get("contracts") or chain.get("optionChain", {}).get("contracts") or []
    leg_key = "ce" if kind == "CE" else "pe"
    for row in contracts:
        if int(round(float(row.get("strikePrice", -1)))) != strike:
            continue
        leg = row.get(leg_key) or {}
        live = leg.get("liveData") or {}
        try:
            bid = float(live.get("bid", 0) or 0)
            ask = float(live.get("ask", 0) or 0)
        except (TypeError, ValueError):
            return None
        if bid > 0 and ask >= bid:
            symbol = str(leg.get("tradingSymbol") or f"NIFTY {strike} {kind}")
            return bid, ask, symbol
    return None


class DualAgentShadowAccount:
    """Isolated SQLite paper account running `dual-agent-ema-st-ratchet-v2-conditional`."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        capital_inr: float = 50000.0,
        config: DualAgentConfig = DualAgentConfig(),
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.capital_inr = float(capital_inr)
        self.config = config
        self.arbiter = DualAgentArbiter(config)
        self.active_day: str | None = None
        self.last_processed_1m: str | None = None
        self.pos: PositionState | None = None
        self.entry_bar_count: int = 0
        self._init_db()
        self._restore_state()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    side TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    max_lots INTEGER NOT NULL,
                    entry_spot REAL NOT NULL,
                    exit_spot REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    gross_inr REAL NOT NULL,
                    fees_inr REAL NOT NULL,
                    net_inr REAL NOT NULL,
                    thesis_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    day TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO shadow_meta(key, value) VALUES (?, ?)",
                ("cash_inr", str(self.capital_inr)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO shadow_meta(key, value) VALUES (?, ?)",
                ("policy_version", VERSION),
            )
            conn.execute(
                "INSERT OR REPLACE INTO shadow_meta(key, value) VALUES (?, ?)",
                ("rule_hash", RULE_HASH),
            )

    def _get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM shadow_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO shadow_meta(key, value) VALUES (?, ?)", (key, value))

    def _restore_state(self) -> None:
        self.active_day = self._get_meta("active_day")
        self.last_processed_1m = self._get_meta("last_processed_1m")
        raw_pos = self._get_meta("open_position")
        if raw_pos:
            try:
                d = json.loads(raw_pos)
                d["lots"] = [LotEntry(**x) for x in d["lots"]]
                self.pos = PositionState(**d)
            except Exception:  # noqa: BLE001
                self.pos = None
        for key, agent in (("ce_pending", self.arbiter.ce_agent), ("pe_pending", self.arbiter.pe_agent)):
            raw_p = self._get_meta(key)
            if raw_p:
                try:
                    agent.pending = PendingCross(**json.loads(raw_p))
                except Exception:  # noqa: BLE001
                    agent.pending = None

    def _save_pending(self) -> None:
        with self._connect() as conn:
            for key, agent in (("ce_pending", self.arbiter.ce_agent), ("pe_pending", self.arbiter.pe_agent)):
                if agent.pending is None:
                    conn.execute("DELETE FROM shadow_meta WHERE key=?", (key,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO shadow_meta(key, value) VALUES (?, ?)",
                        (key, json.dumps(agent.pending.__dict__)),
                    )

    def _save_position(self) -> None:
        if self.pos is None:
            with self._connect() as conn:
                conn.execute("DELETE FROM shadow_meta WHERE key='open_position'")
        else:
            payload = {
                **self.pos.__dict__,
                "lots": [l.__dict__ for l in self.pos.lots],
            }
            self._set_meta("open_position", json.dumps(payload))

    def _log_decision(self, at: datetime, action: str, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO shadow_decisions(at, day, action, reason) VALUES (?, ?, ?, ?)",
                (at.isoformat(), at.date().isoformat(), action, reason),
            )

    def tick(
        self,
        *,
        now: datetime,
        raw_1m: Any,
        chain: dict[str, Any] | None,
        news_safe: bool = True,
        fee_policy: str | None = None,
    ) -> dict[str, Any]:
        today_str = now.date().isoformat()
        if self.active_day != today_str:
            self.arbiter.reset_day()
            self.active_day = today_str
            self._set_meta("active_day", today_str)
            self._save_pending()

        rows = _extract_rows_1m(raw_1m)
        # Keep only completed 1m bars strictly prior to `now`
        closed_1m = [r for r in rows if datetime.fromisoformat(r["date"]) + timedelta(minutes=1) <= now]
        if len(closed_1m) < 25:
            return {"state": "WAITING_FOR_1M", "bars": len(closed_1m)}

        pending_indices = [
            i
            for i, b in enumerate(closed_1m)
            if b["day"] == today_str
            and i >= 1
            and (self.last_processed_1m is None or b["date"] > self.last_processed_1m)
        ]
        if not pending_indices:
            return {"state": "IDLE", "last_bar": self.last_processed_1m, "held": 1 if self.pos else 0}

        # Precompute strictly causal 1M EMAs and 5M indicators once across closed history
        c1 = [b["close"] for b in closed_1m]
        e9_1, e20_1 = compute_ema(c1, 9), compute_ema(c1, 20)

        all_5m = _aggregate_5m(closed_1m)
        closed_5m_all = [b for b in all_5m if datetime.fromisoformat(b["date"]) + timedelta(minutes=5) <= now]
        if closed_5m_all:
            c5 = [b["close"] for b in closed_5m_all]
            e9_5, e20_5, e200_5 = compute_ema(c5, 9), compute_ema(c5, 20), compute_ema(c5, 200)
            st_dir_ce, st_val_ce, atr5 = compute_supertrend(
                closed_5m_all, self.config.ce.supertrend_period, self.config.ce.supertrend_multiplier
            )
            st_dir_pe, st_val_pe, _ = compute_supertrend(
                closed_5m_all, self.config.pe.supertrend_period, self.config.pe.supertrend_multiplier
            )
            for idx5, b5 in enumerate(closed_5m_all):
                b5.update(
                    e9=e9_5[idx5], e20=e20_5[idx5], e200=e200_5[idx5],
                    st_dir=st_dir_ce[idx5], st_val=st_val_ce[idx5],
                    st_dir_pe=st_dir_pe[idx5], st_val_pe=st_val_pe[idx5],
                    atr=atr5[idx5],
                    _end=datetime.fromisoformat(b5["date"]) + timedelta(minutes=5),
                )

        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for b in closed_1m:
            by_day[b["day"]].append(b)
        prev_days = sorted(d for d in by_day if d < today_str)
        prev_day_levels: list[float] = []
        if prev_days:
            pb = by_day[prev_days[-1]]
            prev_day_levels = [max(x["high"] for x in pb), min(x["low"] for x in pb), pb[-1]["close"]]

        cash = float(self._get_meta("cash_inr", str(self.capital_inr)) or self.capital_inr)
        last_idx = pending_indices[-1]
        today_bars_all = by_day[today_str]
        today_pos_by_date = {b["date"]: k for k, b in enumerate(today_bars_all)}

        for idx in pending_indices:
            is_latest = idx == last_idx
            bar = closed_1m[idx]
            t = bar["time"]
            bar_end = datetime.fromisoformat(bar["date"]) + timedelta(minutes=1)
            p_e9, p_e20 = e9_1[idx - 1], e20_1[idx - 1]
            c_e9, c_e20 = e9_1[idx], e20_1[idx]

            closed_5m = [b for b in closed_5m_all if b["_end"] <= bar_end]
            prev_5m = closed_5m[-2] if len(closed_5m) >= 2 else None
            curr_5m = closed_5m[-1] if len(closed_5m) >= 2 else None
            new_5m_closed = (len(closed_5m) >= 2) and (bar_end.minute % 5 == 0)

            today_bars = today_bars_all[: today_pos_by_date[bar["date"]] + 1]
            base_levels = list(prev_day_levels)
            if t >= "09:30":
                or15 = [x for x in today_bars if x["time"] < "09:30"]
                if or15:
                    base_levels.extend([max(x["high"] for x in or15), min(x["low"] for x in or15)])
            if t >= "09:45":
                or30 = [x for x in today_bars if x["time"] < "09:45"]
                if or30:
                    base_levels.extend([max(x["high"] for x in or30), min(x["low"] for x in or30)])
            base_levels = sorted(set(base_levels))

            recent_sweep = _detect_recent_sweep(today_bars[-16:], base_levels)
            eqh_levels = find_equal_high_levels(closed_5m, curr_5m["atr"] if curr_5m else None)
            eqh_ok, sweep_hi = detect_confirmed_eqh_reversal(today_bars[-26:], eqh_levels, curr_5m)
            pe_levels = sorted(set(base_levels + ([sweep_hi] if sweep_hi is not None else [])))
            ctx_ce = MarketContext(
                time_hhmm=t,
                base_levels=base_levels,
                atr_5m=curr_5m["atr"] if curr_5m else None,
                st_dir_5m=curr_5m["st_dir"] if curr_5m else None,
                st_val_5m=curr_5m["st_val"] if curr_5m else None,
                ema20_5m=curr_5m["e20"] if curr_5m else None,
                ema200_5m=curr_5m["e200"] if curr_5m else None,
                recent_liquidity_sweep=recent_sweep,
            )
            ctx_pe = MarketContext(
                time_hhmm=t,
                base_levels=pe_levels,
                atr_5m=curr_5m["atr"] if curr_5m else None,
                st_dir_5m=curr_5m["st_dir_pe"] if curr_5m else None,
                st_val_5m=curr_5m["st_val_pe"] if curr_5m else None,
                ema20_5m=curr_5m["e20"] if curr_5m else None,
                ema200_5m=curr_5m["e200"] if curr_5m else None,
                recent_liquidity_sweep=eqh_ok,
            )

            # Manage open position across every missed bar so stops/exits are never skipped
            if self.pos is not None:
                self.entry_bar_count += 1
                kind = "CE" if self.pos.side == "CALL" else "PE"
                dec = self.arbiter.evaluate_position(
                    self.pos,
                    ctx=ctx_ce,
                    curr_1m_high=bar["high"],
                    curr_1m_low=bar["low"],
                    curr_1m_close=bar["close"],
                    mins_since_entry=self.entry_bar_count,
                    new_5m_closed=new_5m_closed,
                    prev_5m=prev_5m,
                    curr_5m=curr_5m,
                )
                q = _lookup_live_quote(chain, self.pos.strike, kind)
                if dec.action in ("EXIT_MONOTONIC_SL", "EXIT_5M_CROSS_END", "EXIT_EOD_1510"):
                    if q is None:
                        self._save_position()
                        return {"state": "EXIT_WAITING_FOR_BID", "action": dec.action}
                    bid, _, symbol = q
                    gross = self.pos.realized_gross_inr + sum(
                        (bid - l.entry_prem) * self.config.lot_size for l in self.pos.lots
                    )
                    fees = self.pos.total_fees_inr + _calc_upstox_fee(
                        "SELL", bid, len(self.pos.lots) * self.config.lot_size
                    )
                    net = round(gross - fees, 2)
                    cash = round(cash + net, 2)
                    self._set_meta("cash_inr", str(cash))
                    self.arbiter.record_trade_outcome(net)
                    with self._connect() as conn:
                        conn.execute(
                            """INSERT INTO shadow_trades(
                                day, side, contract, entry_time, exit_time, max_lots,
                                entry_spot, exit_spot, exit_reason, gross_inr, fees_inr, net_inr, thesis_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                today_str, self.pos.side, symbol, self.pos.entry_time, t,
                                1 + self.pos.adds, self.pos.entry_spot, dec.exit_spot or bar["close"],
                                dec.action, round(gross, 2), round(fees, 2), net, json.dumps(self.pos.thesis_log),
                            ),
                        )
                    self._log_decision(now, dec.action, f"{dec.reason} | net=Rs {net:+.2f}")
                    self.pos = None
                    self.entry_bar_count = 0
                elif is_latest and dec.action == "SCALE_OUT_TRIM" and len(self.pos.lots) > 1 and q is not None:
                    bid, _, _ = q
                    trimmed = self.pos.lots.pop()
                    self.pos.realized_gross_inr += (bid - trimmed.entry_prem) * self.config.lot_size
                    self.pos.total_fees_inr += _calc_upstox_fee("SELL", bid, self.config.lot_size)
                    self.pos.trims += 1
                    self._log_decision(now, dec.action, dec.reason)
                elif is_latest and dec.action == "PYRAMID_ADD" and q is not None:
                    _, ask_add, _ = q
                    fee_add = _calc_upstox_fee("BUY", ask_add, self.config.lot_size)
                    if ask_add * self.config.lot_size <= cash * 0.4:
                        self.pos.lots.append(LotEntry(entry_spot=bar["close"], entry_prem=ask_add, entered_at=t))
                        self.pos.total_fees_inr += fee_add
                        self.pos.adds += 1
                        self._log_decision(now, dec.action, dec.reason)
                self.last_processed_1m = bar["date"]
                self._set_meta("last_processed_1m", self.last_processed_1m)
                self._save_position()
                if is_latest:
                    return {"state": "MANAGING" if self.pos else "CLOSED", "action": dec.action}
                continue

            # Flat -> advance directional agents on every closed 1m bar so Bar t crosses stay armed
            ce_prop = self.arbiter.ce_agent.on_1m_close(
                ctx=ctx_ce, prev_e9=p_e9, prev_e20=p_e20, curr_close=bar["close"], curr_e9=c_e9, curr_e20=c_e20
            )
            pe_prop = self.arbiter.pe_agent.on_1m_close(
                ctx=ctx_pe, prev_e9=p_e9, prev_e20=p_e20, curr_close=bar["close"], curr_e9=c_e9, curr_e20=c_e20
            )
            self.last_processed_1m = bar["date"]
            self._set_meta("last_processed_1m", self.last_processed_1m)
            self._save_pending()

            preview = ce_prop or pe_prop
            if not is_latest:
                if preview is not None:
                    self._log_decision(
                        now,
                        "SKIPPED_STALE_CATCHUP",
                        f"{preview.side} confirmed on missed bar {t} (spot={preview.entry_spot:.2f})",
                    )
                continue

            # Latest live bar -> gate on news_safe & fee_policy before live entry
            if not news_safe or fee_policy is None:
                return {"state": "ADMISSION_BLOCKED", "news_safe": news_safe, "fee_policy": fee_policy}

            if preview is None:
                return {"state": "SCANNING", "held": 0}

            expiry = str(
                (chain or {}).get("expiry")
                or (chain or {}).get("optionChain", {}).get("aggregatedDetails", {}).get("currentExpiry")
                or ""
            )
            dte = 1
            if len(expiry) >= 10:
                try:
                    dte = max(0, (datetime.fromisoformat(expiry[:10]).date() - now.date()).days)
                except ValueError:
                    dte = 1
            if self.config.avoid_0dte and dte == 0:
                self._log_decision(now, "HOLD", "SKIP_0DTE_CHAIN")
                return {"state": "SKIPPED_0DTE", "held": 0}

            kind = "CE" if preview.side == "CALL" else "PE"

            def _quote_risk(cand_strike: int) -> tuple[float, float]:
                q_live = _lookup_live_quote(chain, cand_strike, kind)
                if q_live is None:
                    return self.config.max_trade_risk_inr + 1.0, cash + 1.0
                _, ask_c, _ = q_live
                # Approximate worst-case option drop using conservative 0.60 delta for ITM/ATM risk gate
                est_drop = preview.risk_pts * 0.60
                est_sl_bid = max(0.05, ask_c - est_drop)
                r_inr = (
                    (ask_c - est_sl_bid) * self.config.lot_size
                    + _calc_upstox_fee("BUY", ask_c, self.config.lot_size)
                    + _calc_upstox_fee("SELL", est_sl_bid, self.config.lot_size)
                )
                return r_inr, ask_c * self.config.lot_size

            sel = select_dynamic_strike(
                spot=preview.entry_spot,
                side=preview.side,
                dte=dte,
                time_hhmm=t,
                quote_and_risk_fn=_quote_risk,
                max_risk_inr=self.config.max_trade_risk_inr,
                max_premium_inr=cash * 0.4,
            )
            if sel is None:
                self._log_decision(now, "HOLD", "NO_ELIGIBLE_LIVE_STRIKE")
                return {"state": "NO_ELIGIBLE_STRIKE", "held": 0}

            est_risk, _ = _quote_risk(sel.strike)
            action, chosen, reason = self.arbiter.arbitrate_entry(
                ce_prop, pe_prop, estimated_1lot_risk_inr=est_risk
            )
            if action != "ENTER" or chosen is None:
                self._log_decision(now, action, reason)
                return {"state": action, "reason": reason}

            q_fill = _lookup_live_quote(chain, sel.strike, kind)
            if q_fill is None:
                return {"state": "MISSING_LIVE_QUOTE", "held": 0}
            _, ask_fill, symbol = q_fill
            self.pos = PositionState(
                side=chosen.side,
                strike=sel.strike,
                expiry=expiry,
                dte=dte,
                entry_time=t,
                entry_spot=chosen.entry_spot,
                initial_risk_pts=chosen.risk_pts,
                stop_spot=chosen.initial_sl,
                lots=[LotEntry(entry_spot=chosen.entry_spot, entry_prem=ask_fill, entered_at=t)],
                total_fees_inr=_calc_upstox_fee("BUY", ask_fill, self.config.lot_size),
                thesis_log=[f"[{sel.tier} {symbol}] {chosen.thesis_summary}"],
            )
            self.entry_bar_count = 0
            self._save_position()
            self._log_decision(now, "ENTER", f"{symbol} @ {ask_fill:.2f} | {sel.reason}")
            return {"state": "ENTERED", "contract": symbol, "tier": sel.tier, "entry_prem": ask_fill}

        return {"state": "IDLE", "last_bar": self.last_processed_1m, "held": 1 if self.pos else 0}

    def stats(self) -> dict[str, Any]:
        cash = float(self._get_meta("cash_inr", str(self.capital_inr)) or self.capital_inr)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*), coalesce(sum(net_inr), 0.0) FROM shadow_trades"
            ).fetchone()
            closed = int(row[0] or 0)
            net_pnl = float(row[1] or 0.0)
        return {
            "label": "Dual-Agent Shadow",
            "strategy_version": VERSION,
            "rule_hash": RULE_HASH[:12],
            "capital_inr": f"{self.capital_inr:.2f}",
            "cash_inr": f"{cash:.2f}",
            "closed_net_pnl_inr": f"{net_pnl:.2f}",
            "equity_inr": f"{cash:.2f}" if self.pos is None else None,
            "entries": closed + (1 if self.pos else 0),
            "closed": closed,
            "held": 1 if self.pos else 0,
            "unresolved": 0,
        }

    def close(self) -> None:
        self._save_position()
