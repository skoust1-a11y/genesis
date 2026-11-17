"""Offline coverage replay; never places orders or sends messages.

Reports spot signal coverage and independent 60-minute barrier diagnostics,
not options P&L or a portfolio backtest. Existing historical provider receipts
and today's locally recorded snapshot are the only inputs.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.market.hours import holidays
from app.trading import forward_zone_candidate as zone
from app.trading import structure_entry_research as policy
from app.trading.market_validation import regular_session
from app.trading.option_quote import IST


def barrier(signal, rows):
    """Independent spot excursion only; same-bar ambiguity counts stop first."""
    start = datetime.fromisoformat(signal["signal_at"])
    sign = 1 if signal["side"] == "CALL" else -1
    entry, stop, risk = (signal[k] for k in ("entry_spot", "stop_spot", "risk_points"))
    future = [
        r
        for r in rows
        if start <= datetime.fromisoformat(r["date"]) < start + timedelta(minutes=60)
    ]
    for row in future:
        adverse = row["low"] <= stop if sign == 1 else row["high"] >= stop
        favourable = (
            row["high"] >= entry + risk if sign == 1 else row["low"] <= entry - risk
        )
        if adverse:
            return "STOP_BEFORE_1R"
        if favourable:
            return "1R_BEFORE_STOP"
    return "UNRESOLVED" if len(future) == 60 else "CENSORED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-10-15")
    parser.add_argument("--end", default="2026-11-17")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "logs/resimulation-20261117/structure-entries-v1",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipts, by_day, stored = {}, defaultdict(list), {}
    source = ROOT / "logs/resimulation-20261116/reliability-v1"
    intraday_manifest = source / "intraday-manifest.json"
    intraday_capture = datetime.fromisoformat(
        json.loads(intraday_manifest.read_text())["captured_at"]
    ).astimezone(IST)
    receipts[str(intraday_manifest.relative_to(ROOT))] = hashlib.sha256(
        intraday_manifest.read_bytes()
    ).hexdigest()
    for path in sorted(source.glob("nifty-1m-*.json")):
        raw = path.read_bytes()
        receipts[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
        for item in json.loads(raw)["data"]["candles"]:
            stamp = datetime.fromisoformat(item[0]).astimezone(IST)
            if (
                path.name == "nifty-1m-intraday.json"
                and stamp + timedelta(minutes=1) > intraday_capture
            ):
                continue
            row = {
                "date": stamp.isoformat(),
                "open": item[1],
                "high": item[2],
                "low": item[3],
                "close": item[4],
            }
            if stamp.date().isoformat() == "2026-11-17":
                continue
            if row["date"] in stored and stored[row["date"]] != row:
                raise ValueError("Conflicting historical candle: " + row["date"])
            stored[row["date"]] = row
    manifest = json.loads((ROOT / "logs/paper-forward/active-run.json").read_text())
    snapshots = sorted(
        (Path(manifest["directory"]) / "evidence/2026-11-17").glob(
            "*-candidate.json.gz"
        )
    )
    capture = None
    for path in reversed(snapshots):
        try:
            raw = path.read_bytes()
            snapshot = json.loads(gzip.decompress(raw))
            break
        except (EOFError, OSError):
            continue
    else:
        raise ValueError("No complete current-day snapshot")
    capture = datetime.fromisoformat(snapshot["observed_at"]).astimezone(IST)
    receipts[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
    for raw_row in snapshot["raw_1m"]:
        if str(raw_row["date"])[:10] != "2026-11-17":
            continue
        stamp = datetime.fromisoformat(raw_row["date"])
        stamp = (
            stamp.replace(tzinfo=IST)
            if stamp.utcoffset() is None
            else stamp.astimezone(IST)
        )
        if stamp + timedelta(minutes=1) > capture:
            continue
        stored[stamp.isoformat()] = {
            **{k: raw_row[k] for k in ("open", "high", "low", "close")},
            "date": stamp.isoformat(),
        }
    calendar = holidays(
        Settings(_env_file=None, data_dir=str(ROOT / "logs/paper-forward/data"))
    )
    for stamp, row in sorted(stored.items()):
        day = stamp[:10]
        if (
            args.start <= day <= args.end
            and "09:15" <= stamp[11:16] < "15:30"
            and regular_session(datetime.fromisoformat(stamp).date(), calendar)
        ):
            by_day[day].append(row)
    signals, baseline, days, skipped = [], [], [], []
    for day, rows in sorted(by_day.items()):
        now = (
            capture
            if day == "2026-11-17"
            else intraday_capture
            if day == intraday_capture.date().isoformat()
            else datetime.fromisoformat(day + "T15:30:00+05:30")
        )
        try:
            state, emitted = policy.advance(rows, policy.initial_state(day), now)
        except ValueError as exc:
            skipped.append({"day": day, "reason": str(exc)})
            continue
        # Check restart equivalence against an independently executed prefix.
        split = len(rows) // 2
        halfway = datetime.fromisoformat(rows[split - 1]["date"]) + timedelta(minutes=1)
        partial, earlier = policy.advance(
            rows[:split], policy.initial_state(day), halfway
        )
        resumed, later = policy.advance(rows, json.loads(json.dumps(partial)), now)
        assert earlier + later == emitted and resumed == state
        for signal in emitted:
            assert datetime.fromisoformat(
                signal["reference_known_at"]
            ) <= datetime.fromisoformat(signal["signal_at"]) - timedelta(minutes=1)
            signals.append({**signal, "independent_spot_60m": barrier(signal, rows)})
        _, old = zone.advance(rows, zone.initial_state(day))
        old = [
            s
            for s in old
            if 10 <= s["risk_points"] <= 55 and s["signal_at"][11:16] < "15:00"
        ]
        baseline.extend({**s, "independent_spot_60m": barrier(s, rows)} for s in old)
        days.append(
            {
                "day": day,
                "bars": len(rows),
                "structure_signals": len(emitted),
                "zone_signals": len(old),
            }
        )
    families = {
        family: dict(
            Counter(
                s["independent_spot_60m"]
                for s in signals
                if s["source_family"] == family
            )
        )
        for family in sorted({s["source_family"] for s in signals})
    }
    windows = [
        ("opening", "09:36", "09:40", "CALL"),
        ("morning re-entry", "10:40", "10:50", "CALL"),
        ("afternoon breakdown", "12:55", "13:05", "PUT"),
        ("afternoon re-entry", "13:35", "13:40", "CALL"),
    ]
    coverage = [
        {
            "window": label,
            "matches": [
                s
                for s in signals
                if s["day"] == "2026-11-17"
                and lo <= s["signal_at"][11:16] <= hi
                and s["side"] == side
            ],
        }
        for label, lo, hi, side in windows
    ]
    result = {
        "rule_hash": policy.RULE_HASH,
        "policy_source_sha256": hashlib.sha256(
            Path(policy.__file__).read_bytes()
        ).hexdigest(),
        "runner_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rules": policy.RULES,
        "capture": capture.isoformat(),
        "source_hashes": receipts,
        "scope": "Research-only spot signals; no quotes, option sizing, account capacity, news, execution, fees or portfolio returns. Not promoted. Independent 60-minute 1R/stop diagnostics count same-bar ambiguity as stop first; unfinished windows are censored. User-selected windows are not out-of-sample validation.",
        "days": days,
        "skipped": skipped,
        "counts": families,
        "zone_counts": dict(Counter(s["independent_spot_60m"] for s in baseline)),
        "today_coverage": coverage,
        "signals": signals,
        "zone_signals": baseline,
    }
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        "# Structure-entry coverage research",
        "",
        result["scope"],
        "",
        f"Sessions: {len(days)}; skipped: {len(skipped)}. Structure signals: {len(signals)}; current-zone signals: {len(baseline)}.",
        "",
        "| Family | 1R before stop | Stop before 1R | Unresolved | Censored |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, counts in {**families, "CURRENT_ZONE": result["zone_counts"]}.items():
        lines.append(
            f"| {name} | "
            + " | ".join(
                str(counts.get(k, 0))
                for k in ("1R_BEFORE_STOP", "STOP_BEFORE_1R", "UNRESOLVED", "CENSORED")
            )
            + " |"
        )
    lines += ["", "## Today's nominated windows", ""]
    for item in coverage:
        matches = (
            "; ".join(
                f"{s['signal_at'][11:16]} {s['source_family']} {s['side']} at {s['entry_spot']}, stop {s['stop_spot']}"
                for s in item["matches"]
            )
            or "No match"
        )
        lines.append(f"- {item['window']}: {matches}")
    lines += [
        "",
        "Signal emission is separate from admission. A stale rejected signal cannot be retried, but a new distinct structure remains eligible while flat. Quote, risk, calendar, news and one-position checks are still required before a trade. Active paper policy is unchanged.",
        "",
        "The first broad draft is deliberately not tuned to maximize today's coverage or retrospective profit. Large signal counts and stopped setups are reasons to validate/select filters on earlier sessions, then freeze and forward-test. These figures cannot establish trading profitability.",
    ]
    (args.output / "report.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "sessions": len(days),
                "skipped": skipped,
                "signals": len(signals),
                "zone_signals": len(baseline),
                "counts": families,
                "today": [
                    {
                        "window": c["window"],
                        "matches": [
                            (s["signal_at"][11:16], s["source_family"])
                            for s in c["matches"]
                        ],
                    }
                    for c in coverage
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
