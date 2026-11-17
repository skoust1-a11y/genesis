"""Prepare/install Mac LaunchAgents for the authorized local paper service."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from paper_watchdog import LABEL, exposure

AWAKE = "com.genesis.paper-awake"
WATCHDOG = "com.genesis.paper-watchdog"


def definitions(root, python):
    state = root / "logs/paper-forward"
    common = {"RunAtLoad": True, "WorkingDirectory": str(root), "ThrottleInterval": 10}
    return {
        AWAKE: {
            **common,
            "Label": AWAKE,
            "KeepAlive": True,
            "ProgramArguments": ["/usr/bin/caffeinate", "-dimsu"],
            "StandardOutPath": str(state / "awake.log"),
            "StandardErrorPath": str(state / "awake.log"),
        },
        WATCHDOG: {
            **common,
            "Label": WATCHDOG,
            "StartInterval": 30,
            "ProcessType": "Background",
            "ProgramArguments": [
                str(python),
                str(root / "scripts/paper_watchdog.py"),
                "--state-root",
                str(state),
                "--act",
            ],
            "StandardOutPath": str(state / "watchdog.log"),
            "StandardErrorPath": str(state / "watchdog.log"),
        },
    }


def command(*arguments):
    result = subprocess.run(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"launchctl {arguments[0]} failed: {result.stderr.strip()[:300]}"
        )
    return result


def install(root, *, apply=False, reload_runner=False):
    root = root.resolve()
    python = root / "backend/.venv-dev/bin/python"
    if not python.exists() or not (root / "scripts/paper_forward.py").exists():
        raise ValueError("Existing paper runtime was not found")
    state = root / "logs/paper-forward"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    staged = state / "host-supervision" / stamp
    staged.mkdir(parents=True)
    configs = definitions(root, python)
    for label, body in configs.items():
        payload = plistlib.dumps(body)
        assert plistlib.loads(payload) == body
        (staged / (label + ".plist")).write_bytes(payload)
    result = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "staged": str(staged),
        "applied": False,
        "requirements": ["AC power connected", "Lid open", "Network available"],
        "runner_reloaded": False,
        "strategy_changed": False,
    }
    if apply:
        if os.getuid() == 0:
            raise ValueError("Run as the logged-in Mac user, not root")
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        for label in configs:
            destination = agents / (label + ".plist")
            if destination.exists():
                shutil.copy2(destination, staged / (label + ".previous.plist"))
            loaded = subprocess.run(
                ["/bin/launchctl", "print", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if loaded.returncode == 0:
                command("bootout", f"{domain}/{label}")
            shutil.copy2(staged / (label + ".plist"), destination)
            command("bootstrap", domain, str(destination))
        result["applied"] = True
        if reload_runner:
            positions = exposure(state)
            result["reload_exposure_check"] = positions
            if positions["flat"]:
                command("kickstart", "-k", f"{domain}/{LABEL}")
                result["runner_reloaded"] = True
            else:
                result["reload_deferred"] = (
                    "Open or unresolved exposure; wake guard still installed"
                )
    (staged / "installation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reload-runner",
        action="store_true",
        help="Reload only if all journals verify flat",
    )
    args = parser.parse_args(argv)
    install(args.root, apply=args.apply, reload_runner=args.reload_runner)


if __name__ == "__main__":
    main()
