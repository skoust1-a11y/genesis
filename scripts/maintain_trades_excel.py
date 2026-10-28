"""Maintain Strategy-Wise Excel Journal for Genesis Suggested Trades.

Generates and maintains:
1. Master Workbook: trades/suggested_trades_by_strategy.xlsx
   - Tab 1: Strategy Dashboard (Comparative Metrics, Win Rates, Total R, Profit Factor)
   - Tab 2: All Suggested Trades (Chronological Master Ledger)
   - Tab 3: Trendline Reversal
   - Tab 4: Breakout & Retest
   - Tab 5: Liquidity Sweep
2. Individual Strategy Workbooks:
   - trades/by_strategy/trendline_reversal.xlsx
   - trades/by_strategy/breakout_and_retest.xlsx
   - trades/by_strategy/liquidity_sweep.xlsx
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
TRADES_DIR = ROOT_DIR / "trades"
STRATEGY_DIR = TRADES_DIR / "by_strategy"

sys.path.insert(0, str(BACKEND_DIR))

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.api.market import IST, _chart, add_vwap, parse_yahoo_chart
from app.config import Settings, get_settings
from app.liquidity_signals import (
    EXIT,
    INDEX,
    OTM_STEPS,
    VWAP_PROXY,
    estimated_plan,
    scan,
    scan_retest,
    scan_trendline_reversal,
    staged_exit_of,
    weekly_expiry,
)
from app.paper import order_charges
from app.trading.option_pnl import black76


def get_market_data(days: int = 60) -> tuple[list[dict], dict[str, bool | None], list[dict]]:
    """Fetch 5m bars, VWAP proxy, and Daily bars from Yahoo Finance."""
    span = f"{days}d" if days <= 60 else "60d"
    bars_raw = parse_yahoo_chart(_chart(INDEX, "5m", span))
    proxy = add_vwap(parse_yahoo_chart(_chart(VWAP_PROXY, "5m", span)))
    above = {b["date"]: b["close"] > b["vwap"] if b.get("vwap") else None for b in proxy}
    bars = [b for b in bars_raw if None not in (b.get("open"), b.get("high"), b.get("low"))]
    daily = [d for d in parse_yahoo_chart(_chart(INDEX, "1d", "1y")) if d.get("high")]
    return bars, above, daily


def extract_all_suggested_trades(
    bars: list[dict],
    above: dict[str, bool | None],
    daily: list[dict],
    settings: Settings,
    lot_size: int = 65,
    lots: int = 2,
) -> list[dict]:
    """Scan all trading days for suggested trades across all 3 strategies."""
    by_date: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)

    dates = sorted(by_date.keys())
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Mapping daily levels
    daily_map = {d["date"][:10]: d for d in daily}
    vix_data = {
        d["date"][:10]: d["close"]
        for d in parse_yahoo_chart(_chart("^INDIAVIX", "1d", "1y"))
        if d.get("close")
    }

    all_trades: list[dict] = []

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]

        # Best estimate of PDH/PDL: use previous day's 5m extremes
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)
        day_open = cur_bars[0]["open"]
        is_today = cur_d == today_str

        # Expiry calculation: If today is expiry day, roll to next weekly contract
        # (institutional best practice so that positions don't expire within hours)
        d_obj = date.fromisoformat(cur_d)
        expiry = weekly_expiry(d_obj, settings)
        if expiry <= d_obj:
            next_day = d_obj + timedelta(days=1)
            expiry = weekly_expiry(next_day, settings)
            while expiry <= d_obj:
                next_day += timedelta(days=1)
                expiry = weekly_expiry(next_day, settings)

        # Implied Volatility
        iv_pct = vix_data.get(prev_d, 14.5)

        # Evaluate Primary Institutional Structure on 5-min candles (consistent across all dates)
        bar_mins = 5
        eval_bars = cur_bars
        eval_above = above

        # Precompute key Spot levels for dynamic targets
        spot_levels = [pdh, pdl]
        if len(cur_bars) >= 6:
            spot_levels.extend([max(b["high"] for b in cur_bars[:6]), min(b["low"] for b in cur_bars[:6])])
        spot_levels = sorted(list(set(spot_levels)))

        # Collect raw signals
        day_signals: list[tuple[str, dict]] = []

        # 1. Trendline Reversal (CHoCH Confirmed + Dynamic Spot Target)
        for s in scan_trendline_reversal(
            eval_bars,
            skip_lunch="conditional",
            allow_reentry=True,
            bar_minutes=bar_mins,
            require_choch=True,
            max_risk=55.0,
            dynamic_targets=True,
            spot_levels=spot_levels,
        ):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                is_reentry = "Re-Entry" in s.get("pool", "")
                strat_label = f"Trendline Reversal{' (Re-Entry)' if is_reentry else ''}"
                day_signals.append((strat_label, s))

        # 2. Breakout & Retest (Grade A PDH/PDL + ORH/ORL + Dynamic Spot Target)
        for s in scan_retest(
            eval_bars,
            eval_above,
            pdh,
            pdl,
            skip_lunch="conditional",
            allow_reentry=False,
            bar_minutes=bar_mins,
            include_orh_orl=True,
            dynamic_targets=True,
            spot_levels=spot_levels,
        ):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                is_reentry = "Re-Entry" in s.get("pool", "")
                pool_desc = s.get("pool", "")
                is_orb = "OR" in pool_desc
                strat_label = f"Breakout & Retest ({'OR-30' if is_orb else 'PD'}){' (Re-Entry)' if is_reentry else ''}"
                day_signals.append((strat_label, s))

        # 3. Liquidity Sweep (Grade A PDH/PDL Sweep & Reclaim)
        if not is_today:
            for s in scan(cur_bars, above, pdh, pdl, skip_lunch="conditional", dynamic_targets=True, spot_levels=spot_levels):
                if 10.0 <= s.get("risk", 0) <= 55.0:
                    day_signals.append(("Liquidity Sweep", s))

        # Deduplicate signals at identical time
        unique_signals = []
        seen_times = set()
        for strat_name, sig in day_signals:
            if sig["time"] not in seen_times:
                seen_times.add(sig["time"])
                unique_signals.append((strat_name, sig))

        for strat_name, sig in unique_signals:
            entry_time = sig["time"]
            entry_spot = sig["entry"]
            stop_spot = sig["stop"]
            target_spot = sig["target"]
            risk_pts = sig["risk"]
            side = sig["side"]

            # Dynamic Sizing (Fixed ~₹2,200 - ₹2,500 Risk Budget)
            trade_lots = 1 if risk_pts > 40.0 else 2
            qty = trade_lots * lot_size

            # Evaluate Exit with Structure Trailing
            res = staged_exit_of(eval_bars, sig, bar_minutes=bar_mins, trail_mode="structure")
            exit_reason = res["exit_reason"]
            exit_price = res["exit_price"]
            exit_time = res["exit_time"]
            r_mult = res["r_multiple"]

            # If today and session is active, determine live status
            if is_today and exit_reason in ("End of Session", "Last bar"):
                latest_close = eval_bars[-1]["close"]
                cur_pts = latest_close - entry_spot if side == "Long" else entry_spot - latest_close
                cur_r = round(cur_pts / risk_pts, 2)
                exit_reason = "Active / Running"
                exit_price = latest_close
                exit_time = eval_bars[-1]["date"]
                r_mult = cur_r

            points = exit_price - entry_spot if side == "Long" else entry_spot - exit_price

            # Strike selection: ATM strike (nearest 50 points)
            step = settings.strike_step("NIFTY") or 50
            strike = round(entry_spot / step) * step
            kind = "CE" if side == "Long" else "PE"
            opt_contract = f"{strike:g} {kind}"

            # Rigorous Black-76 Option Valuation at Entry & Exit timestamps
            exp_dt = datetime.combine(expiry, dtime.fromisoformat(settings.market_close), IST)
            ent_dt = datetime.strptime(f"{cur_d} {entry_time[11:]}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            ext_dt = datetime.strptime(f"{cur_d} {exit_time[11:]}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)

            y_ent = max((exp_dt - ent_dt).total_seconds(), 300) / 86400 / 365
            y_ext = max((exp_dt - ext_dt).total_seconds(), 60) / 86400 / 365

            prem_in = round(black76(entry_spot, strike, y_ent, iv_pct / 100, kind), 2)
            prem_out = round(black76(exit_price, strike, y_ext, iv_pct / 100, kind), 2)

            capital_deployed = round(prem_in * qty, 0)
            exit_value = round(prem_out * qty, 0)

            # Statutory Order Charges
            buy_chg = order_charges(settings, kind, "BUY", prem_in, qty, cur_d)["total"]
            sell_chg = order_charges(settings, kind, "SELL", prem_out, qty, cur_d)["total"]
            tot_charges = round(buy_chg + sell_chg, 2)

            est_pnl_rs = round((prem_out - prem_in) * qty - tot_charges, 0)
            roi_pct = round((est_pnl_rs / capital_deployed) * 100, 1) if capital_deployed > 0 else 0.0

            if exit_reason == "Active / Running":
                status = f"ACTIVE ({r_mult:+.2f}R)"
            elif r_mult >= 2.5 or exit_reason == "Target":
                status = "TARGET HIT (3R)"
            elif r_mult > 0.1:
                status = f"TRAILED (+{r_mult:.2f}R)"
            elif r_mult <= -0.9 or "Stop" in exit_reason:
                status = "STOPPED OUT (-1R)"
            else:
                status = "FLAT"

            all_trades.append({
                "Date": cur_d,
                "Time": entry_time[11:],
                "Strategy": strat_name,
                "Pool / Setup": sig.get("pool", strat_name),
                "Side": side.upper(),
                "Status": status,
                "Entry Spot": round(entry_spot, 2),
                "Stop Spot": round(stop_spot, 2),
                "Target Spot": round(target_spot, 2),
                "Risk Pts": round(risk_pts, 2),
                "Exit Spot": round(exit_price, 2),
                "Exit Time": exit_time[11:],
                "Exit Reason": exit_reason,
                "Points": round(points, 2),
                "R-Multiple": round(r_mult, 2),
                "Option": f"NIFTY {opt_contract}",
                "Expiry": expiry.isoformat(),
                "Entry Premium": prem_in,
                "Exit Premium": prem_out,
                "Capital Deployed": capital_deployed,
                "Exit Value": exit_value,
                "Est P&L (Rs)": est_pnl_rs,
                "ROI %": roi_pct,
                "Lots": trade_lots,
                "Qty": qty,
                "Rationale": " | ".join(sig.get("why", [])),
            })

    # Sort reverse chronologically (latest trade first)
    all_trades.sort(key=lambda t: (t["Date"], t["Time"]), reverse=True)
    return all_trades


def calculate_metrics(trades: list[dict]) -> dict:
    """Compute performance KPIs for a given set of trades."""
    if not trades:
        return {
            "Total Trades": 0,
            "Wins": 0,
            "Losses": 0,
            "Win Rate %": 0.0,
            "Total R": 0.0,
            "Avg R": 0.0,
            "Total Points": 0.0,
            "Total Est P&L (Rs)": 0.0,
            "Profit Factor": 0.0,
        }

    closed = [t for t in trades if "ACTIVE" not in t["Status"]]
    if not closed:
        closed = trades

    wins = [t for t in closed if t["R-Multiple"] > 0]
    losses = [t for t in closed if t["R-Multiple"] < 0]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    tot_r = sum(t["R-Multiple"] for t in closed)
    avg_r = (tot_r / len(closed)) if closed else 0.0
    tot_pts = sum(t["Points"] for t in closed)
    tot_pnl = sum(t["Est P&L (Rs)"] for t in closed)

    gross_profit = sum(t["Est P&L (Rs)"] for t in wins)
    gross_loss = abs(sum(t["Est P&L (Rs)"] for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 1.0)

    return {
        "Total Trades": len(trades),
        "Wins": len(wins),
        "Losses": len(losses),
        "Win Rate %": round(win_rate, 1),
        "Total R": round(tot_r, 2),
        "Avg R": round(avg_r, 2),
        "Total Points": round(tot_pts, 1),
        "Total Est P&L (Rs)": round(tot_pnl, 0),
        "Profit Factor": round(profit_factor, 2),
    }


def style_worksheet(ws: openpyxl.worksheet.worksheet.Worksheet, is_dashboard: bool = False):
    """Apply professional, sleek institutional formatting."""
    navy_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )
    green_font = Font(name="Calibri", size=11, bold=True, color="15803D")
    red_font = Font(name="Calibri", size=11, bold=True, color="B91C1C")
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    if is_dashboard:
        # Dashboard custom styling handled separately
        return

    # Style Header
    ws.row_dimensions[1].height = 28
    for col_idx, cell in enumerate(ws[1], 1):
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Style Data Rows
    for row_idx, row in enumerate(ws.iter_rows(min_row=2), 2):
        ws.row_dimensions[row_idx].height = 20
        is_even = row_idx % 2 == 0
        for cell in row:
            cell.border = thin_border
            if is_even:
                cell.fill = zebra_fill
            cell.alignment = Alignment(vertical="center")

            # Center dates, times, sides, status, expiry
            header_val = ws.cell(1, cell.column).value or ""
            if header_val in ("Date", "Time", "Side", "Status", "Exit Time", "Lots", "Qty", "Expiry"):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif header_val in ("Entry Spot", "Stop Spot", "Target Spot", "Exit Spot", "Risk Pts", "Points", "Entry Premium", "Exit Premium"):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif header_val in ("Capital Deployed", "Exit Value", "Est P&L (Rs)"):
                cell.number_format = '"Rs "#,##0;[Red]-"Rs "#,##0;"Rs "0'
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if header_val == "Est P&L (Rs)" and cell.value and isinstance(cell.value, (int, float)):
                    cell.font = green_font if cell.value > 0 else (red_font if cell.value < 0 else Font(name="Calibri", size=11))
            elif header_val in ("ROI %",):
                cell.number_format = "+0.0%;-0.0%;0.0%"
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if cell.value and isinstance(cell.value, (int, float)):
                    cell.font = green_font if cell.value > 0 else (red_font if cell.value < 0 else Font(name="Calibri", size=11))
            elif header_val in ("R-Multiple",):
                cell.number_format = "+0.00;-0.00;0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if cell.value and isinstance(cell.value, (int, float)):
                    cell.font = green_font if cell.value > 0 else (red_font if cell.value < 0 else Font(name="Calibri", size=11))

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 65)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def create_dashboard_tab(ws: openpyxl.worksheet.worksheet.Worksheet, all_trades: list[dict]):
    """Create the executive KPI Dashboard comparing strategies side-by-side."""
    ws.title = "📊 Strategy Dashboard"
    ws.views.sheetView[0].showGridLines = True

    # Title Banner
    ws.merge_cells("B2:J2")
    ws["B2"] = "GENESIS TRADING STRATEGY LAB — PERFORMANCE JOURNAL"
    ws["B2"].font = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    ws["B2"].fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    ws["B2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 40

    ws["B3"] = f"Generated on {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')} | All Active Strategies Analyzed"
    ws["B3"].font = Font(name="Calibri", size=10, italic=True, color="64748B")

    # Strategies to analyze
    strats = ["Breakout & Retest", "Trendline Reversal", "Liquidity Sweep"]
    metrics_map = {
        "All Strategies Combined": calculate_metrics(all_trades),
        "Trendline Reversal": calculate_metrics([t for t in all_trades if "Trendline Reversal" in t["Strategy"]]),
        "Breakout & Retest": calculate_metrics([t for t in all_trades if "Breakout & Retest" in t["Strategy"]]),
        "Liquidity Sweep": calculate_metrics([t for t in all_trades if "Liquidity Sweep" in t["Strategy"]]),
    }

    # Table Header
    headers = ["Strategy Name", "Total Trades", "Wins", "Losses", "Win Rate %", "Total R-Multiple", "Avg R/Trade", "Total Points", "Est P&L (Rs)", "Profit Factor"]
    ws.row_dimensions[5].height = 26
    for c_idx, h in enumerate(headers, 2):
        cell = ws.cell(5, c_idx, h)
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    row_idx = 6
    for strat_name, m in metrics_map.items():
        ws.row_dimensions[row_idx].height = 24
        is_highlight = strat_name == "All Strategies Combined"
        bg_color = "F1F5F9" if is_highlight else ("FFFFFF" if row_idx % 2 == 0 else "F8FAFC")
        fill = PatternFill(start_color=bg_color, end_color=bg_color, fill_type="solid")

        vals = [
            strat_name,
            m["Total Trades"],
            m["Wins"],
            m["Losses"],
            f"{m['Win Rate %']:.1f}%",
            m["Total R"],
            m["Avg R"],
            m["Total Points"],
            m["Total Est P&L (Rs)"],
            m["Profit Factor"],
        ]

        for c_idx, val in enumerate(vals, 2):
            cell = ws.cell(row_idx, c_idx, val)
            cell.fill = fill
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=11, bold=is_highlight)

            if c_idx == 2:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif c_idx in (3, 4, 5, 6):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")

            if c_idx == 7:  # Total R
                cell.number_format = "+0.00;-0.00;0.00"
                if isinstance(val, (int, float)):
                    cell.font = Font(name="Calibri", size=11, bold=True, color="15803D" if val > 0 else "B91C1C")
            elif c_idx == 10:  # Est P&L
                cell.number_format = '"Rs "#,##0;[Red]-"Rs "#,##0;"Rs "0'
                if isinstance(val, (int, float)):
                    cell.font = Font(name="Calibri", size=11, bold=True, color="15803D" if val > 0 else "B91C1C")

        row_idx += 1

    # Key Insights Section
    row_idx += 2
    ws.cell(row_idx, 2, "📌 STRATEGY DISCIPLINE & EXECUTION RULES").font = Font(name="Calibri", size=12, bold=True, color="0F172A")
    rules = [
        "1. Risk Budget: Risk is strictly capped at 5% of trading capital per trade (~₹1,800 to ₹3,500 for 2 lots).",
        "2. Break-Even Trail: Once price achieves +1.0R gain, Stop Loss is moved to Entry (Zero Risk Runner).",
        "3. Partial Profit Booking: At +2.0R, book 50% lots and trail remaining lot candle-by-candle until 3R or reversal.",
        "4. Circuit Breaker: 3 consecutive losses = 3 trading days cooling off mandatory halt.",
        "5. Maximum Trades: Max 1 trade per direction per day (at most 2 trades per session).",
    ]
    for r in rules:
        row_idx += 1
        c = ws.cell(row_idx, 2, r)
        c.font = Font(name="Calibri", size=10, color="475569")

    # Column widths
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 28
    for col_char in ["C", "D", "E", "F", "G", "H", "I", "J", "K"]:
        ws.column_dimensions[col_char].width = 16


def build_strategy_sheet(wb: openpyxl.Workbook, title: str, trades: list[dict]):
    """Build and format a specific trade sheet."""
    ws = wb.create_sheet(title=title)
    if not trades:
        ws.append(["No trades recorded for this strategy"])
        return

    headers = list(trades[0].keys())
    ws.append(headers)
    for t in trades:
        ws.append(list(t.values()))
    style_worksheet(ws)


def save_strategy_workbooks(all_trades: list[dict]):
    """Generate master workbook and individual strategy workbooks."""
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Master Multi-Tab Workbook
    master_path = TRADES_DIR / "suggested_trades_by_strategy.xlsx"
    wb_master = openpyxl.Workbook()

    # Tab 1: Dashboard
    ws_dash = wb_master.active
    create_dashboard_tab(ws_dash, all_trades)

    # Tab 2: All Suggested Trades
    build_strategy_sheet(wb_master, "📋 All Suggested Trades", all_trades)

    # Tab 3: Trendline Reversal
    tl_trades = [t for t in all_trades if "Trendline Reversal" in t["Strategy"]]
    build_strategy_sheet(wb_master, "📈 Trendline Reversal", tl_trades)

    # Tab 4: Breakout & Retest
    retest_trades = [t for t in all_trades if "Breakout & Retest" in t["Strategy"]]
    build_strategy_sheet(wb_master, "🔄 Breakout & Retest", retest_trades)

    # Tab 5: Liquidity Sweep
    sweep_trades = [t for t in all_trades if "Liquidity Sweep" in t["Strategy"]]
    build_strategy_sheet(wb_master, "🎯 Liquidity Sweep", sweep_trades)

    wb_master.save(master_path)
    print(f"✅ Master Workbook saved: {master_path}")

    # 2. Individual Strategy Workbooks in trades/by_strategy/
    strategy_files = {
        "trendline_reversal.xlsx": ("Trendline Reversal", tl_trades),
        "breakout_and_retest.xlsx": ("Breakout & Retest", retest_trades),
        "liquidity_sweep.xlsx": ("Liquidity Sweep", sweep_trades),
    }

    for fname, (name, s_trades) in strategy_files.items():
        fpath = STRATEGY_DIR / fname
        wb_strat = openpyxl.Workbook()
        ws = wb_strat.active
        ws.title = name
        if s_trades:
            ws.append(list(s_trades[0].keys()))
            for t in s_trades:
                ws.append(list(t.values()))
            style_worksheet(ws)
        else:
            ws.append(["No trades recorded"])
        wb_strat.save(fpath)
        print(f"✅ Strategy Workbook saved: {fpath}")

    # Save Live & Forward Workbooks
    save_live_and_forward_workbooks(all_trades)

    # Create README in trades/
    readme_path = TRADES_DIR / "README.md"
    live_master_path = TRADES_DIR / "live_and_forward_trades.xlsx"
    today_path = TRADES_DIR / "todays_live_trades.xlsx"
    readme_content = f"""# Genesis Suggested Trades Journal

