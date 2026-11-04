"""One stock flag, used only when the Nifty retest and trendline scans are empty.

The distances that are Nifty points on the index are scaled by each stock's own
5-minute true range. The flag is not run on Nifty. A volume check is not applied to Nifty.
"""

from __future__ import annotations

from typing import Any

LIQUID_STOCKS = (
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "AXISBANK",
)


def volume_ok(symbol: str, bars: list[dict]) -> bool:
    """Nifty is never rejected for volume. A stock needs the last bar above its own prior 20-bar average."""
    if symbol.upper() in {"NIFTY", "^NSEI"}:
        return True
    if len(bars) < 21:
        return False
    last = float(bars[-1].get("volume") or 0)
    if last <= 0:
        return False
    prior = [float(b.get("volume") or 0) for b in bars[-21:-1]]
    avg = sum(prior) / len(prior)
    return avg > 0 and last > avg


def stock_median_tr(bars: list[dict]) -> float:
    if not bars:
        return 0.0
    trs = []
    prev = None
    for b in bars:
        high, low = float(b["high"]), float(b["low"])
        if prev is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - prev), abs(low - prev)))
        prev = float(b["close"])
    trs.sort()
    return trs[len(trs) // 2]


def _vwap(bars: list[dict]) -> float | None:
    if not bars:
        return None
    num = den = 0.0
    for b in bars:
        vol = float(b.get("volume") or 0)
        tp = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        if vol > 0:
            num += tp * vol
            den += vol
    if den > 0:
        return num / den
    return sum((float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0 for b in bars) / len(bars)


def scan_stock_flag(
    symbol: str,
    bars: list[dict],
    nifty_median_tr: float,
    nifty_below_vwap: bool,
    pdh: float | None = None,
    pdl: float | None = None,
) -> dict[str, Any] | None:
    """Push, 1 to 3 tight pause bars, then a closed candle that breaks the prior bar.

    The last bar is the only signal. A pause bar, or a bar that has not closed, is not an entry.
    """
    if symbol.upper() in {"NIFTY", "^NSEI"} or nifty_median_tr <= 0 or not volume_ok(symbol, bars):
        return None
    if bars and bars[-1].get("closed") is False:
        return None
    med = stock_median_tr(bars)
    if med <= 0:
        return None
    scale = med / nifty_median_tr
    min_breakout = 2.0 * scale
    max_retest_depth = 25.0 * scale
    # Stop is the pause extreme. The Nifty 5-point pad is not added past it.
    vwap = _vwap(bars)
    day_open = float(bars[0]["open"])

    for n_pause in (1, 2, 3):
        need = n_pause + 2
        if len(bars) < need:
            continue
        push = bars[-need]
        pauses = bars[-need + 1 : -1]
        signal = bars[-1]
        push_range = float(push["high"]) - float(push["low"])
        if push_range < 1.5 * med:
            continue
        long = float(push["close"]) > float(push["open"])
        short = float(push["close"]) < float(push["open"])
        if not long and not short:
            continue
        if long and nifty_below_vwap:
            continue
        if long:
            giveback = float(push["high"]) - min(float(p["low"]) for p in pauses)
        else:
            giveback = max(float(p["high"]) for p in pauses) - float(push["low"])
        if giveback >= 0.5 * push_range or giveback > max_retest_depth:
            continue
        prior = pauses[-1]
        close = float(signal["close"])
        opened = float(signal["open"])
        body = abs(close - opened)
        if long:
            if close <= opened or float(signal["high"]) < float(prior["high"]) + min_breakout:
                continue
            wick = float(signal["high"]) - max(opened, close)
            if wick > max(body * 1.5, 4.0 * scale):
                continue
            if vwap is not None and close < vwap:
                continue
            if pdl is not None and close < float(pdl):
                continue
            stop = min(float(p["low"]) for p in pauses)
            target = float(push["high"])
            side = "LONG"
        else:
            if close >= opened or float(signal["low"]) > float(prior["low"]) - min_breakout:
                continue
            wick = min(opened, close) - float(signal["low"])
            if wick > max(body * 1.5, 4.0 * scale):
                continue
            if vwap is not None and close > vwap:
                continue
            if pdh is not None and close > float(pdh):
                continue
            stop = max(float(p["high"]) for p in pauses)
            target = float(push["low"])
            side = "SHORT"
        pct = (close - day_open) / day_open * 100 if day_open else 0.0
        return {
            "symbol": symbol,
            "side": side,
            "entry": round(close, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "pct_from_open": pct,
            "day": str(signal.get("date") or "")[:10],
        }
    return None


def pick_stock_line(
    stock_bars: dict[str, dict[str, Any]],
    nifty_median_tr: float,
    nifty_below_vwap: bool,
    already_sent: set[tuple[str, str]] | None = None,
) -> str | None:
    """One line, for the qualifying stock with the largest percent gain from today's open."""
    found = []
    for symbol in LIQUID_STOCKS:
        pack = stock_bars.get(symbol)
        if not pack:
            continue
        hit = scan_stock_flag(
            symbol, pack.get("bars") or [], nifty_median_tr, nifty_below_vwap,
            pdh=pack.get("pdh"), pdl=pack.get("pdl"),
        )
        if hit is None:
            continue
        key = (hit["day"], symbol)
        if already_sent is not None and key in already_sent:
            continue
        found.append(hit)
    if not found:
        return None
    best = max(found, key=lambda s: s["pct_from_open"])
    if already_sent is not None:
        already_sent.add((best["day"], best["symbol"]))
    return f"{best['symbol']} {best['side']} entry {best['entry']} stop {best['stop']} target {best['target']}"
