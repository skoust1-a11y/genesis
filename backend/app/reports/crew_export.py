"""Explicitly invoked crew research export to a new .xlsx file. The journal is only read.

    python -m app.reports.crew_export --journal J.db --out ~/crew_research.xlsx \
        --strategy-version scanner-v0 --execution-policy research-exec-v1-monotonic-stop

Refuses an existing output (never overwrites a workbook) and any location inside the legacy data, trades
or reports directories. Values are index points and R only; there are no premium or rupee columns.
"""

from __future__ import annotations

import argparse
import io
import json
from datetime import datetime
from pathlib import Path

import openpyxl

from app.db import crew_reader


class ExportRefused(Exception):
    pass


def check_output(out: Path | str, journal: Path | str) -> Path:
    target = Path(out).expanduser().resolve()
    if target.suffix.lower() != ".xlsx":
        raise ExportRefused("the export must be a .xlsx file")
    if any(target.is_relative_to(d) for d in crew_reader.PROTECTED_DIRS):
        raise ExportRefused("the export may not be written inside a protected legacy directory")
    if target == Path(journal).expanduser().resolve():
        raise ExportRefused("the export may not replace the journal")
    if target.exists() or target.is_symlink():
        raise ExportRefused("the export file already exists; choose a new name")
    if not target.parent.is_dir():
        raise ExportRefused("the export directory does not exist")
    return target


def export(journal: Path | str, out: Path | str, *, strategy_version: str, execution_policy: str,
           now: datetime | None = None) -> Path:
    target = check_output(out, journal)
    now = now or crew_reader.now_ist()
    with crew_reader.snapshot(journal) as conn:
        stats = crew_reader.stats(conn, strategy_version=strategy_version, execution_policy=execution_policy, now=now)
        exceptions = crew_reader.exceptions(conn, now=now, limit=crew_reader.MAX_LIMIT)
        rows, offset = [], 0
        while True:
            page = crew_reader.trades(conn, now=now, strategy_version=strategy_version,
                                      execution_policy=execution_policy, limit=crew_reader.MAX_LIMIT, offset=offset)
            rows += page["items"]
            offset += crew_reader.MAX_LIMIT
            if offset >= page["total"]:
                break

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stats"
    for key in ("source", "strategy_version", "execution_policy", "eligibility", "applicable_signal_rule",
                "monetary_metrics", "interpretation", "min_sample", "as_of", "statistics_withheld"):
        ws.append([key, str(stats[key])])
    ws.append(["units", json.dumps(stats["units"])])
    ws.append(["consistency_ok", stats["consistency"]["ok"]])
    ws.append([])
    ws.append(["strategy_id", "section", "metric", "value"])
    for group in stats["groups"]:
        for section in ("population", "exclusions", "not_in_population", "results"):
            for metric, value in group[section].items():
                ws.append([group["strategy_id"], section, metric, json.dumps(value) if isinstance(value, dict) else value])
        for metric, value in group["coverage"].items():
            ws.append([group["strategy_id"], "coverage", metric, json.dumps(value)])

    trades_ws = wb.create_sheet("Model Trades")
    columns = ["trade_id", "market_day", "strategy_id", "strategy_version", "execution_policy", "status",
               "direction", "contract", "entry_spot", "stop_spot", "target_spot", "model_lots_initial",
               "model_lots_open", "opened_at", "closed_at", "exit_reason", "model_points", "model_r"]
    trades_ws.append(columns)
    for row in rows:
        trades_ws.append([row.get(c) for c in columns])

    ex_ws = wb.create_sheet("Exceptions")
    ex_ws.append(["consistency_findings", exceptions["consistency"]["count"]])
    for key in ("missing_model_entries", "orphaned_trades", "open_prior_day_trades", "expired_unresolved_trades",
                "quarantined_trades", "unknown_publications"):
        ex_ws.append([key, exceptions[key]["count"]])

    buf = io.BytesIO()
    wb.save(buf)
    with open(target, "xb") as f:  # exclusive create: never replaces a file that appeared meanwhile
        f.write(buf.getvalue())
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crew research export (read-only journal, new .xlsx file)")
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--execution-policy", required=True)
    args = parser.parse_args(argv)
    try:
        target = export(args.journal, args.out, strategy_version=args.strategy_version,
                        execution_policy=args.execution_policy)
    except (ExportRefused, crew_reader.Unavailable) as e:
        print(f"export refused: {e}")
        return 2
    print(f"wrote {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
