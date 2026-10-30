"""SQLite Database Store for Genesis Live & Forward-Tested Trades.

Provides an institutional Single Source of Truth (SSOT) with:
1. Atomic, crash-resilient ACID transactions (WAL mode).
2. Strict schema typing and validation.
3. Concurrent multi-process access (Scanner, API, Excel Exporter).
4. Automated migration from JSON / Excel workbooks.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[3]
DB_PATH = ROOT_DIR / "data" / "trades.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Returns a SQLite connection configured for concurrent WAL access."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    """Initialize the trade store database schema and indices."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_and_forward_trades (
                trade_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                strategy TEXT NOT NULL CHECK(strategy IN (
                    'Breakout & Retest (OR-30)',
                    'Breakout & Retest (PD)',
                    'Trendline Reversal',
                    'Liquidity Sweep'
                )),
                setup TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                status TEXT NOT NULL,
                entry_spot REAL NOT NULL,
                stop_spot REAL NOT NULL,
                target_spot REAL NOT NULL,
                risk_pts REAL NOT NULL,
                exit_spot REAL,
                exit_time TEXT,
                exit_reason TEXT,
                points REAL DEFAULT 0.0,
                r_multiple REAL DEFAULT 0.0,
                option_contract TEXT NOT NULL,
                expiry TEXT NOT NULL,
                entry_premium REAL NOT NULL,
                exit_premium REAL DEFAULT 0.0,
                capital_deployed REAL NOT NULL,
                exit_value REAL DEFAULT 0.0,
                est_pnl_inr REAL DEFAULT 0.0,
                roi_pct REAL DEFAULT 0.0,
                lots INTEGER NOT NULL DEFAULT 4,
                qty INTEGER NOT NULL DEFAULT 260,
                rationale TEXT,
                source TEXT DEFAULT 'live',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON live_and_forward_trades(date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON live_and_forward_trades(strategy);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON live_and_forward_trades(status);")


def make_trade_id(date_str: str, time_str: str, strategy: str) -> str:
    """Generate a deterministic, unique trade ID."""
    clean_strat = strategy.replace(" ", "_").replace("&", "AND").replace("(", "").replace(")", "").replace("-", "")
    return f"{date_str.replace('-', '')}_{time_str.replace(':', '')}_{clean_strat}"


def upsert_trade(trade: dict[str, Any]) -> str:
    """Upsert a single trade into SQLite."""
    init_db()
    d_str = str(trade.get("Date") or trade.get("date") or "")[:10]
    t_str = str(trade.get("Time") or trade.get("time") or "")
    strat = str(trade.get("Strategy") or trade.get("strategy") or "")

    # Normalize strategy name
    if strat == "Breakout & Retest":
        pool = str(trade.get("Pool / Setup") or trade.get("setup") or "")
        strat = "Breakout & Retest (OR-30)" if "OR" in pool else "Breakout & Retest (PD)"

    trade_id = str(trade.get("trade_id") or make_trade_id(d_str, t_str, strat))

    side = str(trade.get("Side") or trade.get("side") or "LONG").upper()
    status = str(trade.get("Status") or trade.get("status") or "ACTIVE")
    setup = str(trade.get("Pool / Setup") or trade.get("setup") or strat)

    entry_spot = float(trade.get("Entry Spot") or trade.get("entry") or 0.0)
    stop_spot = float(trade.get("Stop Spot") or trade.get("stop") or 0.0)
    target_spot = float(trade.get("Target Spot") or trade.get("target") or 0.0)
    risk_pts = float(trade.get("Risk Pts") or trade.get("risk") or abs(entry_spot - stop_spot))

    exit_spot = float(trade.get("Exit Spot") or trade.get("exit_spot") or entry_spot)
    exit_time = str(trade.get("Exit Time") or trade.get("exit_time") or "")
    exit_reason = str(trade.get("Exit Reason") or trade.get("exit_reason") or "")

    points = float(trade.get("Points") or trade.get("points") or 0.0)
    r_mult = float(trade.get("R-Multiple") or trade.get("r_multiple") or 0.0)

    opt = str(trade.get("Option") or trade.get("option") or "")
    expiry = str(trade.get("Expiry") or trade.get("expiry") or "")
    entry_prem = float(trade.get("Entry Premium") or trade.get("entry_premium") or 0.0)
    exit_prem = float(trade.get("Exit Premium") or trade.get("exit_premium") or 0.0)

    lots = int(trade.get("Lots") or trade.get("lots") or 4)
    qty = int(trade.get("Qty") or trade.get("qty") or (lots * 65))
    capital = float(trade.get("Capital Deployed") or trade.get("capital_deployed") or (entry_prem * qty))
    exit_val = float(trade.get("Exit Value") or trade.get("exit_value") or (exit_prem * qty))
    pnl = float(trade.get("Est P&L (Rs)") or trade.get("est_pnl_inr") or (exit_val - capital))
    roi = float(trade.get("ROI %") or trade.get("roi_pct") or ((pnl / capital * 100) if capital > 0 else 0.0))
    rationale = str(trade.get("Rationale") or trade.get("rationale") or "")
    source = str(trade.get("source") or "live")

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO live_and_forward_trades (
                trade_id, date, time, strategy, setup, side, status,
                entry_spot, stop_spot, target_spot, risk_pts,
                exit_spot, exit_time, exit_reason, points, r_multiple,
                option_contract, expiry, entry_premium, exit_premium,
                capital_deployed, exit_value, est_pnl_inr, roi_pct,
                lots, qty, rationale, source, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, datetime('now')
            )
            ON CONFLICT(trade_id) DO UPDATE SET
                status = excluded.status,
                exit_spot = excluded.exit_spot,
                exit_time = excluded.exit_time,
                exit_reason = excluded.exit_reason,
                points = excluded.points,
                r_multiple = excluded.r_multiple,
                exit_premium = excluded.exit_premium,
                exit_value = excluded.exit_value,
                est_pnl_inr = excluded.est_pnl_inr,
                roi_pct = excluded.roi_pct,
                rationale = excluded.rationale,
                updated_at = datetime('now');
        """, (
            trade_id, d_str, t_str, strat, setup, side, status,
            entry_spot, stop_spot, target_spot, risk_pts,
            exit_spot, exit_time, exit_reason, points, r_mult,
            opt, expiry, entry_prem, exit_prem,
            capital, exit_val, pnl, roi,
            lots, qty, rationale, source
        ))
    return trade_id


def get_all_trades(since: str = "2026-10-15") -> list[dict[str, Any]]:
    """Retrieve trades filtered chronologically with frontend and API compatible keys."""
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT * FROM live_and_forward_trades
            WHERE date >= ?
            ORDER BY date DESC, time DESC;
        """, (since,))
        raw = [dict(r) for r in cursor.fetchall()]
        formatted = []
        for r in raw:
            d = dict(r)
            # Add UI aliases
            d["entry"] = d.get("entry_spot", 0.0)
            d["stop"] = d.get("stop_spot", 0.0)
            d["target"] = d.get("target_spot", 0.0)
            d["risk"] = d.get("risk_pts", 0.0)
            d["option"] = d.get("option_contract", "")
            formatted.append(d)
        return formatted


