"""Shared deterministic candle admission checks (scanner-v0.1 input contract)."""
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
import math
import pandas as pd
from app.trading.option_quote import IST

SESSION_OPEN_BAR_MISSING = "SESSION_OPEN_BAR_MISSING"
INTRADAY_BAR_GAP = "INTRADAY_BAR_GAP"
PREVIOUS_SESSION_LEVELS_MISSING = "PREVIOUS_SESSION_LEVELS_MISSING"
SESSION_CALENDAR_UNVERIFIED = "SESSION_CALENDAR_UNVERIFIED"
INVALID_OR_STALE_CANDLES = "INVALID_OR_STALE_CANDLES"  # bars present but off-grid, malformed or stale
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
# Weekday sessions that did not run 09:15-15:30 uninterrupted (plus expiry.MUHURAT_SESSIONS): their bar schedule
# cannot be verified. ponytail: hand-kept; add each special session and exchange outage as NSE announces it.
EXCHANGE_HALTS = {"2021-02-24": "NSE trading halt 11:40-15:45 (telecom link failure)"}


def irregular_sessions() -> dict[str, str]:
    # Imported here: the crew read API loads closed_bars and must not load app.market.
    from app.market.expiry import MUHURAT_SESSIONS

    return {**MUHURAT_SESSIONS, **EXCHANGE_HALTS}


def _start(row) -> datetime:
    start = datetime.fromisoformat(str(row.get("date") or row.get("timestamp") or ""))
    return start.astimezone(IST).replace(tzinfo=None) if start.utcoffset() is not None else start


def closed_bars(df: pd.DataFrame | None, now: datetime, minutes: int = 5, *,
                session_only: bool = False) -> pd.DataFrame:
    """Closed ``minutes``-long bars, the last one from ``now``'s IST session. A bar is not a signal until it has
    closed. ``session_only`` also drops earlier sessions: what a detector may see (history stays for regime)."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return pd.DataFrame()
    try:
        for _, row in df.iterrows():
            opening, high, low, close = (float(row[k]) for k in ("open", "high", "low", "close"))
            if (not all(math.isfinite(v) and v > 0 for v in (opening, high, low, close))
                    or low > min(opening, close) or high < max(opening, close) or low > high):
                return pd.DataFrame()
    except (TypeError, ValueError):
        return pd.DataFrame()
    starts = []
    try:
        for _, row in df.iterrows():
            start = _start(row)
            if start.minute % minutes or start.second or start.microsecond:
                return pd.DataFrame()
            starts.append(start)
    except (TypeError, ValueError):
        return pd.DataFrame()
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return pd.DataFrame()
    clock = now.astimezone(IST).replace(tzinfo=None)
    span = timedelta(minutes=minutes)
    keep = [start + span <= clock and (not session_only or start.date() == clock.date()) for start in starts]
    completed = [start for start, include in zip(starts, keep) if include]
    if (not completed or completed[-1].date() != clock.date()
            or clock - (completed[-1] + span) >= span):
        return pd.DataFrame()
    return df.loc[keep].copy()


def regular_session(day: date, holidays: Mapping[str, str]) -> bool:
    """A weekday NSE session on the 09:15-15:30 schedule, per a holiday file that covers ``day``'s year."""
    return (str(day.year) in {h[:4] for h in holidays} and day.weekday() < 5
            and day.isoformat() not in holidays and day.isoformat() not in irregular_sessions())


def previous_session(day: date, holidays: Mapping[str, str]) -> date | None:
    """The regular session before ``day``. None when the calendar cannot say: an uncovered year, or an irregular
    session in between (whether it is the "previous day" is not defined)."""
    d = day
    for _ in range(14):
        d -= timedelta(days=1)
        if str(d.year) not in {h[:4] for h in holidays} or d.isoformat() in irregular_sessions():
            return None
        if regular_session(d, holidays):
            return d
    return None


def session_frame(df: pd.DataFrame | None, now: datetime, minutes: int,
                  holidays: Mapping[str, str]) -> tuple[pd.DataFrame, str | None]:
    """The closed bars of ``now``'s session for a detector, or (empty, reason) when that session is not a verified
    regular session or an expected closed bar is missing. Contract: an index feed prints a bar on every
    ``minutes`` grid slot from 09:15 to 15:30 (an index has no no-trade gaps). The bar still forming is never
    required, nor bars after the close (Yahoo's 15:30 closing print)."""
    clock = now.astimezone(IST).replace(tzinfo=None)
    if not regular_session(clock.date(), holidays):
        return pd.DataFrame(), SESSION_CALENDAR_UNVERIFIED
    span = timedelta(minutes=minutes)
    opening = datetime.combine(clock.date(), SESSION_OPEN)
    due = min(clock, datetime.combine(clock.date(), SESSION_CLOSE))
    required = []
    while opening + (len(required) + 1) * span <= due:
        required.append(opening + len(required) * span)
    frame = closed_bars(df, now, minutes, session_only=True)
    if not required:
        return frame, None
    try:
        present = {_start(row) for _, row in df.iterrows()} if df is not None else set()
    except (TypeError, ValueError):
        present = set()
    if required[0] not in present:
        return pd.DataFrame(), SESSION_OPEN_BAR_MISSING
    if any(start not in present for start in required):
        return pd.DataFrame(), INTRADAY_BAR_GAP
    return frame, None if not frame.empty else INVALID_OR_STALE_CANDLES


def previous_session_problem(daily: pd.DataFrame | None, now: datetime, holidays: Mapping[str, str]) -> str | None:
    """None when ``daily`` holds the previous regular session's row and no row the calendar does not know between
    it and today; otherwise why PDH/PDL cannot be verified."""
    today = now.astimezone(IST).date()
    prior = previous_session(today, holidays)
    if prior is None:
        return f"{SESSION_CALENDAR_UNVERIFIED}: no verifiable regular session before {today}"
    days = set() if daily is None or "date" not in daily else {str(d)[:10] for d in daily["date"]}
    if prior.isoformat() not in days:
        return f"no daily row for {prior}"
    unknown = sorted(d for d in days if prior.isoformat() < d < today.isoformat())
    if unknown:
        return f"daily rows {unknown} are sessions the calendar does not list"
    return None
