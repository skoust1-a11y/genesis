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
DEFAULT_DB_PATH = ROOT_DIR / "data" / "trades.db"


def _resolve(db_path: Path | str | None) -> Path:
    # Read DEFAULT_DB_PATH at call time so tests can redirect it.
    return Path(db_path) if db_path is not None else DEFAULT_DB_PATH


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Returns a SQLite connection configured for concurrent WAL access."""
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_readonly_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Read-only connection: no schema init, no PRAGMA writes, fails if the file is missing."""
    conn = sqlite3.connect(f"{_resolve(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None):
    """Initialize the trade store database schema and indices. Never drops data."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_and_forward_trades (
                trade_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                strategy TEXT NOT NULL,
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
                liquidity_pool TEXT DEFAULT 'UNSPECIFIED',
                entry_reason TEXT DEFAULT '',
                base_timeframe TEXT DEFAULT '15M',
                lookout_duration_mins INTEGER DEFAULT 120,
                lookout_status TEXT DEFAULT 'TRIGGERED',
                thesis TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # Safe migrations for existing databases
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(live_and_forward_trades);").fetchall()}
        if "liquidity_pool" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN liquidity_pool TEXT DEFAULT 'UNSPECIFIED';")
        if "entry_reason" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN entry_reason TEXT DEFAULT '';")
        if "base_timeframe" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN base_timeframe TEXT DEFAULT '15M';")
        if "lookout_duration_mins" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN lookout_duration_mins INTEGER DEFAULT 120;")
        if "lookout_status" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN lookout_status TEXT DEFAULT 'TRIGGERED';")
        if "thesis" not in existing_cols:
            conn.execute("ALTER TABLE live_and_forward_trades ADD COLUMN thesis TEXT DEFAULT '';")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON live_and_forward_trades(date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON live_and_forward_trades(strategy);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON live_and_forward_trades(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_liquidity_pool ON live_and_forward_trades(liquidity_pool);")


def make_trade_id(date_str: str, time_str: str, strategy: str) -> str:
    """Generate a deterministic, unique trade ID."""
    clean_strat = strategy.replace(" ", "_").replace("&", "AND").replace("(", "").replace(")", "").replace("-", "")
    return f"{date_str.replace('-', '')}_{time_str.replace(':', '')}_{clean_strat}"


def upsert_trade(trade: dict[str, Any], db_path: Path | str | None = None) -> str:
    """Upsert a single trade into SQLite."""
    init_db(db_path)
    d_str = str(trade.get("Date") or trade.get("date") or "")[:10]
    t_str = str(trade.get("Time") or trade.get("time") or "")
    strat = str(trade.get("Strategy") or trade.get("strategy") or "").strip()

    # Normalize strategy name
    if "Breakout & Retest (OR-30)" in strat or "OR-30" in strat or "ORB" in strat:
        strat = "Breakout & Retest (OR-30)"
    elif "Breakout & Retest (PD)" in strat or "PD" in strat or strat == "Breakout & Retest":
        pool = str(trade.get("Pool / Setup") or trade.get("setup") or "")
        strat = "Breakout & Retest (OR-30)" if "OR" in pool else "Breakout & Retest (PD)"
    elif "Trendline" in strat:
        strat = "Trendline Reversal"
    elif "Sweep" in strat:
        strat = "Liquidity Sweep"

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
    if trade.get("pnl_method") == "index_points_not_priced" or (
        "pnl_rupees" in trade and trade.get("pnl_rupees") is None
    ):
        pnl = None
        roi = None
    else:
        raw_pnl = trade.get("Est P&L (Rs)")
        if raw_pnl is None:
            raw_pnl = trade.get("est_pnl_inr")
        pnl = float(exit_val - capital) if raw_pnl is None else float(raw_pnl)
        roi = float(trade.get("ROI %") or trade.get("roi_pct") or ((pnl / capital * 100) if capital > 0 else 0.0))
    rationale = str(trade.get("Rationale") or trade.get("rationale") or "")
    source = str(trade.get("source") or "live")

    # Dynamic Liquidity & Timeframe Extraction
    liquidity_pool = str(trade.get("Liquidity Pool") or trade.get("liquidity_pool") or "").strip()
    if not liquidity_pool or liquidity_pool == "UNSPECIFIED":
        if "PDH" in setup or "OR-30H" in setup or "PDH" in rationale or "Day High" in rationale:
            liquidity_pool = "BSL_SWEEP" if side == "SHORT" else "BSL_EXPANSION"
        elif "PDL" in setup or "OR-30L" in setup or "PDL" in rationale or "Day Low" in rationale:
            liquidity_pool = "SSL_SWEEP" if side == "LONG" else "SSL_EXPANSION"
        elif "Waterfall" in setup or "Macro Breakdown" in setup:
            liquidity_pool = "SSL_EXPANSION"
        elif "Trendline" in strat or "Trendline" in setup:
            liquidity_pool = "BSL_REVERSAL" if side == "SHORT" else "SSL_REVERSAL"
        else:
            liquidity_pool = "BSL_EXPANSION" if side == "LONG" else "SSL_EXPANSION"

    entry_reason = str(trade.get("Entry Reason") or trade.get("entry_reason") or "").strip()
    if not entry_reason:
        if liquidity_pool == "BSL_SWEEP":
            entry_reason = f"BUY PE: Swept resting Buy-Side Liquidity above {setup.split()[0] if setup else 'high'}; trapped breakout buyers absorbed by institutional selling."
        elif liquidity_pool == "SSL_SWEEP":
            entry_reason = f"BUY CE: Swept resting Sell-Side Liquidity below {setup.split()[0] if setup else 'low'}; trapped panic sellers absorbed by institutional buyers."
        elif liquidity_pool in ("BSL_EXPANSION", "BSL_REVERSAL") and side == "LONG":
            entry_reason = f"BUY CE: Clean displacement & bullish expansion holding above VWAP and dynamic support."
        elif liquidity_pool in ("SSL_EXPANSION", "BSL_REVERSAL") and side == "SHORT":
            entry_reason = f"BUY PE: Clean breakdown & aggressive institutional market selling below dynamic resistance."
        else:
            entry_reason = rationale

    base_tf = str(trade.get("Base Timeframe") or trade.get("base_timeframe") or ("30M" if "OR" in strat or "30" in strat else "15M"))
    lookout_mins = int(trade.get("Lookout Duration") or trade.get("lookout_duration_mins") or (180 if "30M" in base_tf else 90))
    lookout_stat = str(trade.get("Lookout Status") or trade.get("lookout_status") or "TRIGGERED")
    thesis_val = json.dumps(trade.get("thesis")) if isinstance(trade.get("thesis"), dict) else str(trade.get("thesis") or "")

    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO live_and_forward_trades (
                trade_id, date, time, strategy, setup, side, status,
                entry_spot, stop_spot, target_spot, risk_pts,
                exit_spot, exit_time, exit_reason, points, r_multiple,
                option_contract, expiry, entry_premium, exit_premium,
                capital_deployed, exit_value, est_pnl_inr, roi_pct,
                lots, qty, rationale, source,
                liquidity_pool, entry_reason, base_timeframe,
                lookout_duration_mins, lookout_status, thesis, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, datetime('now')
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
                liquidity_pool = excluded.liquidity_pool,
                entry_reason = excluded.entry_reason,
                base_timeframe = excluded.base_timeframe,
                lookout_duration_mins = excluded.lookout_duration_mins,
                lookout_status = excluded.lookout_status,
                thesis = excluded.thesis,
                updated_at = datetime('now');
        """, (
            trade_id, d_str, t_str, strat, setup, side, status,
            entry_spot, stop_spot, target_spot, risk_pts,
            exit_spot, exit_time, exit_reason, points, r_mult,
            opt, expiry, entry_prem, exit_prem,
            capital, exit_val, pnl, roi,
            lots, qty, rationale, source,
            liquidity_pool, entry_reason, base_tf,
            lookout_mins, lookout_stat, thesis_val
        ))
    return trade_id


