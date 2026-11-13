"""Import the event calendar into the market database (MARKET_DATABASE_URL).

- every `*.yaml` file in --dir (default data/events): macro events such as RBI policy and FOMC
- any --board-meetings CSV exported from NSE (Corporate Filings > Board Meetings)

Duplicates (same date, kind and title) are skipped, so the script is safe to re-run.

    uv run python scripts/import_events.py [--dir data/events] [--board-meetings board_meetings.csv]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market.calendar import load_event_yaml, parse_board_meetings  # noqa: E402
from app.market.nse import read_text  # noqa: E402
from app.market.store import open_market_store  # noqa: E402


def import_events(events_dir: Path, board_meetings: list[Path] | None = None) -> dict[str, int]:
    events: list[dict] = []
    for path in sorted(events_dir.glob("*.yaml")) if events_dir.is_dir() else []:
        events += load_event_yaml(path)
    for path in board_meetings or []:
        events += parse_board_meetings(read_text(path))
    store = open_market_store()
    try:
        added = store.add_events(events)
    finally:
        store.close()
    return {"read": len(events), "added": added}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="data/events", type=Path)
    parser.add_argument("--board-meetings", action="append", type=Path, default=[])
    args = parser.parse_args()
    print(import_events(args.dir, args.board_meetings))


if __name__ == "__main__":
    main()
