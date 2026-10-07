"""uv run python -m app.live reconcile [--fix]"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from app.db.session import get_live_sessionmaker, get_sessionmaker
from app.live.approvals import reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.live")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("reconcile", help="find deployments whose research approval event is missing")
    rec.add_argument("--fix", action="store_true", help="write the missing research audit events")
    args = parser.parse_args(argv)

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