def get_all_trades(since: str = "2026-10-15", db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Retrieve trades filtered chronologically with frontend and API compatible keys. Read-only."""
    if not _resolve(db_path).exists():
        return []
    conn = get_readonly_connection(db_path)
    try:
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
    finally:
        conn.close()


METRIC_FIELDS = ("r_multiple", "points", "est_pnl_inr", "capital_deployed", "exit_value")


def _total(rows: list[dict[str, Any]], key: str, digits: int) -> float | None:
    """The sum, or None when any row lacks the value: a missing value is unknown, never zero."""
    values = [t.get(key) for t in rows]
    return None if any(v is None for v in values) else round(sum(values), digits)


def _ratio_pct(num: float | None, den: float | None) -> float | None:
    return None if num is None or not den else round(num / den * 100, 1)


def compute_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate KPIs and strategy breakdowns. A total is None when any of its rows lacks the value.

    Win/loss counts use only rows with an r_multiple; ``missing_values`` counts the rows lacking each field.
    """
    if not trades:
        return {"metrics": {}, "strategy_metrics": {}, "trades": []}

    scored = [t for t in trades if t.get("r_multiple") is not None]
    wins = [t for t in scored if t["r_multiple"] > 0.05]
    losses = [t for t in scored if t["r_multiple"] < -0.05]
    be = [t for t in scored if abs(t["r_multiple"]) <= 0.05]

    tot_trades = len(trades)
    win_rate = (len(wins) / len(scored) * 100) if scored else 0.0
    win_rate_ex_be = (len(wins) / (len(wins) + len(losses)) * 100) if (len(wins) + len(losses)) > 0 else 0.0
    tot_pnl = _total(trades, "est_pnl_inr", 0)
    tot_cap = _total(trades, "capital_deployed", 0)
    avg_cap = round(tot_cap / tot_trades, 0) if tot_cap is not None else None

    gross_profit = _total(wins, "est_pnl_inr", 2)
    gross_loss = _total(losses, "est_pnl_inr", 2)
    if tot_pnl is None or gross_profit is None or gross_loss is None:
        pf = None
    else:
        gross_loss = abs(gross_loss)
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else 1.0)

    strat_metrics: dict[str, Any] = {}
    strategies = sorted(set(t["strategy"] for t in trades))
    for s_name in strategies:
        s_trades = [t for t in trades if t["strategy"] == s_name]
        s_scored = [t for t in s_trades if t.get("r_multiple") is not None]
        s_wins = [t for t in s_scored if t["r_multiple"] > 0.05]
        s_losses = [t for t in s_scored if t["r_multiple"] < -0.05]
        s_be = [t for t in s_scored if abs(t["r_multiple"]) <= 0.05]
        s_pnl = _total(s_trades, "est_pnl_inr", 0)
        s_cap = _total(s_trades, "capital_deployed", 0)
        strat_metrics[s_name] = {
            "trades": len(s_trades),
            "wins": len(s_wins),
            "losses": len(s_losses),
            "be": len(s_be),
            "win_rate": round(len(s_wins) / len(s_scored) * 100, 1) if s_scored else 0.0,
            "total_r": _total(s_trades, "r_multiple", 2),
            "capital_deployed": s_cap,
            "net_pnl_inr": s_pnl,
            "roi_pct": _ratio_pct(s_pnl, s_cap),
        }

    return {
        "metrics": {
            "total_trades": tot_trades,
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(be),
            "win_rate": round(win_rate, 1),
            "win_rate_ex_be": round(win_rate_ex_be, 1),
            "total_r": _total(trades, "r_multiple", 2),
            "total_points": _total(trades, "points", 1),
            "net_pnl_inr": tot_pnl,
            "profit_factor": pf,
            "total_capital_deployed": tot_cap,
            "total_exit_value": _total(trades, "exit_value", 0),
            "avg_trade_capital": avg_cap,
            "total_roi_pct": _ratio_pct(tot_pnl, tot_cap),
            "missing_values": {k: n for k in METRIC_FIELDS if (n := sum(t.get(k) is None for t in trades))},
        },
        "strategy_metrics": strat_metrics,
        "trades": trades,
    }


def sync_from_existing_sources(db_path: Path | str | None = None):
    """Migrate all historical forward trades and live executions into SQLite."""
    init_db(db_path)
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
                    upsert_trade(t, db_path)
                    count += 1

    # Also merge live executed trades file
    json_file = ROOT_DIR / "trades" / "live_executed_trades.json"
    if json_file.exists():
        try:
            live_list = json.loads(json_file.read_text())
            for t in live_list:
                upsert_trade(t, db_path)
                count += 1
        except Exception as e:
            print(f"⚠️ Error reading live_executed_trades.json: {e}")

    print(f"✅ SQLite Trade Store synchronized: {count} entries indexed into {_resolve(db_path)}")


if __name__ == "__main__":
    sync_from_existing_sources()
    data = compute_metrics(get_all_trades())
    print(f"Database contains {data['metrics']['total_trades']} trades.")
    print("Aggregate metrics:", json.dumps(data["metrics"], indent=2))
