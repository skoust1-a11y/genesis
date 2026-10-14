"""uv run python -m app.equal_signals [--once]

FORWARD TEST, paper only: the equal high/low sweep (app.intraday_liquidity), the one setup whose 2015-2025 average R
per trade stayed above zero at the low end of its 95% confidence range
(DocumentsAndDiagrams/intraday_liquidity_research/EQUAL_HIGH_LOW_RESEARCH.md). Runs beside app.liquidity_signals
on the same alert channels, tagged [forward test]. Research baseline, unchanged: 2R target, 30-pt minimum risk, 5m
structure shift, entries 09:30-14:30, one per side per day, expiry days skipped, no losing-streak break; VWAP is the
session's running mean typical price of the index and ATR runs over the previous days' bars too, as in the research.
After the 15:10 exit it sends each signal's result (exit_of: stop, target, breakeven at +1R or 15:10 close).
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

from app import alerts
from app.api.market import IST, intraday_levels, trading_levels
from app.config import get_settings
from app.intraday_liquidity import atr, equal_levels, scan_levels
from app.liquidity_signals import (
    FRESH,
    LAST_ENTRY,
    charts,
    exit_of,
    message,
    option_plan,
    session_data,
    staged_exit_of,
    weekly_expiry,
)

TAG = "[forward test] equal high/low"
RESULTS = "15:20"  # 15:10 exit bar plus Yahoo's few-minute delay


def today_inputs(
    bars: list[dict], today: str, done: str
) -> tuple[list[dict], list[float], list[float]]:
    """Today's 5m bars starting at or before `done` (completed), with VWAP and ATR aligned to them."""
    bars = [b for b in bars if b["date"] <= done]
    atrs, ks = atr(bars), [k for k, b in enumerate(bars) if b["date"][:10] == today]
    if not ks:
        return [], [], []
    day, total, vwap = bars[ks[0] :], 0.0, []
    for n, b in enumerate(day, 1):
        total += (b["high"] + b["low"] + b["close"]) / 3
        vwap.append(total / n)
    return day, vwap, atrs[ks[0] :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.equal_signals")
    parser.add_argument(
        "--once",
        action="store_true",
        help=f"check once and exit (default: every 5 min until {RESULTS})",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if not alerts.signal_channels(settings):
        print(
            "no alert channel: set SIGNALS_TELEGRAM_CHAT_ID (or ALERT_TELEGRAM_CHAT_ID) in .env (printing only)"
        )
    if weekly_expiry(d := datetime.now(IST).date(), settings) == d:
        print("expiry day: no equal high/low trades")
        return 0
    seen, sent, day = set(), [], []
    while True:
        now = datetime.now(IST)
        today, done = (
            now.strftime("%Y-%m-%d"),
            (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
        )
        try:
            bars, _, daily = session_data()
        except httpx.HTTPError as exc:
            print(f"{now:%H:%M} market data unavailable, retrying: {exc}")
            bars, daily = None, []
        if bars is not None:
            day, vwap, atrs = today_inputs(bars, today, done)
        if day and bars is not None:
            for s in scan_levels(
                day,
                vwap,
                atrs,
                equal_levels(day, atrs),
                "equal",
                min_sweep=2.0,
                with_open_only=True,
                require_trendline=True,
            ):
                if s["side"] in seen:
                    continue
                seen.add(s["side"])
                if (
                    now
                    - datetime.strptime(s["time"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                    > FRESH
                ):
                    print(
                        f"missed (over {FRESH.seconds // 60} min old, not sent):\n{message(s, settings)}"
                    )
                    continue
                sent.append(s)
                plan = option_plan(s, settings)
                prev = [d for d in daily if d["date"][:10] < today]
                lvls = trading_levels(prev[-1:], session_over=True) + intraday_levels(
                    day, 3
                )
                body = "\n".join(line for line in message(s, settings, plan, lvls).splitlines()
                                 if "losses in a row" not in line)  # fmt: skip
                body = f"{TAG.upper()} (paper only, staged 1R/2R/3R)\n{body}"
                print(
                    body,
                    "| sent via",
                    alerts.send_signal(
                        settings, f"[genesis] {TAG} NIFTY {s['side'].upper()}", body
                    )
                    or "nothing",
                )
                for png in charts(day, s, lvls, plan, settings):
                    alerts.send_signal_photo(settings, f"{TAG}: {body.splitlines()[1]}", png)
        elif bars is not None and now.strftime("%H:%M") >= "10:00":
            print(
                f"{now:%H:%M} no NIFTY bars for {today} (holiday or data source down)"
            )
            break
        if args.once or now.strftime("%H:%M") > (RESULTS if sent else LAST_ENTRY):
            break
        wake = now.replace(second=0, microsecond=0) + timedelta(
            minutes=5 - now.minute % 5, seconds=60
        )
        time.sleep((wake - now).total_seconds())
    if day and datetime.now(IST).strftime("%H:%M") > RESULTS:
        for s in sent:
            res = staged_exit_of(day, s, partial_r=2.0, partial_pct=0.5, be_r=1.0)
            note = (
                f"{TAG} {s['side']} {s['time'][11:]} result: {res['exit_reason']} "
                f"at {res['exit_price']:,.2f} ({res['exit_time'][11:]}), {res['r_multiple']:+.2f}R net "
                f"(staged: 50% at +2R, BE at +1R)"
            )
            print(
                note,
                "| sent via",
                alerts.send_signal(settings, f"[genesis] {TAG} result", note) or "nothing",
            )
    return 0


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())
