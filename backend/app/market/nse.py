"""Parsers for NSE end-of-day files and the IV-rank calculation.

Supported inputs:
- F&O bhavcopy, both the legacy layout (`INSTRUMENT,SYMBOL,EXPIRY_DT,...`, until July 2024) and the
  UDiFF layout (`TradDt,...,TckrSymb,XpryDt,StrkPric,OptnTp,...`). Plain CSV or the zip NSE serves.
  Legacy rows carry no lot size; it is inferred from turnover, then from `data/lot_sizes/nse.yaml`.
- Daily index closes and India VIX history as exported from the NSE website (any CSV with a
  date column and a close column).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y", "%d%b%Y", "%d-%b-%y")
INDEX_OPTION_TYPES = {"IDO"}  # UDiFF FinInstrmTp for index options
LEGACY_INDEX_OPTIONS = {"OPTIDX"}


@dataclass
class OptionRow:
    date: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    close: float | None
    settle: float | None
    volume: int
    open_interest: int | None
    lot_size: int | None
    underlying_price: float | None


def parse_date(value: str) -> str:
    text = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {value!r}")


def _num(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", "")
    if text in ("", "-", "NA"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def read_text(path: Path) -> str:
    """A CSV file, or the first CSV inside a zip."""
    raw = path.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if name is None:
                raise ValueError(f"{path.name}: zip has no CSV inside")
            raw = zf.read(name)
    return raw.decode("utf-8-sig")


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def bhavcopy_layout(text: str) -> str:
    header = {h.strip() for h in text.splitlines()[0].split(",")} if text.strip() else set()
    if "TckrSymb" in header:
        return "udiff"
    if "INSTRUMENT" in header:
        return "legacy"
    raise ValueError("not an F&O bhavcopy: expected a UDiFF (TckrSymb) or legacy (INSTRUMENT) header")


def parse_fo_bhavcopy(text: str, underlyings: Iterable[str] | None = None) -> list[OptionRow]:
    """Index option rows from an F&O bhavcopy, optionally limited to some underlyings."""
    wanted = {u.upper() for u in underlyings} if underlyings else None
    layout = bhavcopy_layout(text)
    out: list[OptionRow] = []
    for row in _rows(text):
        if layout == "udiff":
            if row.get("FinInstrmTp") not in INDEX_OPTION_TYPES:
                continue
            symbol = row.get("TckrSymb", "").upper()
            expiry_raw = row.get("FininstrmActlXpryDt") or row.get("XpryDt", "")
            fields = {
                "date": row.get("TradDt", ""),
                "strike": row.get("StrkPric"),
                "option_type": row.get("OptnTp", ""),
                "close": row.get("ClsPric"),
                "settle": row.get("SttlmPric"),
                "volume": row.get("TtlTradgVol"),
                "oi": row.get("OpnIntrst"),
                "lot": row.get("NewBrdLotQty"),
                "spot": row.get("UndrlygPric"),
            }
        else:
            if row.get("INSTRUMENT") not in LEGACY_INDEX_OPTIONS:
                continue
            symbol = row.get("SYMBOL", "").upper()
            expiry_raw = row.get("EXPIRY_DT", "")
            fields = {
                "date": row.get("TIMESTAMP", ""),
                "strike": row.get("STRIKE_PR"),
                "option_type": row.get("OPTION_TYP", ""),
                "close": row.get("CLOSE"),
                "settle": row.get("SETTLE_PR"),
                "volume": row.get("CONTRACTS"),
                "oi": row.get("OPEN_INT"),
                "lot": infer_legacy_lot(
                    row.get("CONTRACTS"), row.get("VAL_INLAKH"), row.get("STRIKE_PR"), row.get("CLOSE")
                ),
                "spot": None,
            }
        if wanted is not None and symbol not in wanted:
            continue
        option_type = fields["option_type"].upper()
        strike = _num(fields["strike"])
        if option_type not in ("CE", "PE") or strike is None:
            continue
        out.append(
            OptionRow(
                date=parse_date(fields["date"]),
                underlying=symbol,
                expiry=parse_date(expiry_raw),
                strike=strike,
                option_type=option_type,
                close=_num(fields["close"]),
                settle=_num(fields["settle"]),
                volume=_int(fields["volume"]) or 0,
                open_interest=_int(fields["oi"]),
                lot_size=fields["lot"] if isinstance(fields["lot"], int) else _int(fields["lot"]),
                underlying_price=_num(fields["spot"]),
            )
        )
    if layout == "legacy":
        _common_expiry_lots(out)
    return out


def infer_legacy_lot(contracts: str | None, value_lakh: str | None, strike: str | None, price: str | None) -> int | None:
    """Lot size of a legacy-bhavcopy option row, which has no lot column. Option turnover there is
    notional: contracts x lot x (strike + premium). Using the close for the average premium is off by
    well under one unit, and every NSE index lot size is a multiple of 5."""
    n, value, k = _num(contracts), _num(value_lakh), _num(strike)
    if not n or not value or not k:
        return None
    raw = value * 100_000 / (n * (k + (_num(price) or 0)))
    lot = round(raw / 5) * 5
    return lot if lot > 0 and abs(raw - lot) < 1.5 else None


def _common_expiry_lots(rows: list[OptionRow]) -> None:
    """Every contract of an expiry takes the lot size most of its traded contracts imply that day.

    One expiry has one lot size, so this fills untraded contracts and overrides the odd rounding outlier.
    """
    seen: dict[tuple[str, str], Counter[int]] = {}
    for r in rows:
        if r.lot_size:
            seen.setdefault((r.underlying, r.expiry), Counter())[r.lot_size] += 1
    for r in rows:
        if (r.underlying, r.expiry) in seen:
            r.lot_size = seen[(r.underlying, r.expiry)].most_common(1)[0][0]


@dataclass
class LotChange:
    underlying: str
    from_date: str  # first trading day the lot size applies to newly traded contracts
    lot_size: int


def load_lot_sizes(path: Path) -> list[LotChange]:
    import yaml

    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    changes = [
        LotChange(str(c["underlying"]).upper(), parse_date(str(c["from"])), int(c["lot_size"]))
        for c in raw.get("changes", [])
    ]
    return sorted(changes, key=lambda c: (c.underlying, c.from_date))


def lot_size_on(changes: list[LotChange], underlying: str, day: str) -> int | None:
    """The lot size in force on `day` from the dated table; None before its first entry."""
    current = None
    for c in changes:
        if c.underlying == underlying.upper() and c.from_date <= day:
            current = c.lot_size
    return current


def _find_column(header: Iterable[str], *, exact: tuple[str, ...], contains: str) -> str | None:
    cols = list(header)
    lowered = {c: c.strip().lower() for c in cols}
    for c in cols:
        if lowered[c] in exact:
            return c
    return next((c for c in cols if contains in lowered[c]), None)


@dataclass
class DailyBar:
    date: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None


def parse_daily_bars(text: str) -> list[DailyBar]:
    """Daily bars from an NSE index or India VIX history export, oldest first. Open, high and low
    are kept when the file has them; the close is required."""
    rows = _rows(text)
    if not rows:
        return []
    header = rows[0]
    date_col = _find_column(header, exact=("date", "timestamp"), contains="date")
    close_col = _find_column(header, exact=("close", "closing", "close price", "closing index value"), contains="clos")
    if date_col is None or close_col is None:
        raise ValueError("expected a date column and a close column")
    open_col = _find_column(
        header, exact=("open", "open price", "open index value", "opening index value"), contains="open"
    )
    high_col = _find_column(header, exact=("high", "high price", "high index value"), contains="high")
    low_col = _find_column(header, exact=("low", "low price", "low index value"), contains="low")
    out: dict[str, DailyBar] = {}
    for row in rows:
        close = _num(row.get(close_col))
        if close is None:
            continue
        try:
            day = parse_date(row.get(date_col, ""))
        except ValueError:
            continue
        out[day] = DailyBar(
            date=day,
            close=close,
            open=_num(row.get(open_col)) if open_col else None,
            high=_num(row.get(high_col)) if high_col else None,
            low=_num(row.get(low_col)) if low_col else None,
        )
    return [out[d] for d in sorted(out)]


def parse_daily_closes(text: str) -> list[tuple[str, float]]:
    """(date, close) pairs from an NSE index or India VIX history export, oldest first."""
    return [(b.date, b.close) for b in parse_daily_bars(text)]


def iv_rank_series(values: list[float | None], lookback: int, min_history: int) -> list[float | None]:
    """IV rank of each value against the trailing `lookback` values up to and including that day.

    Only past and same-day values are used, so a rank never depends on later data. Days with
    fewer than `min_history` observations, or a flat window, have no rank.
    """
    window: deque[float] = deque(maxlen=lookback)
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        window.append(v)
        lo, hi = min(window), max(window)
        if len(window) < min_history or hi == lo:
            out.append(None)
        else:
            out.append(round((v - lo) / (hi - lo) * 100, 2))
    return out


def bhavcopy_urls(day: date) -> list[str]:
    """Archive URLs NSE has used for a day's F&O bhavcopy, newest layout first."""
    udiff = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    legacy = (
        "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
        f"{day:%Y}/{day.strftime('%b').upper()}/fo{day.strftime('%d%b%Y').upper()}bhav.csv.zip"
    )
    return [udiff, legacy] if day >= date(2024, 7, 8) else [legacy, udiff]