def compute_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate KPIs and strategy breakdowns directly from database rows."""
    if not trades:
        return {"metrics": {}, "strategy_metrics": {}, "trades": []}

    wins = [t for t in trades if t["r_multiple"] > 0.05]
    losses = [t for t in trades if t["r_multiple"] < -0.05]
    be = [t for t in trades if abs(t["r_multiple"]) <= 0.05]

    tot_trades = len(trades)
    win_rate = (len(wins) / tot_trades * 100) if tot_trades > 0 else 0.0
    win_rate_ex_be = (len(wins) / (len(wins) + len(losses)) * 100) if (len(wins) + len(losses)) > 0 else 0.0
    tot_r = sum(t["r_multiple"] for t in trades)
    tot_pts = sum(t["points"] for t in trades)
    tot_pnl = sum(t["est_pnl_inr"] for t in trades)
    tot_cap = sum(t["capital_deployed"] for t in trades)
    tot_exit = sum(t["exit_value"] for t in trades)
    avg_cap = round(tot_cap / tot_trades, 0) if tot_trades > 0 else 0.0
    tot_roi = round((tot_pnl / tot_cap) * 100, 1) if tot_cap > 0 else 0.0

    gross_profit = sum(t["est_pnl_inr"] for t in wins)
    gross_loss = abs(sum(t["est_pnl_inr"] for t in losses))
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else 1.0)

    strat_metrics: dict[str, Any] = {}
    strategies = sorted(set(t["strategy"] for t in trades))
    for s_name in strategies:
        s_trades = [t for t in trades if t["strategy"] == s_name]
        s_wins = [t for t in s_trades if t["r_multiple"] > 0.05]
        s_losses = [t for t in s_trades if t["r_multiple"] < -0.05]
        s_be = [t for t in s_trades if abs(t["r_multiple"]) <= 0.05]
        s_pnl = sum(t["est_pnl_inr"] for t in s_trades)
        s_r = sum(t["r_multiple"] for t in s_trades)
        s_cap = sum(t["capital_deployed"] for t in s_trades)
        strat_metrics[s_name] = {
            "trades": len(s_trades),
            "wins": len(s_wins),
            "losses": len(s_losses),
            "be": len(s_be),
            "win_rate": round(len(s_wins) / len(s_trades) * 100, 1) if s_trades else 0.0,
            "total_r": round(s_r, 2),
            "capital_deployed": round(s_cap, 0),
            "net_pnl_inr": round(s_pnl, 0),
            "roi_pct": round((s_pnl / s_cap) * 100, 1) if s_cap > 0 else 0.0,
        }

    return {
        "metrics": {
            "total_trades": tot_trades,
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(be),
            "win_rate": round(win_rate, 1),
            "win_rate_ex_be": round(win_rate_ex_be, 1),
            "total_r": round(tot_r, 2),
            "total_points": round(tot_pts, 1),
            "net_pnl_inr": round(tot_pnl, 0),
            "profit_factor": pf,
            "total_capital_deployed": round(tot_cap, 0),
            "total_exit_value": round(tot_exit, 0),
            "avg_trade_capital": avg_cap,
            "total_roi_pct": tot_roi,
        },
        "strategy_metrics": strat_metrics,
        "trades": trades,
    }


def sync_from_existing_sources():
    """Migrate all historical forward trades and live executions into SQLite."""
    init_db()
    import openpyxl

    excel_file = ROOT_DIR / "trades" / "live_and_forward_trades.xlsx"
    count = 0
    if excel_file.exists():
        wb = openpyxl.load_workbook(excel_file, data_only=True)
        ws = wb["📋 Live & Forward Trades"] if "📋 Live & Forward Trades" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if rows:
            header = [str(c) if c is not None else "" for c in rows[0]]
            for r in rows[1:]:
                if any(r):
                    t = dict(zip(header, r))
                    upsert_trade(t)
                    count += 1

    # Also merge live executed trades file
    json_file = ROOT_DIR / "trades" / "live_executed_trades.json"
    if json_file.exists():
        try:
            live_list = json.loads(json_file.read_text())
            for t in live_list:
                upsert_trade(t)
                count += 1
        except Exception as e:
            print(f"⚠️ Error reading live_executed_trades.json: {e}")

    print(f"✅ SQLite Trade Store synchronized: {count} entries indexed into {DB_PATH}")


if __name__ == "__main__":
    sync_from_existing_sources()
    data = compute_metrics(get_all_trades())
    print(f"Database contains {data['metrics']['total_trades']} trades.")
    print("Aggregate metrics:", json.dumps(data["metrics"], indent=2))
