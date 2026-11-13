"""The NIFTY option expiry in force on a historical trade date.

NSE's schedule: monthly (last Thursday) only before 11 Feb 2019, weekly Thursday until Aug 2025, weekly Tuesday
since 1 Sep 2025; an expiry on a closed day moves to the trading day before it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import date, timedelta
from typing import Literal

WEEKLY_FROM, TUESDAY_FROM = date(2019, 2, 11), date(2025, 9, 1)
ExpiryDayPolicy = Literal["same_day", "next"]  # on an expiry day: trade that day's contract, or the following one
# Diwali Laxmi Pujan: closed for normal trading, open only for the Muhurat session, so no contract expires on it
# (absent from the holiday file because a session runs). NSE F&O bhavcopy: the Diwali-week NIFTY expiry was
# 2021-11-03 (fo03NOV2021bhav) and 2025-10-20 (BhavCopy_NSE_FO_..._20251020); neither Muhurat day lists its own.
# ponytail: add each year's Laxmi Pujan date when it falls on a weekday.
MUHURAT_SESSIONS = {"2021-11-04": "Diwali Laxmi Pujan (Muhurat)", "2025-10-21": "Diwali Laxmi Pujan (Muhurat)"}


class ExpiryUnresolved(ValueError):
    """The contract in force cannot be determined reliably; callers must not price or trade it."""


def scheduled_expiry(day: date) -> date:
    """NSE's nominal nearest NIFTY expiry on or after `day`, before any holiday move."""
    if day < WEEKLY_FROM:
        exp = day
        for _ in range(2):  # this month's last Thursday, or next month's once it has passed
            nxt = date(exp.year + exp.month // 12, exp.month % 12 + 1, 1)
            exp = nxt - timedelta(days=(nxt.weekday() - 3) % 7 or 7)
            if exp >= day:
                break
            exp = nxt
        return exp
    thu = day + timedelta(days=(3 - day.weekday()) % 7)
    return thu if thu < TUESDAY_FROM else day + timedelta(days=(1 - day.weekday()) % 7)


def roll_back(exp: date, day: date, closed: Callable[[date], bool]) -> date:
    """`exp` moved to the trading day before it while it is closed, never earlier than `day`."""
    while exp > day and closed(exp):
        exp -= timedelta(days=1)
    return exp


def resolve_expiry(
    day: date,
    *,
    underlying: str = "NIFTY",
    holidays: Mapping[str, str],
    listed: Collection[str] | None = None,
    policy: ExpiryDayPolicy = "same_day",
) -> date:
    """The contract a trade on `day` uses. With `listed` (expiries actually listed on the exchange) the nearest one is
    taken as fact; otherwise NSE's schedule, which needs `holidays` to cover the expiry's year.
    Raises ExpiryUnresolved when neither can say."""
    if policy not in ("same_day", "next"):
        raise ExpiryUnresolved(f"unknown expiry-day policy {policy!r}")
    after = day if policy == "same_day" else day + timedelta(days=1)
    if listed is not None:
        later = sorted(e for e in listed if e >= after.isoformat())
        if not later:
            raise ExpiryUnresolved(f"no listed {underlying} expiry on or after {after}")
        return date.fromisoformat(later[0])
    if underlying.upper() != "NIFTY":
        raise ExpiryUnresolved(f"no expiry schedule for {underlying}; pass the listed expiries")
    covered = {h[:4] for h in holidays}

    def closed(d: date) -> bool:
        return d.weekday() >= 5 or d.isoformat() in holidays or d.isoformat() in MUHURAT_SESSIONS

    exp = roll_back(scheduled_expiry(day), day, closed)
    while exp < after or closed(exp):  # today's expiry under "next", or a closed day itself: the one after it
        nxt = max(after, exp + timedelta(days=1))
        exp = roll_back(scheduled_expiry(nxt), nxt, closed)
    if str(exp.year) not in covered:
        raise ExpiryUnresolved(f"no exchange holiday calendar for {exp.year}: cannot place the {underlying} expiry")
    return exp
