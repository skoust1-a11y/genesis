from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MARKET_DATA_PATH = BACKEND_ROOT / "data" / "sample_nifty_options.csv"


@dataclass
class Bar:
    date: str
    close: float
    iv_rank: float
    dte: int


def load_sample_bars() -> list[Bar]:
    bars: list[Bar] = []
    with MARKET_DATA_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append(
                Bar(
                    date=row["date"],
                    close=float(row["close"]),
                    iv_rank=float(row["iv_rank"]),
                    dte=int(row["dte"]),
                )
            )
    return bars


def sample_data_fingerprint() -> dict:
    raw = MARKET_DATA_PATH.read_bytes()
    rows = max(len(raw.decode("utf-8").strip().splitlines()) - 1, 0)
    return {
        "source": "sample_synthetic",
        "path": str(MARKET_DATA_PATH.relative_to(BACKEND_ROOT)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": rows,
    }


def market_data_fingerprint(underlying: str | None = None) -> dict:
    """What the simulator runs on for `underlying`: imported NSE data if there is any, else the sample."""
    if underlying:
        from app.market.store import open_market_store

        store = open_market_store()
        try:
            if store.has_data(underlying):
                return store.fingerprint(underlying)
        finally:
            store.close()
    return sample_data_fingerprint()
