"""uv run python -m app.liquidity_signals [--once]

Suggested NIFTY trades from the liquidity sweep strategy (v2, sheet "Improved Strategy" in
DocumentsAndDiagrams/NIFTY_Liquidity_Plan_2026-10-13.xlsx), sent to the alert channels (Telegram and/or email)
the moment every entry condition is met on a completed 5m bar. Runs every 5 minutes until the last entry time.

Long:  5m trades >1 pt below the previous day's low -> 15m close above the last confirmed 15m swing high ->
       5m close above VWAP, above that low and above the day's open. Stop: last 15m swing low after the sweep - 5.
Short: 5m trades >1 pt above the previous day's high -> 15m close back below it -> 5m close below the last
       confirmed 5m swing low -> 5m close below VWAP, below that high and below the day's open.
       Stop: last 5m swing high after the raid + 5.
Target 3R, stop to entry once price reaches +1R, entries 09:30-14:30, one per side per day, exit 15:10 if neither
stop nor target is hit. Option: OTM_STEPS strikes out of the money (2016-2026: more trades fit the risk budget).
Skipped: NIFTY expiry days, setups risking under MIN_RISK points, and
BREAK_DAYS trading days after LOSS_STREAK losses in a row. Size: RISK_PCT% of the trading capital at risk per trade.
"""

from __future__ import annotations

import argparse
import io
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

import httpx
from dotenv import load_dotenv
from matplotlib.figure import Figure

from app import alerts
from app.api.market import (
    IST,
    _chart,
    add_vwap,
    intraday_levels,
    parse_yahoo_chart,
    trading_levels,
)
from app.brokers.groww import GrowwQuotes
from app.config import Settings, get_settings
from app.market.feed import BrokerFeed
from app.market.hours import holidays
from app.paper import order_charges
from app.trading.option_pnl import black76, plan_pnl

# ponytail: Yahoo bars are a few minutes delayed and the index has no volume, so VWAP comes from the NIFTYBEES ETF.
# Switch to broker bars (Groww/Kite) and NIFTY futures VWAP for real-time signals.
INDEX, VWAP_PROXY = "^NSEI", "NIFTYBEES.NS"
SWING, R_MULT, OTM_STEPS, FIRST_ENTRY, LAST_ENTRY = 2, 3.0, 0, "09:30", "14:30"
FRESH = timedelta(minutes=15)  # older triggers are reported as missed, never sent: no chasing a late entry
MIN_RISK = 30  # pts: over 2016-2026 setups with a closer stop earned nothing after costs
RISK_PCT, LOSS_STREAK, BREAK_DAYS, EXIT = 5, 3, 3, "15:10"
MIN_VIX, MAX_VIX, STAGNATION_BARS = 11.5, 22.0, 18  # 18 bars = 90 min max stagnation before cutting midday chop
BLACKOUT_START, BLACKOUT_END = "11:30", "13:00"  # Midday lunch lull blackout window to avoid chop


