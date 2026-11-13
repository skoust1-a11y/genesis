"""uv run python -m app.agents.trade_crew run --deployment ID [--feed replay|broker] [--interval 60] [--max-ticks N]
[--explain]"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from app.agents.trade_crew.runner import run
from app.brokers.registry import FEEDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agents.trade_crew")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the Trade Crew for one deployment")
    r.add_argument("--deployment", required=True)
    r.add_argument(
        "--feed", choices=FEEDS, default=None,
        help="replay: bundled/imported bars (paper only); broker: the account broker's live quotes "
        "(default: broker for live deployments, replay otherwise)",
    )  # fmt: skip
    r.add_argument("--interval", type=float, default=60, help="seconds between ticks")
    r.add_argument("--max-ticks", type=int, default=None)
    r.add_argument(
        "--explain", action=argparse.BooleanOptionalAction, default=None,
        help="have an LLM write reason text after each decision (default: TRADE_CREW_EXPLAIN)",
    )  # fmt: skip
    args = parser.parse_args(argv)
    try:
        run(args.deployment, interval=args.interval, max_ticks=args.max_ticks, explain=args.explain, feed=args.feed)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())
