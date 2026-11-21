"""Mac-local paper-runner watchdog. Never places orders or edits trading journals."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

LABEL = "com.genesis.paper-forward"
STALE_SECONDS = 120
COOLDOWN_SECONDS = 180
MAX_RESTARTS_PER_HOUR = 3
IST = timezone(timedelta(hours=5, minutes=30))


def load(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def heartbeat(status, now):
    if not status or not status.get("updated_at"):
        return {"state": "HEARTBEAT_MISSING", "stale": True, "age_seconds": None}
    try:
        stamp = datetime.fromisoformat(status["updated_at"])
        if stamp.utcoffset() is None:
            raise ValueError("Naive heartbeat")
        age = (now - stamp).total_seconds()
    except (TypeError, ValueError):
        return {"state": "HEARTBEAT_INVALID", "stale": True, "age_seconds": None}
    if age < -5:
        return {"state": "CLOCK_SKEW", "stale": False, "age_seconds": round(age, 1)}
    stopped = status.get("state") in ("STOPPED", "ERROR") and age > 30
    stale = age > STALE_SECONDS or stopped
    return {
        "state": "STALE" if stale else "ADVANCING",
        "stale": stale,
        "age_seconds": round(max(0, age), 1),
    }


def _connect_readonly(path: Path, timeout: float = 2.0) -> sqlite3.Connection:
    resolved = path.resolve()

    def _open(query: str) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{resolved.as_uri()}?{query}", uri=True, timeout=timeout)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except Exception:
            conn.close()
            raise

    try:
        return _open("mode=ro")
    except sqlite3.OperationalError as exc:
        if "disk i/o error" not in str(exc).lower() or not resolved.exists():
            raise
        wal = Path(f"{resolved}-wal")
        if not wal.exists() or wal.stat().st_size == 0:
            try:
                return _open("mode=ro&immutable=1")
            except sqlite3.OperationalError:
                pass
        return _open("mode=rw")


def exposure(root):
    """Require both paper ledgers and baseline model trades to be verifiably flat."""
    manifest = load(root / "active-run.json")
    run = Path(manifest["directory"]).resolve()
    if not run.is_relative_to((root / "runs").resolve()):
        raise ValueError("Active journal is outside the declared run directory")
    counts = {}
    for filename, tables in (
        ("candidate.db", ("trades",)),
        ("baseline.db", ("crew_paper_trades", "crew_trades")),
    ):
        path = run / filename
        db = _connect_readonly(path, timeout=2)
        try:
            for table in tables:
                counts[table] = db.execute(
                    f"SELECT count(*) FROM {table} WHERE state <> 'CLOSED'"
                ).fetchone()[0]
        finally:
            db.close()
    return {
        "verified": True,
        "flat": not any(counts.values()),
        "counts": counts,
        "run_id": manifest["run_id"],
    }


def restart_allowed(previous, positions, now):
    if not positions.get("verified"):
        return False, "RESTART_BLOCKED_STATE_UNVERIFIED"
    if not positions.get("flat"):
        return False, "RESTART_BLOCKED_OPEN_OR_UNRESOLVED_EXPOSURE"
    attempts = [datetime.fromisoformat(x) for x in previous.get("restart_attempts", [])]
    recent = [stamp for stamp in attempts if now - stamp < timedelta(hours=1)]
    if recent and (now - max(recent)).total_seconds() < COOLDOWN_SECONDS:
        return False, "RESTART_COOLDOWN"
    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        return False, "RESTART_LIMIT_REACHED"
    return True, "FLAT_RESTART_ALLOWED"


class Launchd:
    def __init__(self, uid=None):
        self.target = f"gui/{os.getuid() if uid is None else uid}/{LABEL}"

    def command(self, *arguments):
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )

    def pid(self):
        result = self.command("print", self.target)
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return int(match[1]) if match else None

    def restart_flat(self, root):
        # Recheck immediately before signalling. Never clear or steal a lease.
        if not exposure(root)["flat"]:
            return {"result": "RESTART_CANCELLED_EXPOSURE_CHANGED"}
        old = self.pid()
        if old is not None:
            self.command("kill", "SIGTERM", self.target)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = self.pid()
                if current is None:
                    break
                if current != old:
                    return {
                        "result": "LAUNCHD_ALREADY_RESTARTED",
                        "old_pid": old,
                        "pid": current,
                    }
                time.sleep(0.5)
        if not exposure(root)["flat"]:
            return {"result": "FORCE_RESTART_BLOCKED_EXPOSURE_CHANGED", "old_pid": old}
        current = self.pid()
        result = self.command("kickstart", "-k" if current else "-p", self.target)
        return {
            "result": "RESTART_REQUESTED"
            if result.returncode == 0
            else "RESTART_FAILED",
            "old_pid": old,
            "returncode": result.returncode,
            "diagnostic": result.stderr.strip()[:300],
        }


def power_conditions():
    result = {
        "requirements": [
            "Keep the lid open",
            "Keep AC power connected",
            "Keep network available",
        ]
    }
    try:
        power = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        result["ac_power"] = "Now drawing from 'AC Power'" in power.stdout
        lid = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        match = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', lid.stdout)
        result["lid_open"] = match[1] == "No" if match else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["probe_error"] = type(exc).__name__
    return result


def check(root, *, now=None, act=False, controller=None):
    root = Path(root).resolve()
    now = now or datetime.now(UTC)
    previous_path = root / "host-health.json"
    try:
        previous = load(previous_path) if previous_path.exists() else {}
    except (OSError, ValueError):
        previous = {}
    try:
        status = load(root / "status.json")
    except (OSError, ValueError):
        status = {}
    health = heartbeat(status, now)
    result = {
        "checked_at": now.isoformat(),
        "heartbeat": health,
        "state": "RUNNING" if not health["stale"] else "ATTENTION_REQUIRED",
        "restart_attempts": [
            x
            for x in previous.get("restart_attempts", [])
            if now - datetime.fromisoformat(x) < timedelta(hours=1)
        ],
        "mode": "ACT" if act else "CHECK_ONLY",
        "live_orders": False,
    }
    service = controller or Launchd()
    try:
        pid = service.pid()
    except (OSError, subprocess.TimeoutExpired):
        pid = None
    result["pid"] = pid
    if pid:
        first_seen = (
            previous.get("pid_first_seen_at") if previous.get("pid") == pid else None
        )
        result["pid_first_seen_at"] = first_seen or now.isoformat()
    if health["state"] == "CLOCK_SKEW":
        result["state"] = "CLOCK_SKEW"
    if health["stale"]:
        try:
            positions = exposure(root)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            positions = {"verified": False, "flat": False, "error": type(exc).__name__}
        result["exposure"] = positions
        allowed, reason = restart_allowed(previous, positions, now)
        if (
            allowed
            and pid
            and (
                now - datetime.fromisoformat(result["pid_first_seen_at"])
            ).total_seconds()
            < 45
        ):
            allowed, reason = False, "PROCESS_STARTUP_GRACE"
        result["restart_gate"] = reason
        if act and allowed:
            result["restart_attempts"].append(now.isoformat())
            result["restart"] = service.restart_flat(root)
    result["host_conditions"] = power_conditions()
    atomic_json(previous_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "logs/paper-forward",
    )
    parser.add_argument(
        "--act",
        action="store_true",
        help="Restart a stale runner only after verifying flat state",
    )
    args = parser.parse_args(argv)
    args.state_root.mkdir(parents=True, exist_ok=True)
    with (args.state_root / "host-watchdog.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        result = check(args.state_root, act=args.act)
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