This folder maintains institutional Excel spreadsheets of all suggested trades generated by the Genesis Trading Engine, categorized **strategy-wise**.

## Files in this Directory

### 1. 🔴 Live & Forward Test Trades (Recent Deployment & Live Sessions)
* **[`live_and_forward_trades.xlsx`](file://{live_master_path})** (Dedicated Live & Forward Workbook)
  * **🔴 Live Dashboard:** Executive metrics for live testing (Win Rate, Total R, Profit Factor).
  * **📋 All Live & Forward Trades:** Master ledger of all trades since live deployment (October 15, 2026 onwards).
  * **📈 Trendline Reversal (Live)**
  * **🔄 Breakout & Retest (Live)**
  * **🎯 Liquidity Sweep (Live)**
* **[`todays_live_trades.xlsx`](file://{today_path})** (Today's Live Trades Only: real-time entries, stops, targets, option contracts, and live P&L).

### 2. 📊 Master Workbook (All 60-Day Historical & Live Trades)
* **[`suggested_trades_by_strategy.xlsx`](file://{master_path})**
  * **📊 Strategy Dashboard:** Side-by-side comparison of Win Rates, Total R-Multiples, Profit Factor, and Net P&L.
  * **📋 All Suggested Trades:** Full chronological trade ledger across all strategies.
  * **📈 Trendline Reversal**
  * **🔄 Breakout & Retest**
  * **🎯 Liquidity Sweep**

### 3. 📂 Individual Strategy Workbooks
* **[`by_strategy/`](file://{STRATEGY_DIR})**
  * [`live_trendline_reversal.xlsx`](file://{STRATEGY_DIR / "live_trendline_reversal.xlsx"})
  * [`live_breakout_and_retest.xlsx`](file://{STRATEGY_DIR / "live_breakout_and_retest.xlsx"})
  * [`live_liquidity_sweep.xlsx`](file://{STRATEGY_DIR / "live_liquidity_sweep.xlsx"})
  * [`trendline_reversal.xlsx`](file://{STRATEGY_DIR / "trendline_reversal.xlsx"})
  * [`breakout_and_retest.xlsx`](file://{STRATEGY_DIR / "breakout_and_retest.xlsx"})
  * [`liquidity_sweep.xlsx`](file://{STRATEGY_DIR / "liquidity_sweep.xlsx"})

## How to Refresh / Maintain

Run the automated maintenance script anytime to sync the latest trades:
```bash
python scripts/maintain_trades_excel.py
```
Or with custom days:
```bash
python scripts/maintain_trades_excel.py --days 60
```

Last updated: {datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")}
"""
    readme_path.write_text(readme_content)
    print(f"✅ README saved: {readme_path}")


def save_live_and_forward_workbooks(all_trades: list[dict]):
    """Generate separate dedicated Excel workbooks for Live and Forward trades only."""
    # Filter forward test trades (since live alerting started, October 15, 2026)
    forward_trades = [t for t in all_trades if t["Date"] >= "2026-10-15"]
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_trades = [t for t in all_trades if t["Date"] == today_str]

    # 1. Master Live & Forward Workbook
    live_master_path = TRADES_DIR / "live_and_forward_trades.xlsx"
    wb_live = openpyxl.Workbook()

    # Tab 1: Live Dashboard
    ws_dash = wb_live.active
    create_dashboard_tab(ws_dash, forward_trades)
    ws_dash.title = "🔴 Live Dashboard"
    ws_dash["B2"] = "GENESIS LIVE & FORWARD-TEST TRADES — PERFORMANCE JOURNAL"

    # Tab 2: All Live & Forward Trades
    build_strategy_sheet(wb_live, "📋 Live & Forward Trades", forward_trades)

    # Tab 3: Trendline Reversal (Live)
    tl_live = [t for t in forward_trades if t["Strategy"] == "Trendline Reversal"]
    build_strategy_sheet(wb_live, "📈 Trendline Reversal", tl_live)

    # Tab 4: Breakout & Retest (Live)
    retest_live = [t for t in forward_trades if t["Strategy"] == "Breakout & Retest"]
    build_strategy_sheet(wb_live, "🔄 Breakout & Retest", retest_live)

    # Tab 5: Liquidity Sweep (Live)
    sweep_live = [t for t in forward_trades if t["Strategy"] == "Liquidity Sweep"]
    build_strategy_sheet(wb_live, "🎯 Liquidity Sweep", sweep_live)

    wb_live.save(live_master_path)
    print(f"✅ Live & Forward Master Workbook saved: {live_master_path}")

    # 2. Today's Live Trades Only
    today_path = TRADES_DIR / "todays_live_trades.xlsx"
    wb_today = openpyxl.Workbook()
    ws_today = wb_today.active
    ws_today.title = f"Live Trades {today_str}"
    if today_trades:
        ws_today.append(list(today_trades[0].keys()))
        for t in today_trades:
            ws_today.append(list(t.values()))
        style_worksheet(ws_today)
    else:
        ws_today.append(["No trades recorded for today"])
    wb_today.save(today_path)
    print(f"✅ Today's Live Trades Workbook saved: {today_path}")

    # 3. Individual Strategy Live Files
    live_strategy_files = {
        "live_trendline_reversal.xlsx": ("Trendline Reversal (Live)", tl_live),
        "live_breakout_and_retest.xlsx": ("Breakout & Retest (Live)", retest_live),
        "live_liquidity_sweep.xlsx": ("Liquidity Sweep (Live)", sweep_live),
    }

    for fname, (name, s_trades) in live_strategy_files.items():
        fpath = STRATEGY_DIR / fname
        wb_strat = openpyxl.Workbook()
        ws = wb_strat.active
        ws.title = name
        if s_trades:
            ws.append(list(s_trades[0].keys()))
            for t in s_trades:
                ws.append(list(t.values()))
            style_worksheet(ws)
        else:
            ws.append(["No live trades recorded for this strategy"])
        wb_strat.save(fpath)
        print(f"✅ Strategy Live Workbook saved: {fpath}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Maintain strategy-wise Excel logs for Genesis suggested trades")
    parser.add_argument("--days", type=int, default=60, help="Number of days to backfill/analyze (default 60)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    print(f"Fetching market data for the last {args.days} days...")
    bars, above, daily = get_market_data(args.days)
    print(f"Analyzing suggested trades across {len(bars)} 5m candles...")
    trades = extract_all_suggested_trades(bars, above, daily, settings)
    print(f"Found {len(trades)} total suggested trades.")

    save_strategy_workbooks(trades)
    print("Done! All Excel logs successfully maintained.")


if __name__ == "__main__":
    main()
