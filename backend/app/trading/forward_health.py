"""Persist session coverage independently of trading decisions and strategy identity."""

import json
from datetime import datetime
from pathlib import Path


class ScanCoverage:
    MAX_GAP_SECONDS = 45

    def __init__(self, run_root):
        self.root = Path(run_root)
        self.path = self.root / "coverage.json"
        self.value = json.loads(self.path.read_text()) if self.path.exists() else {}
        status_path = self.root / "status.json"
        if not self.value and status_path.exists():
            status = json.loads(status_path.read_text())
            if status.get("updated_at"):
                stamp = datetime.fromisoformat(status["updated_at"])
                self.value = {"day": stamp.date().isoformat(), "gap_count": 0,
                              "max_gap_seconds": 0, "last_completed_at": stamp.isoformat(),
                              "scope": "Since coverage monitoring was installed; earlier scans are not audited here."}

    def complete(self, now, issues):
        day = now.date().isoformat()
        if self.value.get("day") != day:
            self.value = {"day": day, "gap_count": 0, "max_gap_seconds": 0}
        opening = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        previous = datetime.fromisoformat(self.value.get("last_completed_at", opening.isoformat()))
        # Count only the part of a scan interval inside the market session.
        seconds = max(0, (min(now, end) - max(previous, opening)).total_seconds())
        if seconds > self.MAX_GAP_SECONDS:
            event = {"kind": "SCAN_COVERAGE_GAP", "from": max(previous, opening).isoformat(),
                     "to": min(now, end).isoformat(), "seconds": round(seconds, 1)}
            with (self.root / "coverage-incidents.jsonl").open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            self.value["gap_count"] += 1
            self.value["max_gap_seconds"] = max(self.value["max_gap_seconds"], event["seconds"])
            self.value["last_gap"] = event
        self.value.update(last_completed_at=now.isoformat(), current_issues=sorted(set(issues)),
                          continuous_coverage=self.value["gap_count"] == 0)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.value, indent=2) + "\n")
        temporary.replace(self.path)
        return dict(self.value)


def baseline_issues(view):
    issues = []
    contracts = (view.chain or {}).get("contracts") or []
    if not contracts:
        issues.append("BASELINE_OPTION_CHAIN_EMPTY")
    elif not any(leg.get("liveData") for row in contracts
                 for side in ("ce", "pe") if isinstance(leg := row.get(side), dict)):
        issues.append("BASELINE_EXECUTABLE_QUOTES_UNAVAILABLE")
    if view.df_5m is None or view.df_5m.empty:
        issues.append("BASELINE_CLOSED_5M_UNAVAILABLE")
    issues.extend(f"BASELINE_5M:{reason}" for reason in view.rejections.get("5m", []))
    return issues
