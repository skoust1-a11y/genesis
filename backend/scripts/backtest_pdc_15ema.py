"""Backtest of the Instagram Reel Strategy: Previous Day Close (PDC) 5m Zone + 15 EMA.

Strategy Mechanics:
1. Identify the last 5-minute candle of the previous trading day (15:25 - 15:30).
   Zone = [Low of 15:25 candle, High of 15:25 candle].
2. Intraday triggers:
   a) Bullish Rejection (Long CE):
      - Price tests the PDC zone from above (low touches/dips into zone).
      - Rejection candle does not close below the zone low (close >= zone_low).
      - Candle closes above 15 EMA (or next candle confirms above 15 EMA).
      - Buy ATM/OTM Call option. Stop Loss placed below zone low.
      - Trail with 15 EMA: hold as long as 5m close remains above 15 EMA.
   b) Bearish Breakdown (Short PE):
      - Price breaks down and candle gives a solid close below the zone low (close < zone_low).
      - Candle closes below 15 EMA.
      - Buy ATM/OTM Put option. Stop Loss placed above zone high.
      - Trail with 15 EMA: hold as long as 5m close remains below 15 EMA.
3. Option Pricing:
   - Black-76 model with exact historical India VIX and time to weekly expiry.
   - Full NSE & regulatory order charges (STT, stamp duty, brokerage, exchange turnover, GST).
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.market import IST, _chart, parse_yahoo_chart
from app.config import Settings, get_settings
from app.market.hours import holidays
from app.market.nse import load_lot_sizes, lot_size_on
from app.paper import order_charges
from app.trading.option_pnl import black76
from scripts.backtest_liquidity import (
    WEEKLY_FROM,
    TUESDAY_FROM,
    expiry_for,
    groww_5m,
    kaggle_5m,
    option_exit,
    yahoo_data,
)


def compute_ema(series: list[float], period: int = 15) -> list[float]:
    """Calculate Exponential Moving Average for a series of prices."""
    if not series:
        return []
    alpha = 2.0 / (period + 1)
    ema = [series[0]]
    for price in series[1:]:
        ema.append(ema[-1] + alpha * (price - ema[-1]))
    return ema


def find_pdc_zone(prev_day_bars: list[dict]) -> tuple[float, float] | None:
    """Extract High and Low of the last 5-minute candle of the day (15:25 bar)."""
    if not prev_day_bars:
        return None
    # 15:25 bar starts at 15:25
    last_5m = None
    for b in reversed(prev_day_bars):
        if b["date"][11:16] in ("15:25", "15:20"):
            last_5m = b
            break
    if not last_5m:
        last_5m = prev_day_bars[-1]
    
    zone_high = float(last_5m["high"])
    zone_low = float(last_5m["low"])
    # If the candle is too narrow (< 6 pts), add a minor buffer
    if zone_high - zone_low < 6.0:
        mid = (zone_high + zone_low) / 2
        zone_high = mid + 3.0
        zone_low = mid - 3.0
    return zone_high, zone_low


def scan_pdc_trades(
    bars: list[dict],
    pdc_high: float,
    pdc_low: float,
    pdh: float,
    pdl: float,
    settings: Settings,
    iv_pct: float,
    expiry: date,
    lots: int = 1,
    otm_steps: int = 1,
    target_rr: float = 2.5,
    allow_short: bool = True,
    use_ema_exit: bool = True,
) -> list[dict]:
    """Scan intraday 5m bars for PDC zone rejection or breakdown setups."""
    if len(bars) < 5:
        return []

    # Calculate 15 EMA
    closes = [b["close"] for b in bars]
    ema15 = compute_ema(closes, period=15)

    signals = []
    in_trade_until = -1
    d_str = bars[0]["date"][:10]
    step = settings.strike_step("NIFTY") or 50

    for i in range(2, len(bars) - 4):  # skip first 10m and last 20m
        if i <= in_trade_until:
            continue

        b = bars[i]
        t_str = b["date"][11:16]
        if t_str < "09:25" or t_str > "14:45":
            continue

        bar_ema = ema15[i]
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]

        # --- Setup 1: Bullish Rejection (CE Long) ---
        # Price tests into or near the zone, does not close below pdc_low, and closes above 15 EMA
        buffer = 4.0
        tested_zone = (l <= pdc_high + buffer) and (l >= pdc_low - buffer * 2)
        respected_zone = c >= pdc_low
        bullish_close = c > o and c > bar_ema

        if tested_zone and respected_zone and bullish_close:
            entry = c
            stop = min(pdc_low, l) - 4.0
            risk = entry - stop
            if 10.0 <= risk <= 65.0:  # reasonable risk boundary
                # Target: either PDH or fixed target_rr
                target = max(entry + risk * target_rr, pdh) if pdh > entry + risk else entry + risk * target_rr
                strike = round(entry / step) * step + (otm_steps * step)
                
                # Check outcome on subsequent bars
                exit_reason = "15:15 Squareoff"
                exit_price = bars[-1]["close"]
                exit_time = bars[-1]["date"]
                exit_idx = len(bars) - 1

                for j in range(i + 1, len(bars)):
                    jb = bars[j]
                    j_ema = ema15[j]

                    # Stop hit?
                    if jb["low"] <= stop:
                        exit_reason = "Stop Loss"
                        exit_price = stop
                        exit_time = jb["date"]
                        exit_idx = j
                        break
                    # Target hit?
                    if jb["high"] >= target:
                        exit_reason = "Target"
                        exit_price = target
                        exit_time = jb["date"]
                        exit_idx = j
                        break
                    # 15 EMA Trailing Exit
                    if use_ema_exit and jb["close"] < j_ema and j > i + 1:
                        exit_reason = "EMA15 Exit"
                        exit_price = jb["close"]
                        exit_time = jb["date"]
                        exit_idx = j
                        break

                points = exit_price - entry
                in_trade_until = exit_idx

                # Plan option
                plan = {
                    "option": f"{strike:g} CE",
                    "expiry": expiry.isoformat(),
                    "iv_pct": iv_pct,
                }
                # Time to expiry
                exp_dt = datetime.combine(expiry, dtime(15, 30), IST)
                entry_dt = datetime.strptime(b["date"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                left_entry = max((exp_dt - entry_dt).total_seconds(), 0) / 86400 / 365
                premium_in = black76(entry, strike, left_entry, iv_pct / 100, "CE")
                premium_out = option_exit(plan, exit_price, exit_time, settings)

                signals.append({
                    "date": d_str,
                    "side": "Long (CE)",
                    "entry_time": t_str,
                    "exit_time": exit_time[11:16],
                    "entry_index": entry,
                    "exit_index": exit_price,
                    "stop_index": stop,
                    "target_index": target,
                    "risk_pts": round(risk, 1),
                    "index_points": round(points, 1),
                    "r_multiple": round(points / risk, 2),
                    "exit_reason": exit_reason,
                    "option": f"{strike:g} CE",
                    "premium_in": round(premium_in, 2),
                    "premium_out": round(premium_out, 2),
                    "iv": round(iv_pct, 1),
                })
                continue

        # --- Setup 2: Bearish Breakdown (PE Short) ---
        if allow_short:
            # Breakdown candle closes cleanly below pdc_low and below 15 EMA
            breakdown_close = c < pdc_low and c < bar_ema and (o - c) >= 3.0
            prior_above = bars[i - 1]["close"] >= pdc_low or bars[i - 2]["close"] >= pdc_low

            if breakdown_close and prior_above:
                entry = c
                stop = max(pdc_high, h) + 4.0
                risk = stop - entry
                if 10.0 <= risk <= 65.0:
                    target = min(entry - risk * target_rr, pdl) if pdl < entry - risk else entry - risk * target_rr
                    strike = round(entry / step) * step - (otm_steps * step)

                    exit_reason = "15:15 Squareoff"
                    exit_price = bars[-1]["close"]
                    exit_time = bars[-1]["date"]
                    exit_idx = len(bars) - 1

                    for j in range(i + 1, len(bars)):
                        jb = bars[j]
                        j_ema = ema15[j]

                        # Stop hit?
                        if jb["high"] >= stop:
                            exit_reason = "Stop Loss"
                            exit_price = stop
                            exit_time = jb["date"]
                            exit_idx = j
                            break
                        # Target hit?
                        if jb["low"] <= target:
                            exit_reason = "Target"
                            exit_price = target
                            exit_time = jb["date"]
                            exit_idx = j
                            break
                        # 15 EMA Trailing Exit
                        if use_ema_exit and jb["close"] > j_ema and j > i + 1:
                            exit_reason = "EMA15 Exit"
                            exit_price = jb["close"]
                            exit_time = jb["date"]
                            exit_idx = j
                            break

                    points = entry - exit_price
                    in_trade_until = exit_idx

                    plan = {
                        "option": f"{strike:g} PE",
                        "expiry": expiry.isoformat(),
                        "iv_pct": iv_pct,
                    }
                    exp_dt = datetime.combine(expiry, dtime(15, 30), IST)
                    entry_dt = datetime.strptime(b["date"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                    left_entry = max((exp_dt - entry_dt).total_seconds(), 0) / 86400 / 365
                    premium_in = black76(entry, strike, left_entry, iv_pct / 100, "PE")
                    premium_out = option_exit(plan, exit_price, exit_time, settings)

                    signals.append({
                        "date": d_str,
                        "side": "Short (PE)",
                        "entry_time": t_str,
                        "exit_time": exit_time[11:16],
                        "entry_index": entry,
                        "exit_index": exit_price,
                        "stop_index": stop,
                        "target_index": target,
                        "risk_pts": round(risk, 1),
                        "index_points": round(points, 1),
                        "r_multiple": round(points / risk, 2),
                        "exit_reason": exit_reason,
                        "option": f"{strike:g} PE",
                        "premium_in": round(premium_in, 2),
                        "premium_out": round(premium_out, 2),
                        "iv": round(iv_pct, 1),
                    })

    return signals


def run_option_backtest(
    bars: list[dict],
    daily: list[dict],
    vix: dict[str, float],
    settings: Settings,
    capital: float = 100_000.0,
    lots: int = 1,
    otm_steps: int = 1,
    target_rr: float = 2.5,
    allow_short: bool = True,
    use_ema_exit: bool = True,
    since: str = "",
) -> dict:
    """Run simulation across all available days and build performance ledger."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_day[b["date"][:10]].append(b)

    daily_map = {d["date"][:10]: d for d in daily}
    daily_dates = sorted(daily_map)
    vix_dates = sorted(vix)
    lots_table = load_lot_sizes(Path(settings.data_dir) / "lot_sizes" / "nse.yaml")
    trading_days = sorted(k for k in by_day if since <= k)

    all_trades = []

    for idx, d in enumerate(trading_days):
        if idx == 0:
            continue
        prev_d = trading_days[idx - 1]
        prev_bars = by_day[prev_d]
        curr_bars = by_day[d]

        # PDC zone
        pdc = find_pdc_zone(prev_bars)
        if not pdc:
            continue
        pdc_high, pdc_low = pdc

        # Prev Day High / Low
        prev_daily = daily_map.get(prev_d)
        if not prev_daily:
            prev_daily = {
                "high": max(b["high"] for b in prev_bars),
                "low": min(b["low"] for b in prev_bars),
            }

        # VIX
        v_idx = bisect_left(vix_dates, d)
        iv = vix[vix_dates[v_idx - 1]] if v_idx > 0 else 15.0

        # Expiry
        expiry = expiry_for(date.fromisoformat(d), set(trading_days), settings)
        if expiry.isoformat() == d:
            continue  # skip expiry day 0DTE gamma spikes

        day_trades = scan_pdc_trades(
            curr_bars,
            pdc_high,
            pdc_low,
            prev_daily["high"],
            prev_daily["low"],
            settings,
            iv,
            expiry,
            lots=lots,
            otm_steps=otm_steps,
            target_rr=target_rr,
            allow_short=allow_short,
            use_ema_exit=use_ema_exit,
        )

        for t in day_trades:
            lot_sz = lot_size_on(lots_table, "NIFTY", d) or 75
            kind = "CE" if "CE" in t["option"] else "PE"
            qty = lots * lot_sz
            gross_pnl = (t["premium_out"] - t["premium_in"]) * qty
            buy_chg = order_charges(settings, kind, "BUY", t["premium_in"], qty, d)["total"]
            sell_chg = order_charges(settings, kind, "SELL", t["premium_out"], qty, d)["total"]
            charges = buy_chg + sell_chg
            net_pnl = gross_pnl - charges

            t["lot_size"] = lot_sz
            t["quantity"] = qty
            t["gross_pnl"] = round(gross_pnl, 2)
            t["charges"] = round(charges, 2)
            t["net_pnl"] = round(net_pnl, 2)
            t["capital_required"] = round(t["premium_in"] * qty, 2)
            all_trades.append(t)

    # Performance metrics
    if not all_trades:
        return {"trades": [], "summary": {"total_trades": 0}}

    total_trades = len(all_trades)
    winning_trades = [t for t in all_trades if t["net_pnl"] > 0]
    losing_trades = [t for t in all_trades if t["net_pnl"] <= 0]
    win_rate = (len(winning_trades) / total_trades) * 100

    total_gross = sum(t["gross_pnl"] for t in all_trades)
    total_charges = sum(t["charges"] for t in all_trades)
    total_net = sum(t["net_pnl"] for t in all_trades)

    gross_wins = sum(t["gross_pnl"] for t in winning_trades)
    gross_losses = abs(sum(t["gross_pnl"] for t in losing_trades))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else math.inf

    # Drawdown & equity curve
    equity = capital
    peak = capital
    max_dd = 0.0
    equity_curve = []
    yearly: dict[str, list[float]] = defaultdict(list)

    for t in all_trades:
        equity += t["net_pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        equity_curve.append(equity)
        yr = t["date"][:4]
        yearly[yr].append(t["net_pnl"])

    max_dd_pct = round((max_dd / peak) * 100, 2) if peak > 0 else 0.0
    total_return_pct = round((total_net / capital) * 100, 2)

    yearly_summary = {}
    for yr, pnls in sorted(yearly.items()):
        yr_net = sum(pnls)
        yr_wins = sum(1 for p in pnls if p > 0)
        yearly_summary[yr] = {
            "trades": len(pnls),
            "win_rate": round(yr_wins / len(pnls) * 100, 1),
            "net_pnl": round(yr_net, 2),
        }

    return {
        "trades": all_trades,
        "summary": {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2),
            "total_gross_pnl": round(total_gross, 2),
            "total_charges": round(total_charges, 2),
            "total_net_pnl": round(total_net, 2),
            "profit_factor": profit_factor,
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": max_dd_pct,
            "total_return_pct": total_return_pct,
            "final_equity": round(equity, 2),
            "avg_trade_pnl": round(total_net / total_trades, 2),
        },
        "yearly": yearly_summary,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest Instagram PDC + 15 EMA Strategy")
    parser.add_argument("--history", type=Path, default=Path("data/intraday/NIFTY_1MIN_2015-2025.csv"))
    parser.add_argument("--years", type=float, default=5.0)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--otm-steps", type=int, default=1)
    parser.add_argument("--target-rr", type=float, default=2.5)
    parser.add_argument("--no-short", action="store_true", help="Only take Long CE trades")
    parser.add_argument("--no-ema-exit", action="store_true", help="Don't use 15 EMA trailing exit")
    args = parser.parse_args()

    settings = get_settings()

    if args.history and args.history.exists():
        print(f"Loading data from historical CSV: {args.history} ...")
        bars, _, daily = kaggle_5m(args.history), {}, []
        daily_dict = {}
        for b in bars:
            d = b["date"][:10]
            if d not in daily_dict:
                daily_dict[d] = {"date": d, "high": b["high"], "low": b["low"]}
            else:
                daily_dict[d]["high"] = max(daily_dict[d]["high"], b["high"])
                daily_dict[d]["low"] = min(daily_dict[d]["low"], b["low"])
        daily = list(daily_dict.values())
    else:
        print("Loading data from Yahoo Finance (last 60 days)...")
        bars, _, daily = yahoo_data()

    since = (date.today() - timedelta(days=round(365.25 * args.years))).isoformat()
    vix_range = f"{int(args.years) + 1}y"
    try:
        vix = {
            d["date"][:10]: d["close"]
            for d in parse_yahoo_chart(_chart("^INDIAVIX", "1d", vix_range))
            if d.get("close")
        }
    except Exception as e:
        print(f"Warning: could not fetch India VIX ({e}), using default 15.0")
        vix = {}

    print(f"\n=======================================================")
    print(f"BACKTESTING: PDC 5m Zone + 15 EMA Option Strategy")
    print(f"Parameters: Years={args.years}, Capital=₹{args.capital:,.0f}, Lots={args.lots}")
    print(f"Target R:R={args.target_rr}, 15 EMA Exit={'ON' if not args.no_ema_exit else 'OFF'}, Sides={'Long & Short' if not args.no_short else 'Long Only'}")
    print(f"=======================================================\n")

    res = run_option_backtest(
        bars,
        daily,
        vix,
        settings,
        capital=args.capital,
        lots=args.lots,
        otm_steps=args.otm_steps,
        target_rr=args.target_rr,
        allow_short=not args.no_short,
        use_ema_exit=not args.no_ema_exit,
        since=since,
    )

    summary = res["summary"]
    if summary["total_trades"] == 0:
        print("No trades generated.")
        return

    print("--- PERFORMANCE SUMMARY ---")
    print(f"Total Trades:       {summary['total_trades']}")
    print(f"Win Rate:           {summary['win_rate']}%")
    print(f"Profit Factor:      {summary['profit_factor']}")
    print(f"Gross Option PnL:   ₹{summary['total_gross_pnl']:,.2f}")
    print(f"Total NSE Charges:  ₹{summary['total_charges']:,.2f}")
    print(f"Net Option PnL:     ₹{summary['total_net_pnl']:,.2f}")
    print(f"Total Return:       {summary['total_return_pct']}% on ₹{args.capital:,.0f}")
    print(f"Max Drawdown:       ₹{summary['max_drawdown']:,.2f} ({summary['max_drawdown_pct']}%)")
    print(f"Avg PnL / Trade:    ₹{summary['avg_trade_pnl']:,.2f}")

    print("\n--- YEARLY BREAKDOWN ---")
    print(f"{'Year':<8} {'Trades':<10} {'Win Rate':<12} {'Net PnL (₹)':<15}")
    print("-" * 45)
    for yr, stat in res["yearly"].items():
        print(f"{yr:<8} {stat['trades']:<10} {stat['win_rate']:<11}% ₹{stat['net_pnl']:>12,.2f}")

    # Recent 10 sample trades
    print("\n--- RECENT 10 TRADES ---")
    for t in res["trades"][-10:]:
        print(
            f"{t['date']} {t['entry_time']} -> {t['exit_time']} | {t['side']:<11} | {t['option']} "
            f"| In: ₹{t['premium_in']:>6.2f} Out: ₹{t['premium_out']:>6.2f} "
            f"| Net PnL: ₹{t['net_pnl']:>8.2f} ({t['exit_reason']})"
        )


if __name__ == "__main__":
    main()
