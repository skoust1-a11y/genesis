"""Keep one Trade Crew process running per started deployment.

`uv run python -m app.live supervise` polls the live database. Each `started` deployment gets a
crew subprocess (`python -m app.agents.trade_crew run`); a crew that crashes is restarted with
exponential backoff (it reconciles with the broker before its first tick), and one whose
deployment is paused, stopped or halted is terminated. Alerts go out when a crew misses its
heartbeat for HEARTBEAT_MAX_AGE_S and when it keeps crashing. A crew that exits cleanly (its replay
feed ran out) is not restarted.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app import alerts
from app.config import Settings
from app.db.types import utcnow
from app.live.store import LiveStore

log = logging.getLogger("genesis.supervisor")
BACKEND = Path(__file__).resolve().parents[2]


@dataclass
class Child:
    deployment_id: str
    process: Any = None  # Popen-like: poll(), terminate(), wait()
    started_at: datetime | None = None
    crashes: list[float] = field(default_factory=list)  # monotonic times
    restart_at: float | None = None
    finished: bool = False
    heartbeat_alerted: bool = False


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        sessions: Callable[[], Session],
        *,
        spawn: Callable[[list[str], str], Any] | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        interval: float = 60,
        feed: str | None = None,
        backoff_s: float = 5,
        max_backoff_s: float = 300,
        crash_alert_after: int = 3,
        crash_window_s: float = 3600,
        log_dir: Path | None = None,
    ) -> None:
        self.settings, self.sessions = settings, sessions
        self.spawn = spawn or self._popen
        self.clock, self.monotonic = clock, monotonic
        self.interval, self.feed = interval, feed
        self.backoff_s, self.max_backoff_s = backoff_s, max_backoff_s
        self.crash_alert_after, self.crash_window_s = crash_alert_after, crash_window_s
        self.log_dir = log_dir or Path(settings.data_dir) / "logs"
        self.children: dict[str, Child] = {}

    def command(self, deployment_id: str) -> list[str]:
        cmd = [sys.executable, "-m", "app.agents.trade_crew", "run", "--deployment", deployment_id,
               "--interval", str(self.interval)]  # fmt: skip
        return cmd + (["--feed", self.feed] if self.feed else [])

    def _popen(self, cmd: list[str], deployment_id: str):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        out = open(self.log_dir / f"crew-{deployment_id}.log", "a")  # noqa: SIM115 - owned by the child
        return subprocess.Popen(cmd, cwd=BACKEND, stdout=out, stderr=subprocess.STDOUT)

    def _start(self, child: Child) -> None:
        child.process = self.spawn(self.command(child.deployment_id), child.deployment_id)
        child.started_at, child.restart_at, child.heartbeat_alerted = self.clock(), None, False
        log.info("started crew for %s", child.deployment_id)

    def _terminate(self, child: Child) -> None:
        if child.process is not None and child.process.poll() is None:
            child.process.terminate()
            try:
                child.process.wait(timeout=10)
            except Exception:
                child.process.kill()
        child.process = None

    def _crashed(self, child: Child, code: int) -> str:
        now = self.monotonic()
        child.crashes = [t for t in child.crashes if now - t <= self.crash_window_s] + [now]
        delay = min(self.backoff_s * 2 ** (len(child.crashes) - 1), self.max_backoff_s)
        child.process, child.restart_at = None, now + delay
        detail = f"crew exited with code {code}; restart #{len(child.crashes)} in {delay:.0f}s"
        log.warning("%s: %s", child.deployment_id, detail)
        if len(child.crashes) >= self.crash_alert_after:
            alerts.notify(self.settings, "crashes", child.deployment_id,
                          f"{len(child.crashes)} crashes in {self.crash_window_s / 60:.0f} min; {detail}")  # fmt: skip
        return "crashed"

    def _check_heartbeat(self, store: LiveStore, child: Child) -> None:
        beat = store.latest_heartbeat(child.deployment_id)
        last = max(beat.at, child.started_at) if beat and child.started_at else (beat.at if beat else child.started_at)
        age = (self.clock() - last).total_seconds() if last else 0
        if age > self.settings.heartbeat_max_age_s and not child.heartbeat_alerted:
            child.heartbeat_alerted = True
            alerts.notify(self.settings, "heartbeat_missed", child.deployment_id,
                          f"no Trade Crew tick for {age:.0f}s (HEARTBEAT_MAX_AGE_S={self.settings.heartbeat_max_age_s})")  # fmt: skip
        elif age <= self.settings.heartbeat_max_age_s:
            child.heartbeat_alerted = False

    def step(self) -> dict[str, str]:
        """One pass: start, restart, terminate and check heartbeats. Returns what happened per deployment."""
        live = self.sessions()
        actions: dict[str, str] = {}
        try:
            store = LiveStore(live)
            started = {d.id for d in store.active_deployments() if store.deployment_status(d.id) == "started"}
            for deployment_id in list(self.children):
                if deployment_id not in started:
                    self._terminate(self.children.pop(deployment_id))
                    actions[deployment_id] = "terminated"
            for deployment_id in sorted(started):
                child = self.children.setdefault(deployment_id, Child(deployment_id))
                if child.finished:
                    continue
                if child.process is not None:
                    code = child.process.poll()
                    if code is None:
                        self._check_heartbeat(store, child)
                        continue
                    if code == 0:
                        child.finished, child.process = True, None
                        actions[deployment_id] = "finished"
                        continue
                    actions[deployment_id] = self._crashed(child, code)
                if child.restart_at is None or self.monotonic() >= child.restart_at:
                    self._start(child)
                    actions[deployment_id] = "restarted" if child.crashes else "started"
        finally:
            live.rollback()
            live.close()
        return actions

    def run(self, poll_s: float = 10, sleep: Callable[[float], None] = time.sleep, max_steps: int | None = None):
        steps = 0
        try:
            while max_steps is None or steps < max_steps:
                for deployment_id, action in self.step().items():
                    print(f"{deployment_id}: {action}", flush=True)
                steps += 1
                sleep(poll_s)
        finally:
            for child in self.children.values():
                self._terminate(child)
