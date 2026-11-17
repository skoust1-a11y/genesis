"""Prepare/activate a new two-account paper experiment without deleting prior history."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from paper_forward import STATE, settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Select this new run for the existing supervisor",
    )
    parser.add_argument("--exit-quote-policy", choices=("entry-spread-v1", "fresh-bid-v1"),
                        default="entry-spread-v1", help="Immutable common paper exit policy for both accounts")
    parser.add_argument("--replace-active", action="store_true",
                        help="Outside supervision hours only: preserve a flat active experiment and select a new one")
    parser.add_argument("--candidate-profit-giveback-pct", type=Decimal,
                        choices=(Decimal(10), Decimal(15), Decimal(20)), default=None,
                        help="Replace candidate's 3R target with a peak-net-profit trail, armed after 1R")
    args = parser.parse_args()
    if args.replace_active and not args.activate:
        parser.error("--replace-active requires --activate")
    if args.candidate_profit_giveback_pct is not None and args.exit_quote_policy != "fresh-bid-v1":
        parser.error("Profit trailing requires --exit-quote-policy fresh-bid-v1")
    config = settings().model_copy(update={"paper_exit_quote_policy": args.exit_quote_policy,
        "paper_candidate_profit_giveback_pct": args.candidate_profit_giveback_pct})
    from app.trading import forward_zone_candidate as zone
    from app.trading import paper_exit_policy, paper_profit_trail, paper_registry
    from app.trading.forward_candidate_account import CandidateAccount
    from app.trading.forward_comparison import (
        atomic_json,
        baseline_stats,
        initialize_baseline,
    )
    from app.trading.option_quote import IST

    active_path = STATE / "active-run.json"
    previous = json.loads(active_path.read_text()) if active_path.exists() else None
    if previous and not args.replace_active:
        raise SystemExit(
            "An experiment is already selected; preserve/review it before creating another"
        )
    now = datetime.now(IST)
    previous_root = None
    if previous:
        from app.market.hours import holidays

        if (now.weekday() < 5 and now.date().isoformat() not in holidays(config)
                and "09:10" <= now.strftime("%H:%M") < "15:40"):
            raise SystemExit("Active experiments may be replaced only outside supervision hours")
        previous_root = Path(previous["directory"]).resolve()
        if not previous_root.is_relative_to(STATE / "runs"):
            raise SystemExit("Previous experiment path is outside the declared runs directory")
        for filename, sql in (
            ("baseline.db", "SELECT count(*) FROM crew_paper_trades WHERE state<>'CLOSED'"),
            ("candidate.db", "SELECT count(*) FROM trades WHERE state<>'CLOSED'"),
        ):
            with sqlite3.connect(f"file:{previous_root / filename}?mode=ro", uri=True) as source:
                if source.execute(sql).fetchone()[0]:
                    raise SystemExit("Active experiment has held/unresolved positions; replacement refused")
    run_id = "forward-" + now.strftime("%Y%m%d-%H%M%S")
    directory = STATE / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    if previous_root:
        for lane in ("baseline", "candidate"):
            with (
                sqlite3.connect(f"file:{previous_root / (lane + '.db')}?mode=ro", uri=True) as source,
                sqlite3.connect(directory / ("previous-" + lane + ".db")) as snapshot,
            ):
                source.backup(snapshot)
        atomic_json(directory / "previous-active-run.json", previous)
    prior = STATE / "journal.db"
    if prior.exists():
        source = sqlite3.connect(f"file:{prior.resolve()}?mode=ro", uri=True)
        try:
            held = source.execute(
                "SELECT count(*) FROM crew_paper_trades WHERE state<>'CLOSED'"
            ).fetchone()[0]
            if held:
                raise SystemExit(
                    "Previous paper journal has held/unresolved positions; no run activated"
                )
            with sqlite3.connect(directory / "previous-journal.db") as snapshot:
                source.backup(snapshot)
        finally:
            source.close()
    if (STATE / "status.json").exists():
        (directory / "previous-status.json").write_bytes(
            (STATE / "status.json").read_bytes()
        )
    initialize_baseline(directory / "baseline.db", config)
    candidate = CandidateAccount(directory / "candidate.db", config, run_id=run_id)
    try:
        candidate_stats = candidate.stats()
    finally:
        candidate.close()
    baseline = baseline_stats(directory / "baseline.db")
    if baseline["entries"] or candidate_stats["entries"]:
        raise SystemExit("New accounts are not empty")
    references = {}
    from app.db.paper_ledger import LOT_TABLE

    for path in (Path(config.data_dir) / "holidays/nse.yaml", LOT_TABLE):
        references[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "run_id": run_id,
        "mode": "PAPER_COMPARISON",
        "started_at": now.isoformat(),
        "directory": str(directory),
        "capital_per_account_inr": 50000,
        "notification_chat_id": config.signals_telegram_chat_id
        or config.alert_telegram_chat_id,
        "baseline_registry_hash": paper_registry.REGISTRY_HASH,
        "candidate_rules_hash": zone.RULE_HASH,
        "candidate_rules": zone.RULES,
        "reference_hashes": references,
        "previous_journal_preserved": str(prior),
        "snapshot": str(directory / "previous-journal.db"),
        "live_orders": False,
        "exit_quote_policy": args.exit_quote_policy,
        "exit_quote_policy_identity": paper_exit_policy.identity(args.exit_quote_policy),
        "previous_active_run": previous,
        "candidate_profit_giveback_pct": args.candidate_profit_giveback_pct,
        "candidate_profit_policy_identity": paper_profit_trail.identity(args.candidate_profit_giveback_pct),
        "candidate_profit_profile": paper_profit_trail.profile(args.candidate_profit_giveback_pct),
    }
    atomic_json(directory / "manifest.json", manifest)
    status = {
        "state": "PREPARED",
        "updated_at": now.isoformat(),
        "live_orders": False,
        "capital_per_account_inr": 50000,
        "blockers": [
            "Supervisor restart pending; no fresh market-session verification."
        ],
        "experiment": {
            "run_id": run_id,
            "started_at": now.isoformat(),
            "accounts": {"baseline": baseline, "candidate": candidate_stats},
            "candidate_rules": zone.RULES,
            "exit_quote_policy": args.exit_quote_policy,
            "exit_quote_policy_identity": paper_exit_policy.identity(args.exit_quote_policy),
            "candidate_profit_profile": paper_profit_trail.profile(args.candidate_profit_giveback_pct),
        },
    }
    atomic_json(directory / "status.json", status)
    if args.activate:
        atomic_json(STATE / "active-run.json", manifest)
        atomic_json(STATE / "status.json", status)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "directory": str(directory),
                "activated": args.activate,
                "accounts": {"baseline": baseline, "candidate": candidate_stats},
            },
            default=str,
        )
    )


if __name__ == "__main__":
    main()
