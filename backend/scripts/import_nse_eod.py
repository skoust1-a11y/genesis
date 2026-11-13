"""Import NSE end-of-day data into the market database (MARKET_DATABASE_URL).

Files can be dropped into a raw directory and are classified by name:
- F&O bhavcopy (legacy `fo02JAN2024bhav.csv[.zip]` or UDiFF `BhavCopy_NSE_FO_..._F_0000.csv[.zip]`):
  any name containing "bhav"
- India VIX history: any name containing "vix" (the IV rank of every imported underlying)
- Index closes: `index_<UNDERLYING>*.csv`, for example `index_NIFTY_2024.csv`

Files already imported (same SHA-256) are skipped, so the script is safe to re-run.

    uv run python scripts/import_nse_eod.py [--raw-dir data/nse/raw] [--underlyings NIFTY]
    uv run python scripts/import_nse_eod.py --download-from 2024-01-01 --download-to 2024-03-31
    uv run python scripts/import_nse_eod.py --history-from 2024-01-01 --download-from 2025-01-01

NSE often blocks scripted downloads; when it does, download the files in a browser and drop them
into the raw directory instead.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market.nse import bhavcopy_urls  # noqa: E402
from app.market.store import open_market_store  # noqa: E402

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}


def classify(path: Path) -> tuple[str, str | None] | None:
    name = path.name.lower()
    if not name.endswith((".csv", ".zip")):
        return None
    if "bhav" in name:
        return "bhavcopy", None
    if "vix" in name:
        return "vix", "NIFTY"
    if name.startswith("index_"):
        return "index", path.stem.split("_")[1].upper()
    return None


def download_bhavcopies(raw_dir: Path, start: date, end: date, pause: float = 1.0) -> list[Path]:
    import httpx

    raw_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=30) as client:
        client.get("https://www.nseindia.com/")  # sets the cookies the archive expects
        day = start
        while day <= end:
            if day.weekday() < 5:
                for url in bhavcopy_urls(day):
                    target = raw_dir / url.rsplit("/", 1)[1]
                    if target.exists():
                        saved.append(target)
                        break
                    response = client.get(url)
                    if response.status_code == 200 and response.content[:2] == b"PK":
                        target.write_bytes(response.content)
                        saved.append(target)
                        break
                time.sleep(pause)
            day += timedelta(days=1)
    return saved


HISTORY_API = "https://www.nseindia.com/api/historicalOR"
INDEX_NAMES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
HISTORY_CHUNK_DAYS = 90  # the API caps how many days one request returns


def download_history(raw_dir: Path, underlyings: list[str], start: date, end: date, pause: float = 1.0) -> list[Path]:
    """Daily open/high/low/close for each index and India VIX, from NSE's history API, written as CSVs
    that `classify` and `parse_daily_bars` understand (`index_<UNDERLYING>_...csv`, `vix_...csv`)."""
    import csv

    import httpx

    raw_dir.mkdir(parents=True, exist_ok=True)
    series = [(f"index_{u}", f"indicesHistory?indexType={INDEX_NAMES[u]}") for u in underlyings if u in INDEX_NAMES]
    series.append(("vix", "vixhistory?"))
    saved = []
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=30) as client:
        client.get("https://www.nseindia.com/")
        for prefix, query in series:
            chunk = start
            while chunk <= end:
                last = min(chunk + timedelta(days=HISTORY_CHUNK_DAYS - 1), end)
                target = raw_dir / f"{prefix}_{chunk:%Y%m%d}_{last:%Y%m%d}.csv"
                if not target.exists():
                    sep = "" if query.endswith("?") else "&"
                    url = f"{HISTORY_API}/{query}{sep}from={chunk:%d-%m-%Y}&to={last:%d-%m-%Y}"
                    response = client.get(url)
                    rows = response.json().get("data", []) if response.status_code == 200 else []
                    if rows:
                        with target.open("w", newline="") as f:
                            writer = csv.writer(f)
                            writer.writerow(["Date", "Open", "High", "Low", "Close"])
                            for r in rows:
                                writer.writerow([
                                    r["EOD_TIMESTAMP"], r["EOD_OPEN_INDEX_VAL"], r["EOD_HIGH_INDEX_VAL"],
                                    r["EOD_LOW_INDEX_VAL"], r["EOD_CLOSE_INDEX_VAL"],
                                ])  # fmt: skip
                    time.sleep(pause)
                if target.exists():
                    saved.append(target)
                chunk = last + timedelta(days=1)
    return saved


def import_dir(raw_dir: Path, underlyings: list[str], force: bool = False) -> dict[str, int]:
    counts = {"bhavcopy": 0, "index": 0, "vix": 0, "skipped": 0}
    store = open_market_store()
    try:
        files = sorted(raw_dir.iterdir()) if raw_dir.is_dir() else []
        # Index closes and option rows first; VIX last so IV rank is computed over the full history.
        ordered = sorted(
            (f for f in files if classify(f)), key=lambda f: {"index": 0, "bhavcopy": 1, "vix": 2}[classify(f)[0]]
        )
        for path in ordered:
            kind, underlying = classify(path)
            if kind == "bhavcopy":
                n = store.import_bhavcopy(path, underlyings, force=force)
            elif kind == "index":
                n = store.import_index_closes(path, underlying)
            else:
                # India VIX is the only volatility index NSE publishes; every underlying's IV rank uses it.
                n = sum(store.import_vix(path, u, force=force or i > 0) for i, u in enumerate(underlyings))
            counts[kind if n else "skipped"] += n if n else 1
            print(f"{path.name}: {kind} {n} rows" if n else f"{path.name}: already imported")
        for u in underlyings:
            store.recompute_iv_rank(u)
        store.session.commit()
        for u in underlyings:
            print(store.fingerprint(u))
    finally:
        store.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", default="data/nse/raw", type=Path)
    parser.add_argument("--underlyings", default="NIFTY", help="comma-separated, e.g. NIFTY,BANKNIFTY")
    parser.add_argument("--download-from", type=date.fromisoformat)
    parser.add_argument("--download-to", type=date.fromisoformat)
    parser.add_argument(
        "--history-from", type=date.fromisoformat,
        help="also download index and India VIX daily history from this date (start a year before the "
        "bhavcopies so IV rank has a full lookback)",
    )  # fmt: skip
    parser.add_argument(
        "--force", action="store_true",
        help="re-read files imported before, for example to add an underlying (rows are upserted)",
    )  # fmt: skip
    args = parser.parse_args()
    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    if args.history_from:
        saved = download_history(args.raw_dir, underlyings, args.history_from, args.download_to or date.today())
        print(f"downloaded or found {len(saved)} index and VIX history files in {args.raw_dir}")
    if args.download_from:
        saved = download_bhavcopies(args.raw_dir, args.download_from, args.download_to or date.today())
        print(f"downloaded or found {len(saved)} bhavcopies in {args.raw_dir}")
    print(import_dir(args.raw_dir, underlyings, force=args.force))


if __name__ == "__main__":
    main()
