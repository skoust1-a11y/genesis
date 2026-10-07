from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Bar:
    date: str
    close: float
    iv_rank: float
    dte: int


def load_sample_bars() -> list[Bar]:
    path = Path(__file__).resolve().parents[2] / "data" / "sample_nifty_options.csv"
    bars: list[Bar] = []
    with path.open(encoding="utf-8") as f:
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
