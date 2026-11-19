"""Full Audited Option Backtest for Conditional Dual-Agent EMA + Supertrend + Level-Ratchet Policy.

Imports directly from `app.trading.dual_agent_ema_policy` and replays all verified 1M NIFTY sessions
from 15 Oct 2026 through 19 Nov 2026 (with Oct 1–14 warmup) using live ATR-adaptive parameters,
verified liquidity-sweep detection, Black-76 option pricing, India VIX, NSE expiry/holiday calendar,
and exact Upstox statutory fees.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    from app.trading.dual_agent_ema_policy import (
        DualAgentArbiter,
        DualAgentConfig,
        IST,
        LotEntry,
        MarketContext,
        PositionState,
        RULE_HASH,
        VERSION,
        compute_ema,
        compute_supertrend,
        detect_confirmed_eqh_reversal,
        find_equal_high_levels,
        select_dynamic_strike,
        select_non_0dte_expiry,
    )
except ModuleNotFoundError:
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "dual_agent_ema_policy",
        ROOT / "backend/app/trading/dual_agent_ema_policy.py",
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
    DualAgentArbiter = _mod.DualAgentArbiter
    DualAgentConfig = _mod.DualAgentConfig
    IST = _mod.IST
    LotEntry = _mod.LotEntry
    MarketContext = _mod.MarketContext
    PositionState = _mod.PositionState
    RULE_HASH = _mod.RULE_HASH
    VERSION = _mod.VERSION
    compute_ema = _mod.compute_ema
    compute_supertrend = _mod.compute_supertrend
    detect_confirmed_eqh_reversal = _mod.detect_confirmed_eqh_reversal
    find_equal_high_levels = _mod.find_equal_high_levels
    select_dynamic_strike = _mod.select_dynamic_strike
    select_non_0dte_expiry = _mod.select_non_0dte_expiry


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(spot: float, strike: float, years: float, sigma: float, kind: str) -> float:
    if years <= 0 or sigma <= 0:
        return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
    sq = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / sq
    d2 = d1 - sq
    if kind == "CE":
        return max(0.05, spot * norm_cdf(d1) - strike * norm_cdf(d2))
    return max(0.05, strike * norm_cdf(-d2) - spot * norm_cdf(-d1))


def tick_round(val: float, up: bool = False) -> float:
    scaled = val * 20.0
    return (math.ceil(scaled) if up else math.floor(scaled)) / 20.0


def quote_option(spot: float, strike: int, kind: str, expiry: str, now: datetime, iv: float) -> tuple[float, float]:
    years = max((datetime.fromisoformat(expiry + "T15:30:00+05:30") - now).total_seconds(), 60) / (365 * 86400)
    prem = black76(spot, strike, years, iv / 100.0, kind)
    bid = max(0.05, tick_round(prem * 0.995, False))
    ask = max(bid + 0.05, tick_round(prem * 1.005, True))
    return bid, ask


def calc_upstox_fee(side: str, price: float, qty: int) -> float:
    turnover = price * qty
    brokerage = 20.0
    exchange = turnover * 0.0003503
    sebi = turnover * 0.000001
    gst = (brokerage + exchange + sebi) * 0.18
    stt = turnover * 0.001 if side == "SELL" else 0.0
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    return brokerage + exchange + sebi + gst + stt + stamp


def load_nse_holidays() -> set[date]:
    hols: set[date] = set()
    hdir = ROOT / "logs/paper-forward/data/holidays"
    if hdir.exists():
        for p in hdir.glob("*.json"):
            data = json.loads(p.read_text())
            items = data if isinstance(data, list) else data.get("holidays", data.get("dates", []))
            for item in items:
                s = item if isinstance(item, str) else item.get("date", "")
                if len(s) >= 10:
                    try:
                        hols.add(date.fromisoformat(s[:10]))
                    except ValueError:
                        pass
    return hols


def load_1m_candles() -> tuple[list[str], dict[str, list[dict]]]:
    cov_path = ROOT / "logs/resimulation-20261117/expiry-time-v1/coverage/results.json"
    coverage = json.loads(cov_path.read_text())
    by_day: dict[str, list[dict]] = defaultdict(list)

    for relative, digest in coverage["source_hashes"].items():
        path = ROOT / relative
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        if path.suffixes[-2:] == [".json", ".gz"]:
            snap = json.loads(gzip.decompress(raw))
            for r in snap["raw_1m"]:
                if str(r["date"])[:10] == "2026-11-17":
                    stamp = datetime.fromisoformat(r["date"])
                    stamp = stamp.replace(tzinfo=IST) if stamp.utcoffset() is None else stamp.astimezone(IST)
                    if stamp + timedelta(minutes=1) <= datetime.fromisoformat(coverage["capture"]):
                        by_day["2026-11-17"].append({**r, "date": stamp.isoformat()})
        elif path.name.startswith("nifty-1m-"):
            for a in json.loads(raw)["data"]["candles"]:
                stamp = datetime.fromisoformat(a[0]).astimezone(IST)
                day = stamp.date().isoformat()
                if day != "2026-11-17" and "09:15" <= stamp.strftime("%H:%M") < "15:30":
                    by_day[day].append({
                        "date": stamp.isoformat(),
                        "open": float(a[1]), "high": float(a[2]),
                        "low": float(a[3]), "close": float(a[4]),
                    })

    ev_path = ROOT / "logs/paper-forward/runs/forward-20261116-172047/evidence/2026-11-19/151648002324-candidate.json.gz"
    if ev_path.exists():
        snap = json.loads(gzip.decompress(ev_path.read_bytes()))
        for r in snap.get("raw_1m", []):
            d_str = str(r["date"])[:10]
            if d_str in ("2026-11-18", "2026-11-19"):
                stamp = datetime.fromisoformat(r["date"])
                stamp = stamp.replace(tzinfo=IST) if stamp.utcoffset() is None else stamp.astimezone(IST)
                if "09:15" <= stamp.strftime("%H:%M") < "15:30":
                    by_day[d_str].append({
                        "date": stamp.isoformat(),
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                    })

    days = sorted(by_day.keys())
    for d_str in days:
        unique = {r["date"]: r for r in by_day[d_str]}
        by_day[d_str] = [unique[k] for k in sorted(unique)]
    return days, by_day


def aggregate_5m(rows_1m: list[dict]) -> list[dict]:
    g5 = {}
    for b in rows_1m:
        dt = datetime.fromisoformat(b["date"])
        slot = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0).isoformat()
        if slot not in g5:
            g5[slot] = {"date": slot, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
        else:
            g5[slot]["high"] = max(g5[slot]["high"], b["high"])
            g5[slot]["low"] = min(g5[slot]["low"], b["low"])
            g5[slot]["close"] = b["close"]
    return [g5[k] for k in sorted(g5.keys())]


def detect_recent_sweep(recent_bars: list[dict], levels: list[float]) -> bool:
    """True if any bar in the recent lookback swept below a marked level and reclaimed above it (or vice versa)."""
    if not recent_bars or not levels:
        return False
    curr_close = recent_bars[-1]["close"]
    for lvl in levels:
        if any(b["low"] < lvl for b in recent_bars) and curr_close > lvl:
            return True
        if any(b["high"] > lvl for b in recent_bars) and curr_close < lvl:
            return True
    return False


def main() -> None:
    days, by_day = load_1m_candles()
    hols = load_nse_holidays()
    vix_path = ROOT / "logs/resimulation-20261116/reliability-v1/vix-daily.json"
    vix_map = {r[0][:10]: float(r[4]) for r in json.loads(vix_path.read_text())["data"]["candles"]}

    all_1m = [{**b, "day": d_str, "time": b["date"][11:16]} for d_str in days for b in by_day[d_str]]
    c1 = [b["close"] for b in all_1m]
    e9_1, e20_1 = compute_ema(c1, 9), compute_ema(c1, 20)
    for i, b in enumerate(all_1m):
        b["e9"], b["e20"] = e9_1[i], e20_1[i]

    cfg = DualAgentConfig()
    arbiter = DualAgentArbiter(cfg)

    all_5m = [b for d_str in days for b in aggregate_5m(by_day[d_str])]
    c5 = [b["close"] for b in all_5m]
    e9_5, e20_5, e200_5 = compute_ema(c5, 9), compute_ema(c5, 20), compute_ema(c5, 200)
    st_dir_ce, st_val_ce, atr5 = compute_supertrend(all_5m, cfg.ce.supertrend_period, cfg.ce.supertrend_multiplier)
    st_dir_pe, st_val_pe, _ = compute_supertrend(all_5m, cfg.pe.supertrend_period, cfg.pe.supertrend_multiplier)
    m5_map = {}
    for i, b in enumerate(all_5m):
        b["e9"], b["e20"], b["e200"] = e9_5[i], e20_5[i], e200_5[i]
        b["st_dir"], b["st_val"], b["atr"] = st_dir_ce[i], st_val_ce[i], atr5[i]
        b["st_dir_pe"], b["st_val_pe"] = st_dir_pe[i], st_val_pe[i]
        m5_map[(datetime.fromisoformat(b["date"]) + timedelta(minutes=5)).isoformat()] = b

    by_day_1m = defaultdict(list)
    for b in all_1m:
        by_day_1m[b["day"]].append(b)

    cash = 50000.0
    trades = []
    closed_5m = []
    last_5m, prev_5m = None, None
    eqh_levels: list[float] = []

    for d_idx in range(1, len(days)):
        day, prev_day = days[d_idx], days[d_idx - 1]
        prev_bars, day_bars = by_day_1m[prev_day], by_day_1m[day]
        if len(day_bars) < 30:
            continue

        arbiter.reset_day()
        prior_vix = [k for k in vix_map if k < day]
        iv = vix_map[max(prior_vix)] if prior_vix else 13.5
        exp_date, dte = select_non_0dte_expiry(date.fromisoformat(day), hols, avoid_0dte=cfg.avoid_0dte)
        expiry = exp_date.isoformat()
        PDH = max(x["high"] for x in prev_bars)
        PDL = min(x["low"] for x in prev_bars)
        PDC = prev_bars[-1]["close"]

        pos: PositionState | None = None
        entry_idx = 0

        for i in range(1, len(day_bars)):
            p, c = day_bars[i - 1], day_bars[i]
            t = c["time"]
            now = datetime.fromisoformat(c["date"]) + timedelta(minutes=1)
            now_iso = now.isoformat()
            new_5m = False
            if now_iso in m5_map:
                prev_5m, last_5m = last_5m, m5_map[now_iso]
                closed_5m.append(last_5m)
                eqh_levels = find_equal_high_levels(closed_5m, last_5m["atr"])
                new_5m = True

            base_levels = [PDL, PDC, PDH]
            if t >= "09:30":
                or15 = [x for x in day_bars[:i] if x["time"] < "09:30"]
                if or15:
                    base_levels.extend([max(x["high"] for x in or15), min(x["low"] for x in or15)])
            if t >= "09:45":
                or30 = [x for x in day_bars[:i] if x["time"] < "09:45"]
                if or30:
                    base_levels.extend([max(x["high"] for x in or30), min(x["low"] for x in or30)])
            base_levels = sorted(set(base_levels))

            recent_sweep = detect_recent_sweep(day_bars[max(0, i - 15) : i + 1], base_levels)
            eqh_ok, sweep_hi = detect_confirmed_eqh_reversal(day_bars[max(0, i - 25) : i + 1], eqh_levels, last_5m)
            pe_levels = sorted(set(base_levels + ([sweep_hi] if sweep_hi is not None else [])))
            ctx_ce = MarketContext(
                time_hhmm=t,
                base_levels=base_levels,
                atr_5m=last_5m["atr"] if last_5m else None,
                st_dir_5m=last_5m["st_dir"] if last_5m else None,
                st_val_5m=last_5m["st_val"] if last_5m else None,
                ema20_5m=last_5m["e20"] if last_5m else None,
                ema200_5m=last_5m["e200"] if last_5m else None,
                recent_liquidity_sweep=recent_sweep,
            )
            ctx_pe = MarketContext(
                time_hhmm=t,
                base_levels=pe_levels,
                atr_5m=last_5m["atr"] if last_5m else None,
                st_dir_5m=last_5m["st_dir_pe"] if last_5m else None,
                st_val_5m=last_5m["st_val_pe"] if last_5m else None,
                ema20_5m=last_5m["e20"] if last_5m else None,
                ema200_5m=last_5m["e200"] if last_5m else None,
                recent_liquidity_sweep=eqh_ok,
            )

            if pos is not None:
                kind = "CE" if pos.side == "CALL" else "PE"
                dec = arbiter.evaluate_position(
                    pos,
                    ctx=ctx_ce,
                    curr_1m_high=c["high"],
                    curr_1m_low=c["low"],
                    curr_1m_close=c["close"],
                    mins_since_entry=i - entry_idx,
                    new_5m_closed=new_5m,
                    prev_5m=prev_5m,
                    curr_5m=last_5m,
                )
                if dec.action == "SCALE_OUT_TRIM" and len(pos.lots) > 1:
                    bid, _ = quote_option(c["close"], pos.strike, kind, pos.expiry, now, iv)
                    trimmed = pos.lots.pop()
                    pos.realized_gross_inr += (bid - trimmed.entry_prem) * cfg.lot_size
                    pos.total_fees_inr += calc_upstox_fee("SELL", bid, cfg.lot_size)
                    pos.trims += 1
                elif dec.action == "PYRAMID_ADD":
                    _, ask_add = quote_option(c["close"], pos.strike, kind, pos.expiry, now, iv)
                    bid_sl, _ = quote_option(pos.stop_spot, pos.strike, kind, pos.expiry, now, iv)
                    fee_add = calc_upstox_fee("BUY", ask_add, cfg.lot_size)
                    fee_exit_all = calc_upstox_fee("SELL", bid_sl, (len(pos.lots) + 1) * cfg.lot_size)
                    worst_pnl = (
                        pos.realized_gross_inr
                        - pos.total_fees_inr
                        - fee_add
                        - fee_exit_all
                        + sum((bid_sl - l.entry_prem) * cfg.lot_size for l in pos.lots)
                        + (bid_sl - ask_add) * cfg.lot_size
                    )
                    if worst_pnl >= -cfg.max_trade_risk_inr and ask_add * cfg.lot_size <= cash * 0.4:
                        pos.lots.append(LotEntry(entry_spot=c["close"], entry_prem=ask_add, entered_at=t))
                        pos.total_fees_inr += fee_add
                        pos.adds += 1
                elif dec.action in ("EXIT_MONOTONIC_SL", "EXIT_5M_CROSS_END", "EXIT_EOD_1510"):
                    exit_spot = dec.exit_spot if dec.exit_spot is not None else c["close"]
                    bid, _ = quote_option(exit_spot, pos.strike, kind, pos.expiry, now, iv)
                    gross = pos.realized_gross_inr + sum((bid - l.entry_prem) * cfg.lot_size for l in pos.lots)
                    fees = pos.total_fees_inr + calc_upstox_fee("SELL", bid, len(pos.lots) * cfg.lot_size)
                    net = gross - fees
                    cash += net
                    arbiter.record_trade_outcome(net)
                    if day >= "2026-10-15":
                        trades.append({
                            "day": day, "side": pos.side, "in_t": pos.entry_time, "out_t": t,
                            "contract": f"NIFTY {pos.strike} {kind} {pos.expiry}",
                            "dte": pos.dte, "max_lots": 1 + pos.adds, "adds": pos.adds, "trims": pos.trims,
                            "entry_spot": pos.entry_spot, "exit_spot": exit_spot,
                            "entry_prem": sum(l.entry_prem for l in pos.lots) / len(pos.lots),
                            "exit_prem": bid, "stage": pos.stage,
                            "reason": dec.action, "fees_inr": fees, "net_inr": net, "cash_after": cash,
                        })
                    pos = None
                continue

            ce_prop = arbiter.ce_agent.on_1m_close(
                ctx=ctx_ce, prev_e9=p["e9"], prev_e20=p["e20"],
                curr_close=c["close"], curr_e9=c["e9"], curr_e20=c["e20"],
            )
            pe_prop = arbiter.pe_agent.on_1m_close(
                ctx=ctx_pe, prev_e9=p["e9"], prev_e20=p["e20"],
                curr_close=c["close"], curr_e9=c["e9"], curr_e20=c["e20"],
            )

            chosen_preview = ce_prop or pe_prop
            est_risk = None
            ask_entry = 0.0
            strike = 0
            tier = "ATM"
            if chosen_preview is not None:
                kind = "CE" if chosen_preview.side == "CALL" else "PE"

                def _quote_risk(cand_strike: int) -> tuple[float, float]:
                    _, ask_c = quote_option(chosen_preview.entry_spot, cand_strike, kind, expiry, now, iv)
                    bid_c, _ = quote_option(chosen_preview.initial_sl, cand_strike, kind, expiry, now, iv)
                    r_inr = (
                        (ask_c - bid_c) * cfg.lot_size
                        + calc_upstox_fee("BUY", ask_c, cfg.lot_size)
                        + calc_upstox_fee("SELL", bid_c, cfg.lot_size)
                    )
                    return r_inr, ask_c * cfg.lot_size

                sel = select_dynamic_strike(
                    spot=chosen_preview.entry_spot,
                    side=chosen_preview.side,
                    dte=dte,
                    time_hhmm=t,
                    quote_and_risk_fn=_quote_risk,
                    max_risk_inr=cfg.max_trade_risk_inr,
                    max_premium_inr=cash * 0.4,
                )
                if sel is not None:
                    strike, tier = sel.strike, sel.tier
                    _, ask_entry = quote_option(chosen_preview.entry_spot, strike, kind, expiry, now, iv)
                    est_risk, _ = _quote_risk(strike)
                else:
                    est_risk = cfg.max_trade_risk_inr + 1.0

            action, chosen, _ = arbiter.arbitrate_entry(ce_prop, pe_prop, estimated_1lot_risk_inr=est_risk)
            if action == "ENTER" and chosen is not None:
                pos = PositionState(
                    side=chosen.side, strike=strike, expiry=expiry, dte=dte,
                    entry_time=t, entry_spot=chosen.entry_spot,
                    initial_risk_pts=chosen.risk_pts, stop_spot=chosen.initial_sl,
                    lots=[LotEntry(entry_spot=chosen.entry_spot, entry_prem=ask_entry, entered_at=t)],
                    total_fees_inr=calc_upstox_fee("BUY", ask_entry, cfg.lot_size),
                    thesis_log=[f"[{tier}] {chosen.thesis_summary}"],
                )
                entry_idx = i

    wins = [x for x in trades if x["net_inr"] > 0]
    losses = [x for x in trades if x["net_inr"] <= 0]
    net = sum(x["net_inr"] for x in trades)
    fees = sum(x["fees_inr"] for x in trades)
    pf = sum(x["net_inr"] for x in wins) / abs(sum(x["net_inr"] for x in losses)) if losses else 99.0
    eq, pk, dd = 50000.0, 50000.0, 0.0
    for x in trades:
        eq += x["net_inr"]
        pk = max(pk, eq)
        dd = max(dd, pk - eq)

    print(f"Policy: {VERSION} (hash={RULE_HASH[:12]})")
    print(f"Window: 2026-10-15 -> {days[-1]} | Trades={len(trades)} | WR={len(wins)/len(trades)*100:.1f}% | PF={pf:.2f}")
    print(f"Net P&L: Rs {net:+,.2f} ({net/50000*100:+.1f}%) | Fees: Rs {fees:,.2f} | MaxDD: Rs {dd:,.2f} | Ret/DD: {net/dd:.2f}x")
    print(f"Worst Trade: Rs {min((x['net_inr'] for x in losses), default=0):+,.2f} | Best Trade: Rs {max((x['net_inr'] for x in wins), default=0):+,.2f}")

    for side in ("CALL", "PUT"):
        st = [x for x in trades if x["side"] == side]
        sw = [x for x in st if x["net_inr"] > 0]
        sl = [x for x in st if x["net_inr"] <= 0]
        spf = sum(x["net_inr"] for x in sw) / abs(sum(x["net_inr"] for x in sl)) if sl else 99.0
        snet = sum(x["net_inr"] for x in st)
        print(f"  {side:4} Agent: N={len(st):2} | WR={len(sw)/len(st)*100 if st else 0:5.1f}% | PF={spf:4.2f} | Net=Rs {snet:+9,.2f}")

    print("\n--- FULL TRADE LEDGER ---")
    for idx, x in enumerate(trades, 1):
        print(
            f"{idx:2d}. {x['day']} {x['in_t']}->{x['out_t']} | {x['side']:4} | {x['contract']:27} (DTE={x['dte']}) | "
            f"Lots={x['max_lots']} (+{x['adds']}/-{x['trims']}) | Spot {x['entry_spot']:8.1f}->{x['exit_spot']:8.1f} | "
            f"Prem {x['entry_prem']:6.2f}->{x['exit_prem']:6.2f} | {x['reason']:17} | Fee=Rs{x['fees_inr']:6.2f} | Net=Rs {x['net_inr']:+9.2f}"
        )


if __name__ == "__main__":
    main()