def _at(date: str, minutes: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d %H:%M") + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def strike_of(s: dict, step: float, otm_steps: int = OTM_STEPS) -> float:
    """otm_steps strikes out of the money: above the entry for a long's call, below it for a short's put."""
    return (round(s["entry"] / step) + (otm_steps if s["side"] == "Long" else -otm_steps)) * step


def _bucket(date: str) -> str:
    """Start of the 15m bar (09:15, 09:30, ...) holding the 5m bar starting at `date`."""
    t = datetime.strptime(date, "%Y-%m-%d %H:%M")
    return (t - timedelta(minutes=(t.minute - 15) % 15)).strftime("%Y-%m-%d %H:%M")


def to_15m(bars: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in bars:
        start = _bucket(b["date"])
        if out and out[-1]["date"] == start:
            out[-1].update(high=max(out[-1]["high"], b["high"]), low=min(out[-1]["low"], b["low"]), close=b["close"])
        else:
            out.append({"date": start, "high": b["high"], "low": b["low"], "close": b["close"]})
    return out


def pivots(bars: list[dict], key: str) -> list[int]:
    """Indexes of swing highs/lows: the extreme of the SWING bars on either side."""
    pick = max if key == "high" else min
    n = SWING
    return [j for j in range(n, len(bars) - n) if bars[j][key] == pick(b[key] for b in bars[j - n : j + n + 1])]


def scan(bars: list[dict], above_vwap: dict[str, bool | None], pdh: float, pdl: float,
         min_risk: float = MIN_RISK, stop_at: str = "swing", min_sweep: float = 2.0,
         require_trendline: bool = False, skip_lunch: bool = False) -> list[dict]:  # fmt: skip
    """Entries in one session's completed 5m `bars`; `above_vwap[date]` is whether that bar closed above VWAP.
    `stop_at` "sweep" puts every stop 5 pts past the sweep low / raid high instead of at the last swing.
    `min_sweep` requires sweep depth >= min_sweep (default 2.0 pts) past PDH/PDL to filter noise.
    `require_trendline` enforces breakdown below ascending trendline (Short) or breakout above descending trendline (Long).
    `skip_lunch` avoids entries between 11:30 and 13:00 IST to prevent lunch lull chop."""
    if not bars:
        return []
    day_open, b15, out = bars[0]["open"], to_15m(bars), []
    for side in ("Long", "Short"):
        long = side == "Long"
        stage, ext, swept, why = 0, 0.0, "", []
        for i, b in enumerate(bars):
            if stage == 0:
                if (long and b["low"] <= pdl - min_sweep) or (not long and b["high"] >= pdh + min_sweep):
                    stage, ext, swept = 1, b["low"] if long else b["high"], b["date"]
                    swept_level = f"{'below PDL' if long else 'above PDH'} {pdl if long else pdh:,.2f}"
                    why = [f"{_at(b['date'], 5)[11:]} sweep: 5m {'low' if long else 'high'} {ext:,.2f} {swept_level}"]
                continue
            ext = min(ext, b["low"]) if long else max(ext, b["high"])
            if stage == 1:
                k = next((k for k, x in enumerate(b15) if _at(x["date"], 10) == b["date"]), None)
                if k is not None:
                    c15, at = b15[k]["close"], _at(b["date"], 5)[11:]
                    if long:
                        prior = pivots(b15[:k], "high")
                        if prior and c15 > (sh := b15[prior[-1]])["high"]:
                            stage = 2
                            why.append(f"{at} structure break: 15m close {c15:,.2f} above 15m swing high {sh['high']:,.2f} ({sh['date'][11:]})")
                    elif c15 < pdh:
                        stage = 2
                        why.append(f"{at} rejection: 15m close {c15:,.2f} back below PDH {pdh:,.2f}")
            elif stage == 2 and not long:
                prior = pivots(bars[:i], "low")
                if prior and b["close"] < (sl := bars[prior[-1]])["low"]:
                    stage = 3
                    why.append(f"{_at(b['date'], 5)[11:]} structure break: 5m close {b['close']:,.2f} below 5m swing low {sl['low']:,.2f} ({sl['date'][11:]})")
            if stage != (2 if long else 3):
                continue
            closes_at = _at(b["date"], 5)
            if closes_at[11:] > LAST_ENTRY:
                break
            if skip_lunch and BLACKOUT_START <= closes_at[11:] <= BLACKOUT_END:
                continue
            c = b["close"]
            inside = c > pdl and c > day_open if long else c < pdh and c < day_open
            if closes_at[11:] < FIRST_ENTRY or above_vwap.get(b["date"]) != long or not inside:
                continue
            word, level = ("above", f"PDL {pdl:,.2f}") if long else ("below", f"PDH {pdh:,.2f}")
            why.append(f"{closes_at[11:]} entry: 5m close {c:,.2f} {word} VWAP, {word} {level} and {word} the open {day_open:,.2f}")

            # Trendline Confluence Check
            tl_key = "high" if long else "low"
            piv = pivots(bars[: i + 1], tl_key)
            tl_ok, tl_desc = True, ""
            if len(piv) >= 2:
                x1, x2 = piv[-2], piv[-1]
                y1, y2 = bars[x1][tl_key], bars[x2][tl_key]
                if x2 != x1:
                    slope = (y2 - y1) / (x2 - x1)
                    tl_val = y1 + slope * (i - x1)
                    if not long and slope >= 0:
                        tl_ok = c < tl_val
                        tl_desc = f"{closes_at[11:]} trendline breakdown: 5m close {c:,.2f} below ascending trendline {tl_val:,.2f}"
                    elif long and slope <= 0:
                        tl_ok = c > tl_val
                        tl_desc = f"{closes_at[11:]} trendline breakout: 5m close {c:,.2f} above descending trendline {tl_val:,.2f}"
            if require_trendline and not tl_ok:
                continue
            if tl_desc:
                why.append(tl_desc)
            if stop_at == "sweep":
                stop, basis = (ext - 5, f"sweep low {ext:,.2f} - 5") if long else (ext + 5, f"raid high {ext:,.2f} + 5")
            elif long:
                done = [x for x in b15 if _at(x["date"], 15) <= closes_at]
                lows = [done[j] for j in pivots(done, "low") if done[j]["date"] >= _bucket(swept)]
                if lows and lows[-1]["low"] - 5 < c:
                    stop, basis = lows[-1]["low"] - 5, f"15m swing low {lows[-1]['low']:,.2f} ({lows[-1]['date'][11:]}) - 5"
                else:
                    stop, basis = ext - 5, f"sweep low {ext:,.2f} - 5"
            else:
                done = [x for x in b15 if _at(x["date"], 15) <= closes_at]
                highs = [done[j] for j in pivots(done, "high") if done[j]["date"] >= _bucket(swept)]
                if highs and highs[-1]["high"] + 5 > c:
                    stop, basis = highs[-1]["high"] + 5, f"15m swing high {highs[-1]['high']:,.2f} ({highs[-1]['date'][11:]}) + 5"
                else:
                    highs5 = [bars[j] for j in pivots(bars[: i + 1], "high") if bars[j]["date"] >= swept]
                    if highs5 and highs5[-1]["high"] + 5 > c:
                        stop, basis = highs5[-1]["high"] + 5, f"5m swing high {highs5[-1]['high']:,.2f} ({highs5[-1]['date'][11:]}) + 5"
                    else:
                        stop, basis = ext + 5, f"raid high {ext:,.2f} + 5"
            risk = abs(c - stop)
            if risk >= min_risk:
                target = c + R_MULT * risk if long else c - R_MULT * risk
                out.append({"side": side, "time": closes_at, "entry": c, "stop": round(stop, 2),
                            "target": round(target, 2), "risk": round(risk, 2), "level": pdl if long else pdh,
                            "why": why, "stop_basis": basis, "candle": b, "day_open": day_open})  # fmt: skip
            break
    return out


def message(s: dict, settings: Settings, plan: dict | None = None, levels: list[dict] | None = None) -> str:
    """The signal as text: trade, option, why it fired, trigger candle, nearby levels and risk."""
    lot, step = settings.lot_size("NIFTY") or 65, settings.strike_step("NIFTY") or 50
    long, e, c = s["side"] == "Long", s["entry"], s.get("candle")
    fut_loss = s["risk"] * lot
    if plan:
        stop, target = plan["exits"]
        lots_n = plan.get("lots", 1) or 1
        option_lines = [
            f"1. NAKED OPTION: BUY NIFTY {plan['option']} {plan['expiry']} ~Rs {plan['entry_premium']:,.2f} "
            + (f"(quote Rs {plan['quote']:,.2f}, IV {plan['iv_pct']:g}%)" if plan["quote"] is not None
               else f"(estimated at India VIX {plan['iv_pct']:g}%: no live option chain, check the real premium)"),
            f"   Stop ~Rs {stop['premium']:,.2f} | Target ~Rs {target['premium']:,.2f} (estimated at the index stop/target)",
            f"   Per lot ({lot}): {stop['pnl'] / lots_n:+,.0f} at stop | {target['pnl'] / lots_n:+,.0f} at target (after charges)",
        ]
        spread = plan.get("spread")
        if spread:
            option_lines += [
                "",
                "2. THETA-PROTECTED SPREAD (Debit Spread, Recommended):",
                f"   BUY {spread['buy_strike']} & SELL {spread['sell_strike']} {plan['expiry']} (Net Debit ~Rs {spread['net_debit']:,.2f})",
                f"   Per lot ({lot}): Max Risk ~Rs {spread['max_risk'] / lots_n:,.0f} | Max Reward ~Rs {spread['max_reward'] / lots_n:,.0f} (Zero midday theta decay)",
            ]
        if plan.get("iv_pct") is not None:
            vix_val = plan["iv_pct"]
            if vix_val < MIN_VIX:
                option_lines += ["", f"⚠️ India VIX {vix_val:g}% is Low (<{MIN_VIX}%): Option gamma expansion is muted; prefer Futures."]
            elif vix_val > MAX_VIX:
                option_lines += ["", f"⚠️ India VIX {vix_val:g}% is Elevated (>{MAX_VIX}%): Risk of IV crush; strongly prefer Debit Spread."]
        option_loss = -stop["pnl"] / lots_n
    else:
        option = f"{strike_of(s, step):g} {'CE' if long else 'PE'}"
        option_lines = [f"{option}: ~Rs {fut_loss * 0.4:,.0f}/lot at stop (delta ~0.4; option chain unavailable)"]
        option_loss = fut_loss * 0.4
    pool_tag = f"{s.get('pool')} " if s.get("pool") else ""
    lines = [
        f"NIFTY {s['side'].upper()} signal at {s['time'][11:]} IST ({'sell-side sweep' if long else 'buy-side raid'} of {pool_tag}{s['level']:,.2f})",
        "",
        "TRADE (index)",
        f"Entry {e:,.2f} (5m close) | Stop {s['stop']:,.2f} | Target {s['target']:,.2f}",
        f"Risk {s['risk']:.0f} pts ({s['risk'] / e:.2%}) | Reward {abs(s['target'] - e):.0f} pts | R:R 1:{abs(s['target'] - e) / s['risk']:.3g}",
        *([f"Stop basis: {s['stop_basis']}"] if s.get("stop_basis") else []),
        f"Move the stop to entry {e:,.2f} once price reaches {e + s['risk'] if long else e - s['risk']:,.2f} (+1R)",
        f"Staged targets: T1 {e + s['risk'] if long else e - s['risk']:,.2f} (+1R: SL to BE) | T2 {e + 2 * s['risk'] if long else e - 2 * s['risk']:,.2f} (+2R: Book 50%, lock +1R) | T3 {s['target']:,.2f} (Runner: trail on 5m candle close reversal)",
        "",
        "OPTION & SPREAD STRATEGY",
        *option_lines,
    ]
    if s.get("why"):
        lines += ["", "WHY (all conditions met)", *s["why"]]
    if c:
        lines += ["", f"TRIGGER CANDLE (5m {c['date'][11:]}-{s['time'][11:]})",
                  f"O {c['open']:,.2f} | H {c['high']:,.2f} | L {c['low']:,.2f} | C {c['close']:,.2f}"]  # fmt: skip
    if levels:
        above = sorted((lv for lv in levels if lv["price"] > e), key=lambda lv: lv["price"])
        below = sorted((lv for lv in levels if lv["price"] < e), key=lambda lv: -lv["price"])
        lo, hi = sorted((e, s["target"]))
        blockers = [lv for lv in (above if long else below) if lo < lv["price"] < hi]

        def fmt(lvs: list[dict]) -> str:
            return ", ".join(f"{lv['label']} {lv['price']:,.0f}" for lv in lvs) or "none"

        lines += ["", "LEVELS", f"Resistance: {fmt(above[:4])}", f"Support: {fmt(below[:4])}",
                  f"Before target: {fmt(blockers)}{' (price may stall there)' if blockers else ''}",
                  "(PD = previous day, DH/DL day high/low, ORH/ORL opening range, BSL/SSL untouched swing high/low, P/R/S pivots)"]  # fmt: skip
    lines += [
        "",
        f"RISK & CAPITAL MANAGEMENT ({RISK_PCT}% Risk per Trade)",
        f"At stop: Rs {fut_loss:,.0f}/lot futures | Rs {option_loss:,.0f}/lot option",
        (f"Option lots = {RISK_PCT}% of your trading capital / Rs {option_loss:,.0f}, e.g. Rs 50,000 -> "
         f"{int(500 * RISK_PCT // option_loss) if option_loss > 0 else 0} lots (skip if 0; 2 lots recommended for staged exits; "
         "if 1 lot, book at T2 or on 5m candle close reversal)"),
        f"Max 1 trade per side (so at most 2 a day), exit {EXIT} if neither stop nor target is hit.",
        f"{LOSS_STREAK} losses in a row = {BREAK_DAYS} trading days off (Circuit Breaker).",
        "Data is a few minutes delayed: check the live price first; skip if it already ran past stop or target.",
    ]
    return "\n".join(lines)


def option_plan(s: dict, settings: Settings, lots: int = 2, otm_steps: int = OTM_STEPS) -> dict | None:
    """The strike_of() call (long) or put (short) priced from Groww's live chain at entry, stop and target; estimated
    at India VIX when the chain is unavailable; None if VIX is unavailable too."""
    step = settings.strike_step("NIFTY") or 50
    feed = BrokerFeed(GrowwQuotes(), settings, iv_rank=lambda *_: None, prev_range=lambda *_: None)
    plan = None
    try:
        snapshot = feed.snapshot("NIFTY")
        if snapshot is not None:
            plan = plan_pnl(snapshot, settings, "CE" if s["side"] == "Long" else "PE", strike_of(s, step, otm_steps=otm_steps),
                            s["entry"], {"stop": s["stop"], "target": s["target"]}, lots=lots)  # fmt: skip
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"option chain unavailable: {exc}")
    if plan is None:
        try:
            vix = parse_yahoo_chart(_chart("^INDIAVIX", "1d", "5d"))[-1]["close"]
        except (httpx.HTTPError, IndexError, KeyError) as exc:
            print(f"India VIX unavailable: {exc}")
            return None
        plan = estimated_plan(s, settings, vix, lots=lots, otm_steps=otm_steps) if vix else None
    if plan and "spread" not in plan:
        kind = "CE" if s["side"] == "Long" else "PE"
        strike = strike_of(s, step, otm_steps=otm_steps)
        spread_strike = strike + (2 * step if kind == "CE" else -2 * step)
        entry_buy = plan["entry_premium"]
        entry_sell = entry_buy * 0.45
        spread_debit = max(entry_buy - entry_sell, 0.5)
        lot = settings.lot_size("NIFTY") or 65
        total_qty = lot * lots
        plan["spread"] = {
            "buy_strike": f"{strike:g} {kind}",
            "sell_strike": f"{spread_strike:g} {kind}",
            "net_debit": round(spread_debit, 2),
            "max_risk": round(spread_debit * total_qty, 2),
            "max_reward": round((2 * step - spread_debit) * total_qty, 2),
        }
    return plan


def weekly_expiry(day: date, settings: Settings) -> date:
    """NIFTY's weekly expiry on or after `day`: Tuesday, or the trading day before it when Tuesday is a holiday."""
    exp = day + timedelta(days=(1 - day.weekday()) % 7)
    while exp.isoformat() in holidays(settings) or exp.weekday() >= 5:
        exp -= timedelta(days=1)
    return exp


def estimated_plan(s: dict, settings: Settings, iv_pct: float, expiry: date | None = None, lots: int = 2, otm_steps: int = OTM_STEPS) -> dict:
    """option_plan() without a live chain: the strike_of() option to `expiry` (default: the weekly one) priced by
    Black-76 at `iv_pct` (India VIX, a 30-day ATM IV), forward = spot. Same shape as plan_pnl() with `quote` None."""
    step, lot = settings.strike_step("NIFTY") or 50, settings.lot_size("NIFTY") or 65
    total_qty = lot * lots
    kind, strike = "CE" if s["side"] == "Long" else "PE", strike_of(s, step, otm_steps=otm_steps)
    expiry = expiry or weekly_expiry(date.fromisoformat(s["time"][:10]), settings)
    left = datetime.combine(expiry, dtime.fromisoformat(settings.market_close), IST) - datetime.strptime(
        s["time"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)  # fmt: skip
    years = lambda hold: max(left.total_seconds() / 86400 - hold, 0) / 365
    entry = black76(s["entry"], strike, years(0), iv_pct / 100, kind)
    buy = order_charges(settings, kind, "BUY", entry, total_qty, s["time"][:10])["total"]
    exits = []
    for spot in (s["stop"], s["target"]):
        premium = black76(spot, strike, years(0.15), iv_pct / 100, kind)
        charges = buy + order_charges(settings, kind, "SELL", premium, total_qty, s["time"][:10])["total"]
        exits.append({"premium": round(premium, 2), "pnl": round((premium - entry) * total_qty - charges, 2)})
    spread_sell_strike = strike + (2 * step if kind == "CE" else -2 * step)
    entry_sell = black76(s["entry"], spread_sell_strike, years(0), iv_pct / 100, kind)
    spread_debit = max(entry - entry_sell, 0.5)
    spread_buy_chg = order_charges(settings, kind, "BUY", entry, total_qty, s["time"][:10])["total"]
    spread_sell_chg = order_charges(settings, kind, "SELL", entry_sell, total_qty, s["time"][:10])["total"]
    spread_max_risk = round(spread_debit * total_qty + spread_buy_chg + spread_sell_chg, 2)
    spread_max_reward = round((2 * step - spread_debit) * total_qty - spread_buy_chg - spread_sell_chg, 2)
    spread = {
        "buy_strike": f"{strike:g} {kind}",
        "sell_strike": f"{spread_sell_strike:g} {kind}",
        "net_debit": round(spread_debit, 2),
        "max_risk": spread_max_risk,
        "max_reward": spread_max_reward,
    }
    return {"option": f"{strike:g} {kind}", "expiry": expiry.isoformat(), "iv_pct": round(iv_pct, 2),
            "forward_basis": 0.0, "entry_premium": round(entry, 2), "quote": None, "quantity": total_qty, "lots": lots,
            "exits": exits, "spread": spread}  # fmt: skip


def option_bars(bars: list[dict], plan: dict, settings: Settings) -> list[dict]:
    """The option's premium per 5m bar, estimated from the index at the plan's IV (no intraday option data)."""
    strike, kind = plan["option"].split()
    expires = datetime.combine(date.fromisoformat(plan["expiry"]), dtime.fromisoformat(settings.market_close), IST)
    out = []
    for b in bars:
        when = datetime.strptime(b["date"], "%Y-%m-%d %H:%M").replace(tzinfo=IST) + timedelta(minutes=5)
        years = max((expires - when).total_seconds(), 0) / 86400 / 365
        p = {k: black76(b[k] + plan["forward_basis"], float(strike), years, plan["iv_pct"] / 100, kind)
             for k in ("open", "high", "low", "close")}  # fmt: skip
        out.append({"date": b["date"], "open": p["open"], "close": p["close"],
                    "high": max(p["high"], p["low"]), "low": min(p["high"], p["low"])})  # fmt: skip
    return out


def chart(bars: list[dict], levels: list[tuple], title: str, entry_time: str = "", entry: float | None = None,
          stop: float | None = None, target: float | None = None,
          trendline: tuple[int, float, int, float] | None = None) -> bytes:  # fmt: skip
    """PNG of 5m candles, TradingView position style: optional green reward and red risk boxes from the entry candle,
    horizontal `levels` (label, price, color, line style) tagged on the right price axis, and optional trendline."""
    fig = Figure(figsize=(10, 5.5), dpi=110)
    ax = fig.add_subplot()
    for x, b in enumerate(bars):
        color = "#26a69a" if b["close"] >= b["open"] else "#ef5350"
        ax.vlines(x, b["low"], b["high"], color=color, linewidth=0.8)
        ax.bar(x, abs(b["close"] - b["open"]) or 0.05, bottom=min(b["open"], b["close"]), color=color, width=0.7)
    entry_x = next((x for x, b in enumerate(bars) if _at(b["date"], 5) == entry_time), len(bars) - 1)
    right = len(bars) + 8
    if entry is not None and stop is not None and target is not None:
        ax.fill_between([entry_x - 0.5, right], entry, target, color="#26a69a", alpha=0.18, linewidth=0)
        ax.fill_between([entry_x - 0.5, right], entry, stop, color="#ef5350", alpha=0.18, linewidth=0)
        ax.plot(entry_x, entry, marker="^" if target > entry else "v", color="#1e88e5", markersize=11)
    if trendline:
        x1, y1, x2, y2 = trendline
        if x2 != x1:
            slope = (y2 - y1) / (x2 - x1)
            x_end = min(entry_x + 3, len(bars) - 1)
            y_end = y1 + slope * (x_end - x1)
            ax.plot([x1, x_end], [y1, y_end], color="#f57c00", linestyle="-.", linewidth=1.8, zorder=4)
            if entry is not None and target is not None:
                offset = 12 if target < entry else -12
                ax.annotate("Trendline Break", (entry_x, entry), xycoords="data",
                            xytext=(entry_x - 4, entry + offset), textcoords="data",
                            fontsize=8, color="#f57c00", fontweight="bold",
                            arrowprops={"arrowstyle": "->", "color": "#f57c00", "lw": 1.2})
    ax.set_xlim(-1, right)
    for _, price, color, style in levels:
        ax.axhline(price, color=color, linestyle=style, linewidth=1)
    low, high = ax.get_ylim()
    gap, tag_y = (high - low) * 0.045, low - 1e9  # tags are ~4.5% of the axis tall: push each above the one below
    for label, price, color, _ in sorted(levels, key=lambda lv: lv[1]):
        tag_y = max(price, tag_y + gap)
        ax.annotate(f"{label} {price:,.2f}", (1, price), xycoords=("axes fraction", "data"), xytext=(1.01, tag_y),
                    textcoords=("axes fraction", "data"), va="center", fontsize=8, color="white", annotation_clip=False,
                    bbox={"boxstyle": "square,pad=0.25", "fc": color, "ec": color})  # fmt: skip
    ticks = [x for x, b in enumerate(bars) if b["date"][14:16] in ("15", "45")]
    ax.set_xticks(ticks, [bars[x]["date"][11:] for x in ticks], fontsize=8)
    ax.set_title(title, fontsize=11)
    ax.grid(alpha=0.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return buf.getvalue()


def charts(day: list[dict], s: dict, levels: list[dict], plan: dict | None, settings: Settings) -> list[bytes]:
    """The option's estimated premium chart (when priced) and the NIFTY chart, each with entry, stop and target;
    the NIFTY chart also draws the `levels` (as in message()) that fall inside its price range and broken trendline."""
    out = []
    entry_x = next((x for x, b in enumerate(day) if _at(b["date"], 5) == s["time"]), len(day) - 1)
    if plan:
        stop, target = (row["premium"] for row in plan["exits"])
        ob = option_bars(day, plan, settings)
        opt_piv = pivots(ob[: entry_x + 1], "high")
        opt_tl = (opt_piv[-2], ob[opt_piv[-2]]["high"], opt_piv[-1], ob[opt_piv[-1]]["high"]) if len(opt_piv) >= 2 else None
        out.append(chart(
            ob,
            [("Entry", plan["entry_premium"], "#1e88e5", "-"), ("Stop", stop, "#e53935", "--"), ("Target", target, "#43a047", "--")],
            f"NIFTY {plan['option']} {plan['expiry']}: estimated premium at IV {plan['iv_pct']:g}% (buy at {s['time'][11:]})",
            s["time"], plan["entry_premium"], stop, target, trendline=opt_tl,
        ))  # fmt: skip
    lo = min(s["stop"], s["target"], *(b["low"] for b in day))
    hi = max(s["stop"], s["target"], *(b["high"] for b in day))
    near = [(lv["label"], lv["price"], "#7e57c2" if lv["label"].startswith("PD") else "#5c6bc0", ":")
            for lv in levels or [] if lo <= lv["price"] <= hi]  # fmt: skip
    entry_x = next((x for x, b in enumerate(day) if _at(b["date"], 5) == s["time"]), len(day) - 1)
    key = "low" if s["side"] == "Short" else "high"
    piv = pivots(day[: entry_x + 1], key)
    tl = (piv[-2], day[piv[-2]][key], piv[-1], day[piv[-1]][key]) if len(piv) >= 2 else None
    out.append(chart(
        day,
        [*near, ("Open", day[0]["open"], "#9e9e9e", ":"),
         ("Entry", s["entry"], "#1e88e5", "-"), ("Stop", s["stop"], "#e53935", "--"), ("Target", s["target"], "#43a047", "--")],
        f"NIFTY 5m {day[0]['date'][:10]}: {s['side'].upper()} at {s['time'][11:]} IST",
        s["time"], s["entry"], s["stop"], s["target"], trendline=tl,
    ))  # fmt: skip
    return out


def session_data(range_: str = "5d") -> tuple[list[dict], dict[str, bool | None], list[dict]]:
    bars = [b for b in parse_yahoo_chart(_chart(INDEX, "5m", range_)) if None not in (b["open"], b["high"], b["low"])]
    proxy = add_vwap(parse_yahoo_chart(_chart(VWAP_PROXY, "5m", range_)))
    above = {b["date"]: b["close"] > b["vwap"] if b["vwap"] else None for b in proxy}
    return bars, above, parse_yahoo_chart(_chart(INDEX, "1d", "6mo"))


def exit_of(day: list[dict], s: dict) -> tuple[str, float, str]:
    """(reason, index price, time) of the first stop/target touch after the entry bar, else the 15:10 close. The stop
    moves to the entry from the bar after one reaching +1R (same-bar order unknown: the stop counts first)."""
    long, stop, reason = s["side"] == "Long", s["stop"], "Stop"
    one_r = s["entry"] + s["risk"] if long else s["entry"] - s["risk"]
    for b in day:
        if b["date"] < s["time"]:
            continue
        at = _at(b["date"], 5)
        if b["low"] <= stop if long else b["high"] >= stop:
            return reason, stop, at
        if b["high"] >= s["target"] if long else b["low"] <= s["target"]:
            return "Target", s["target"], at
        if at[11:] >= EXIT:
            return EXIT, b["close"], at
        if b["high"] >= one_r if long else b["low"] <= one_r:
            stop, reason = s["entry"], "Breakeven"
    return "Last bar", day[-1]["close"], _at(day[-1]["date"], 5)


def staged_exit_of(
    day: list[dict],
    s: dict,
    partial_r: float = 2.0,
    partial_pct: float = 0.5,
    be_r: float = 1.0,
    trail_on_close: bool = True,
    trail_trigger_r: float = 1.5,
    stagnation_bars: int = 0,
) -> dict:
    """Evaluates staged exits:
    1. Breakeven: once price reaches +be_r (+1R), SL moves to entry from next bar.
    2. Partial: books partial_pct (e.g. 50%) at +partial_r (+2R).
    3. Trailing Stop / Lock-in: once +partial_r (+2R) is reached, SL floor moves to at least +1R.
    4. Candle Close Trail: once +trail_trigger_r (+1.5R) or partial target is reached, if a 5m candle
       closes against the trend (or closes beyond the prior candle's extreme), exit on that confirmed candle close.
    5. Final Target: exits remaining at s['target'] (e.g. +3R).
    6. Stagnation Exit: exits if after stagnation_bars (e.g. 18 = 90m) price has not reached +0.5R.
    7. Time Exit: exits at 15:10 close if neither hit.
    Returns a dict with overall weighted R-multiple and exit details.
    """
    long, e, risk = s["side"] == "Long", s["entry"], s["risk"]
    sg = 1 if long else -1
    stop = s["stop"]
    be_level = e + sg * be_r * risk
    partial_level = e + sg * partial_r * risk
    final_target = s["target"]

    partial_hit = False
    partial_at = None
    partial_px = None
    be_triggered = False
    trail_active = False

    after = [b for b in day if b["date"] >= s["time"]]
    if not after:
        after = [day[-1]]

    reason = "Last bar"
    final_px = day[-1]["close"]
    final_at = _at(day[-1]["date"], 5)

    for idx, b in enumerate(after):
        at = _at(b["date"], 5)

        # 1. Check Stop hit on this bar
        if (b["low"] <= stop) if long else (b["high"] >= stop):
            final_px = stop
            final_at = at
            if stop == be_level:
                reason = "Partial + +1R Trail Stop" if partial_hit else "+1R Trail Stop"
            elif stop == e:
                reason = "Partial + Breakeven" if partial_hit else "Breakeven"
            else:
                reason = "Partial + Stop" if partial_hit else "Stop"
            break

        # 2. Check Partial Target hit
        if not partial_hit:
            if (b["high"] >= partial_level) if long else (b["low"] <= partial_level):
                partial_hit = True
                partial_px = partial_level
                partial_at = at
                # Guarantee stop is at least at +1R floor for subsequent bars
                stop = max(stop, be_level) if long else min(stop, be_level)
                be_triggered = True

        # 3. Check Final Target hit
        if (b["high"] >= final_target) if long else (b["low"] <= final_target):
            final_px = final_target
            final_at = at
            reason = "Target"
            break

        # 4. Check Time exit (15:10)
        if at[11:] >= EXIT:
            final_px = b["close"]
            final_at = at
            reason = "Time (15:10)"
            break

        # 4b. Check Midday Stagnation exit (e.g. 18 bars = 90m without making headway)
        if stagnation_bars and idx >= stagnation_bars and not be_triggered and not partial_hit:
            cur_r = (b["close"] - e) * sg / risk
            if cur_r < 0.5:
                final_px = b["close"]
                final_at = at
                reason = "Stagnation Exit (90m)"
                break

        # 5. Check if +1R was reached to move stop to Breakeven for subsequent bars
        if not be_triggered:
            if (b["high"] >= be_level) if long else (b["low"] <= be_level):
                stop = max(stop, e) if long else min(stop, e)
                be_triggered = True

        # 6. Check if price touched trail_trigger_r to enable candle close trailing
        reached_trail = (b["high"] >= e + sg * trail_trigger_r * risk) if long else (b["low"] <= e - trail_trigger_r * risk)
        if reached_trail:
            trail_active = True

        # 7. Once partial or trail threshold is reached, check 5m Candle Close Reversal
        if (partial_hit or trail_active) and trail_on_close and idx > 0:
            prev_b = after[idx - 1]
            reversal = (b["close"] < b["open"] or b["close"] < prev_b["low"]) if long else (b["close"] > b["open"] or b["close"] > prev_b["high"])
            if reversal:
                final_px = b["close"]
                final_at = at
                reason = "Partial + Candle Close Trail" if partial_hit else "Candle Close Trail"
                break

    # Calculate overall realized R
    final_leg_r = (final_px - e) * sg / risk
    if partial_hit:
        total_r = partial_pct * partial_r + (1.0 - partial_pct) * final_leg_r
    else:
        total_r = final_leg_r

    return {
        "exit_reason": reason,
        "exit_price": round(final_px, 2),
        "exit_time": final_at,
        "r_multiple": round(total_r, 3),
        "partial_hit": partial_hit,
        "partial_price": round(partial_px, 2) if partial_px is not None else None,
        "partial_time": partial_at,
        "be_triggered": be_triggered,
    }


def break_until(bars: list[dict], above: dict, daily: list[dict], settings: Settings, today: str) -> date | None:
    """Last day off of the latest LOSS_STREAK-losses-in-a-row break (BREAK_DAYS trading days) that the strategy's
    own signals in `bars` before `today` caused, as if every signal was taken; a loss closes past entry on the index.
    None if they never lost LOSS_STREAK in a row."""
    by_day: dict[str, list[dict]] = {}
    for b in bars:
        if b["date"][:10] < today:
            by_day.setdefault(b["date"][:10], []).append(b)
    streak, until, off = 0, None, holidays(settings)
    for d in sorted(by_day):
        day, prev = date.fromisoformat(d), [x for x in daily if x["date"][:10] < d]
        if (until and day <= until) or not prev or weekly_expiry(day, settings) == day:
            continue
        for s in scan(by_day[d], above, prev[-1]["high"], prev[-1]["low"]):
            price = exit_of(by_day[d], s)[1]
            streak = streak + 1 if (price - s["entry"]) * (1 if s["side"] == "Long" else -1) < 0 else 0
            if streak == LOSS_STREAK:
                streak, until, left = 0, day, BREAK_DAYS
                while left:
                    until += timedelta(days=1)
                    left -= until.weekday() < 5 and until.isoformat() not in off
                break
    return until


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.liquidity_signals")
    parser.add_argument("--once", action="store_true", help="check once and exit (default: every 5 min until 14:30)")
    args = parser.parse_args(argv)
    settings = get_settings()
    if not alerts.signal_channels(settings):
        print("no alert channel: set SIGNALS_TELEGRAM_CHAT_ID (or ALERT_TELEGRAM_CHAT_ID) in .env (printing only)")
    day = datetime.now(IST).date()
    if weekly_expiry(day, settings) == day:
        note = f"No NIFTY trades today ({day}): expiry day."
        print(note, "| sent via", alerts.send_signal(settings, "[genesis] liquidity strategy", note) or "nothing")
        return 0
    try:
        until = break_until(*session_data("60d"), settings, day.isoformat())
    except httpx.HTTPError as exc:
        print(f"losing-streak check skipped, market data unavailable: {exc}")
        until = None
    if until and day <= until:
        note = f"No NIFTY trades today ({day}): {LOSS_STREAK} losses in a row, break until {until}."
        print(note, "| sent via", alerts.send_signal(settings, "[genesis] liquidity strategy", note) or "nothing")
        return 0
    seen: set[str] = set()
    while True:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        try:
            bars, above, daily = session_data()
        except httpx.HTTPError as exc:
            print(f"{now:%H:%M} market data unavailable, retrying: {exc}")
            bars, above, daily = None, {}, []
        done = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        day = [b for b in bars or [] if b["date"][:10] == today and b["date"] <= done]
        prev = [d for d in daily if d["date"][:10] < today]
        if day and prev:
            pdh, pdl = prev[-1]["high"], prev[-1]["low"]
            if "levels" not in seen:
                seen.add("levels")
                levels = (
                    f"🌅 NIFTY MORNING PLAN ({today})\n\n"
                    f"📊 Key Reference Levels:\n"
                    f"• Previous Day High (PDH): {pdh:,.2f}\n"
                    f"• Previous Day Low (PDL): {pdl:,.2f}\n"
                    f"• Day Open: {day[0]['open']:,.2f}\n"
                    f"• Active Entry Window: {FIRST_ENTRY} - {LAST_ENTRY} IST\n\n"
                    f"🎯 TRIGGERS TO WAIT FOR TODAY:\n\n"
                    f"🔴 SHORT (Buy PE):\n"
                    f"1. NIFTY sweeps above PDH ({pdh:,.0f})\n"
                    f"2. 15m candle rejects & closes back below {pdh:,.0f}\n"
                    f"3. 5m structure break below swing low + close below VWAP & Open ({day[0]['open']:,.0f})\n"
                    f"4. Breakdown below ascending trendline (Trendline Break)\n\n"
                    f"🟢 LONG (Buy CE):\n"
                    f"1. NIFTY sweeps below PDL ({pdl:,.0f})\n"
                    f"2. 15m candle closes above last 15m swing high\n"
                    f"3. 5m close reclaims PDL + close above VWAP & Open ({day[0]['open']:,.0f})\n"
                    f"4. Breakout above descending trendline (Trendline Break)"
                )
                print(levels, "| sent via", alerts.send_signal(settings, "[genesis] NIFTY Morning Plan", levels) or "nothing")
                lvls = trading_levels([prev[-1]], session_over=True) + intraday_levels(day, 3)
                near = [
                    (lv["label"], lv["price"], "#f57c00" if lv["label"].startswith("PD") else "#5c6bc0", ":")
                    for lv in lvls if min(b["low"] for b in day) - 50 <= lv["price"] <= max(b["high"] for b in day) + 50
                ]
                piv = pivots(day, "low")
                tl = (piv[-2], day[piv[-2]]["low"], piv[-1], day[piv[-1]]["low"]) if len(piv) >= 2 else None
                chart_png = chart(
                    day,
                    [*near, ("Open", day[0]["open"], "#9e9e9e", ":")],
                    f"NIFTY 5m Structure & Key Levels ({today})",
                    trendline=tl,
                )
                alerts.send_signal_photo(
                    settings,
                    f"📊 NIFTY 5m Levels & Structure ({today})\n• PDH: {pdh:,.2f} | PDL: {pdl:,.2f}\n• Day High: {max(b['high'] for b in day):,.2f} | Low: {min(b['low'] for b in day):,.2f}",
                    chart_png,
                )
            # 1. PDH / PDL Liquidity Sweeps with Trendline Breakdown
            all_signals = scan(day, above, pdh, pdl, require_trendline=True)
            for s in all_signals:
                s.setdefault("pool", f"PD{'L' if s['side'] == 'Long' else 'H'}")

            # 2. Equal High / Equal Low (EQH / EQL) Intraday Sweeps
            from app.intraday_liquidity import atr, equal_levels, scan_levels
            atrs = atr(day)
            eq_lvs = equal_levels(day, atrs)
            if eq_lvs:
                tot_p = 0.0
                v_list = []
                for n_idx, b_item in enumerate(day, 1):
                    tot_p += (b_item["high"] + b_item["low"] + b_item["close"]) / 3.0
                    v_list.append(tot_p / n_idx)
                for eq_s in scan_levels(day, v_list, atrs, eq_lvs, "equal", min_sweep=2.0, with_open_only=True, require_trendline=True):
                    eq_s["pool"] = f"Equal {'High (EQH)' if eq_s['side'] == 'Short' else 'Low (EQL)'}"
                    all_signals.append(eq_s)

            for s in all_signals:
                sig_key = f"{s.get('strategy', 'pd')}_{s['side']}_{s.get('level', '')}"
                if sig_key in seen:
                    continue
                seen.add(sig_key)
                if now - datetime.strptime(s["time"], "%Y-%m-%d %H:%M").replace(tzinfo=IST) > FRESH:
                    print(f"missed (over {FRESH.seconds // 60} min old, not sent):\n{message(s, settings)}")
                    continue
                plan = option_plan(s, settings)
                lvls = trading_levels([prev[-1]], session_over=True) + intraday_levels(day, 3)
                body = message(s, settings, plan, lvls)
                print(body, "| sent via", alerts.send_signal(settings, f"[genesis] NIFTY {s['side'].upper()} signal", body) or "nothing")
                pngs = charts(day, s, lvls, plan, settings)
                if plan and len(pngs) >= 2:
                    stop_p, target_p = plan["exits"][0]["premium"], plan["exits"][1]["premium"]
                    opt_t1 = round(plan["entry_premium"] + (target_p - plan["entry_premium"]) / 3, 2)
                    opt_t2 = round(plan["entry_premium"] + 2 * (target_p - plan["entry_premium"]) / 3, 2)
                    opt_caption = (
                        f"🎯 NIFTY {plan['option']} (Expiry: {plan['expiry']})\n"
                        f"Action: BUY | CMP: ~{plan['entry_premium']:,.2f}\n"
                        f"Stop Loss: {stop_p:,.2f} (-20% / Index SL: {s['stop']:,.2f})\n\n"
                        f"🎯 Staged Targets (2-3 Lots):\n"
                        f"• T1 ({opt_t1:,.2f}): Move SL to Cost (Risk-Free)\n"
                        f"• T2 ({opt_t2:,.2f}): Book 50% Lots (Lock Profit)\n"
                        f"• T3 ({target_p:,.2f}): Book Remaining Runner (3R)\n\n"
                        f"📊 Risk & Sizing (Rs 50k Capital):\n"
                        f"• 2 Lots (130 qty): ~Rs {-plan['exits'][0]['pnl']:,.0f} total risk (5.2% risk rule)\n"
                        f"• 3 Lots (195 qty): ~Rs {-plan['exits'][0]['pnl'] * 1.5:,.0f} total risk (7.8% risk rule)\n"
                        f"• Time Exit: {EXIT} if neither hit\n\n"
                        f"💡 Setup: {s['side']} liquidity sweep at {s['time'][11:]} IST"
                    )
                    alerts.send_signal_photo(settings, opt_caption[:1024], pngs[0])
                    nifty_caption = (
                        f"📊 NIFTY 5m Structure & Levels ({today})\n"
                        f"• Setup: {s['side'].upper()} at {s['time'][11:]} IST\n"
                        f"• Index Entry: {s['entry']:,.2f} | Stop: {s['stop']:,.2f} | Target: {s['target']:,.2f}\n"
                        f"• Risk: {s['risk']:.0f} pts | Reward: {abs(s['target'] - s['entry']):.0f} pts (1:3 R:R)"
                    )
                    alerts.send_signal_photo(settings, nifty_caption[:1024], pngs[1])
                else:
                    for png in pngs:
                        alerts.send_signal_photo(settings, body.splitlines()[0], png)
        elif bars is not None and now.strftime("%H:%M") >= "10:00":
            print(f"{now:%H:%M} no NIFTY bars for {today} (holiday or data source down)")
            break
        if args.once or now.strftime("%H:%M") > LAST_ENTRY:
            break
        wake = now.replace(second=0, microsecond=0) + timedelta(minutes=5 - now.minute % 5, seconds=60)
        time.sleep((wake - now).total_seconds())
    return 0


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())
