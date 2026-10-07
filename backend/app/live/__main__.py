"""uv run python -m app.live reconcile [--fix]
uv run python -m app.live supervise [--interval 60] [--feed replay|broker] [--poll 10]"""

from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from app.config import get_settings
from app.db.session import get_live_sessionmaker, get_sessionmaker
from app.live.approvals import reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.live")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("reconcile", help="find deployments whose research approval event is missing")
    rec.add_argument("--fix", action="store_true", help="write the missing research audit events")
    sup = sub.add_parser("supervise", help="run one Trade Crew per started deployment, restarting crashed ones")
    sup.add_argument("--interval", type=float, default=60, help="seconds between crew ticks")
    sup.add_argument("--feed", choices=("replay", "broker"), default=None, help="passed to every crew")
    sup.add_argument("--poll", type=float, default=10, help="seconds between supervisor checks")
    args = parser.parse_args(argv)

    if args.command == "supervise":
        from app.live.supervisor import Supervisor

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        Supervisor(get_settings(), get_live_sessionmaker(), interval=args.interval, feed=args.feed).run(args.poll)
        return 0

    research, live = get_sessionmaker()(), get_live_sessionmaker()()
    try:
        missing = reconcile(research, live, fix=args.fix)
    finally:
        research.close()
        live.close()
    if not missing:
        print("All deployments have a research approval event.")
        return 0
    verb = "Recorded" if args.fix else "Missing"
    for deployment_id in missing:
        print(f"{verb}: {deployment_id}")
    return 0 if args.fix else 1


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())
