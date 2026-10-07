"""Fetch headlines into the market database (MARKET_DATABASE_URL). Headlines only, no article bodies.

Sources: the RSS/Atom feeds in NEWS_FEEDS (`name=url` pairs) and, with --nse-announcements, NSE's
corporate announcements. Already-stored URLs are skipped. Run it on a schedule (cron) to build a
point-in-time history; feeds only list recent items, so headlines cannot be backfilled later.

    uv run python scripts/fetch_news.py [--nse-announcements]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.types import utcnow  # noqa: E402
from app.market.news import parse_feed, parse_nse_announcements  # noqa: E402
from app.market.store import open_market_store  # noqa: E402

NSE_ANNOUNCEMENTS = "https://www.nseindia.com/api/corporate-announcements?index=equities"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*",
}


def fetch(nse_announcements: bool = False) -> dict[str, int | str]:
    import httpx

    settings = get_settings()
    now = utcnow()
    items: list[dict] = []
    report: dict[str, int | str] = {}
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for name, url in settings.news_feed_urls().items():
            try:
                response = client.get(url)
                response.raise_for_status()
                parsed = parse_feed(response.text, name, now)
            except Exception as exc:  # one broken feed must not stop the others
                report[name] = f"error: {type(exc).__name__}"
                continue
            items += parsed
            report[name] = len(parsed)
        if nse_announcements:
            try:
                client.get("https://www.nseindia.com/")  # sets the cookies the API expects
                response = client.get(NSE_ANNOUNCEMENTS, headers={"Referer": "https://www.nseindia.com/"})
                response.raise_for_status()
                parsed = parse_nse_announcements(response.text, now)
                items += parsed
                report["nse_announcements"] = len(parsed)
            except Exception as exc:
                report["nse_announcements"] = f"error: {type(exc).__name__}"
    store = open_market_store(settings)
    try:
        report["added"] = store.add_news(items)
    finally:
        store.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nse-announcements", action="store_true")
    args = parser.parse_args()
    print(fetch(args.nse_announcements))


if __name__ == "__main__":
    main()
