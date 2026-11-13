"""Intraday NIFTY liquidity research: the strategies in app.intraday_liquidity and the existing PDH/PDL strategy
(app.liquidity_signals.scan, unchanged) on identical data, exits and costs. Writes the reports, charts and an Excel
workbook of the same tables plus every trade to DocumentsAndDiagrams/intraday_liquidity_research/.

    uv run --with openpyxl python scripts/research_intraday_liquidity.py [--history data/intraday/NIFTY_1MIN_2015-2025.csv]

Data: the Kaggle NIFTY 1-minute CSV rolled to 5m, offline. VWAP: the session's time-weighted average typical price
(the index has no volume), as scripts/backtest_liquidity.py --history. Exits: app.liquidity_signals.exit_of for every
strategy (stop, stop to entry after +1R, target, else 15:10). Results are on the index in R (1R = the signal's stop
distance) after COST points a round trip. No historical option prices exist here, so option P&L is not reported.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.liquidity_signals as ls
from app.config import get_settings
from app.engine.metrics import performance
from app.intraday_liquidity import (
    CONS_ATR,
    CONS_BARS,
    CONS_OVERLAP,
    EQ_GAP,
    EQ_PULL,
    EQ_TOL,
    MSS_BARS,
    RECLAIM_BARS,
    TICK,
    _level,
    atr,
    day_levels,
    group_events,
    nearest_opposing,
    scan_levels,
)  # fmt: skip
from app.liquidity_signals import MIN_RISK, R_MULT, exit_of, scan
from scripts.backtest_liquidity import expiry_for, kaggle_5m

# Memoised for speed only (strftime/strptime dominate a 10-year run); the existing strategy's code is unchanged.
ls._at, ls._bucket = lru_cache(maxsize=1 << 16)(ls._at), lru_cache(maxsize=1 << 16)(ls._bucket)
_at = ls._at

COST = 2.0  # index pts a round trip: charges + spread + slippage of an ATM option at delta ~0.5
SPLIT = "2021-01-01"  # in-sample before, out-of-sample from
OUT = Path(__file__).resolve().parents[2] / "DocumentsAndDiagrams" / "intraday_liquidity_research"
NAMES = {"pdh_pdl": "PDH/PDL sweep (existing)", "swing": "Intraday swing sweep", "or15": "Opening range 15m sweep",
         "or30": "Opening range 30m sweep", "equal": "Equal high/low sweep", "range": "Consolidation sweep",
         "hour": "Previous hour sweep"}  # fmt: skip
NEW = [k for k in NAMES if k != "pdh_pdl"]
BASE_TARGET = {k: (R_MULT if k == "pdh_pdl" else 2.0) for k in NAMES}
TARGETS = [1.0, 1.5, 2.0, 2.5, 3.0, "liquidity"]
VARIANTS = {"base": {}, "min risk 0": {"min_risk": 0}, "min risk 10": {"min_risk": 10},
            "min risk 20": {"min_risk": 20}, "no structure shift": {"mss_tf": 0}, "15m structure shift": {"mss_tf": 15}}  # fmt: skip
FEATURES = ["tf", "touches", "external", "n_levels", "age_min", "depth_atr", "reclaim_atr", "displacement_atr",
            "vwap_atr", "room_r", "with_open", "bars_to_entry", "vol_ratio", "hour"]  # fmt: skip


# Data


def sessions(path: Path) -> dict[str, tuple[list[dict], list[float], list[float]]]:
    """day -> (5m bars, ATR, running VWAP proxy), each aligned to the bars."""
    bars = kaggle_5m(path)
    atrs, idx = atr(bars), defaultdict(list)
    for k, b in enumerate(bars):
        idx[b["date"][:10]].append(k)
    out = {}
    for d, ks in idx.items():
        day, total, vwap = bars[ks[0] : ks[-1] + 1], 0.0, []
        for n, b in enumerate(day, 1):
            total += (b["high"] + b["low"] + b["close"]) / 3
            vwap.append(total / n)
        out[d] = (day, atrs[ks[0] : ks[-1] + 1], vwap)
    return out


def regimes(sess: dict) -> dict[str, dict]:
    """Per day. Ex ante (known before or early in the day, usable as filters): `vol` from the last 10 days' mean
    range against the last 250 days' median, `open` from the 09:15-09:45 move. Ex post (descriptive only): `trend`
    from the day's body / range, `orb` from 09:45-12:00 trade against the 30m opening range."""
    out, ranges = {}, []
    for d in sorted(sess):
        day = sess[d][0]
        o, h, lo, c = day[0]["open"], max(b["high"] for b in day), min(b["low"] for b in day), day[-1]["close"]
        avg10 = statistics.fmean(ranges[-10:]) if ranges else h - lo
        med = statistics.median(ranges[-250:]) if len(ranges) >= 60 else None
        ratio = avg10 / med if med else None
        vol = "normal vol" if ratio is None or 0.8 <= ratio <= 1.2 else "high vol" if ratio > 1.2 else "low vol"
        body = (c - o) / (h - lo) if h > lo else 0.0
        trend = ("trending bull" if body >= 0.5 else "trending bear" if body <= -0.5
                 else "sideways" if abs(body) <= 0.25 else "mixed")  # fmt: skip
        first = [b for b in day if b["date"][11:] < "09:45"]
        strong = bool(first) and abs(first[-1]["close"] - o) >= 0.35 * avg10
        orb = "no OR break"
        if first:
            orh, orl = max(b["high"] for b in first), min(b["low"] for b in first)
            later = [b for b in day if "09:45" <= b["date"][11:] < "12:00"]
            k = next((k for k, b in enumerate(later) if b["high"] > orh + TICK or b["low"] < orl - TICK), None)
            if k is not None:
                up = later[k]["high"] > orh + TICK
                back = any((b["close"] < orh) if up else (b["close"] > orl) for b in later[k + 1 :])
                orb = "failed OR break" if back else "OR break held"
        out[d] = {"vol": vol, "vol_ratio": round(ratio, 3) if ratio else None, "trend": trend, "orb": orb,
                  "open": "strong open" if strong else "normal open"}  # fmt: skip
        ranges.append(h - lo)
    return out


# Signals and exits


def signals(sess: dict, settings) -> tuple[dict, list[str], dict]:
    """{(strategy, variant): signals}, the days scanned (not the first, not expiry days) and descriptive counts."""
    trading, dates = set(sess), sorted(sess)
    out, scanned = defaultdict(list), []
    desc = {"levels": defaultdict(int), "or": defaultdict(int), "range": defaultdict(int)}
    for n, d in enumerate(dates):
        if not n or expiry_for(date.fromisoformat(d), trading, settings).isoformat() == d:
            continue
        scanned.append(d)
        day, atrs, vwap = sess[d]
        prev = sess[dates[n - 1]][0]
        pdh, pdl = max(b["high"] for b in prev), min(b["low"] for b in prev)
        lv = day_levels(day, atrs)
        pool = [x for v in lv.values() for x in v] + [_level("pd", "high", pdh, 0, prev[0]["date"], 1440),
                                                       _level("pd", "low", pdl, 0, prev[0]["date"], 1440)]  # fmt: skip
        for k, v in lv.items():
            desc["levels"][k] += len(v)
        describe(day, lv, desc)
        above = {b["date"]: b["close"] > v for b, v in zip(day, vwap)}
        for s in scan(day, above, pdh, pdl):
            i = next(k for k, b in enumerate(day) if _at(b["date"], 5) == s["time"])
            s.update(strategy="pdh_pdl", sweep=f"{d} {s['why'][0][:5]}", levels=["pd"], features={},
                     opp=nearest_opposing(pool, day, i, s["side"] == "Long", s["entry"], s["risk"]))  # fmt: skip
            out["pdh_pdl", "base"].append(s)
        for k in NEW:
            for v, kw in VARIANTS.items():
                out[k, v] += scan_levels(day, vwap, atrs, lv[k], k, opposing=pool, **kw)
    return out, scanned, desc


def describe(day: list[dict], lv: dict, desc: dict) -> None:
    """Opening range breakouts (false = a close back inside within 30 min; continuation = one more range width) and
    consolidation box breakouts (continuation = one box height past the break before a close past the far side)."""
    for k in ("or15", "or30"):
        if len(lv[k]) < 2:
            continue
        hi, lo = lv[k][0]["price"], lv[k][1]["price"]
        rest = day[lv[k][0]["known"] :]
        j = next((j for j, b in enumerate(rest) if b["high"] > hi + TICK or b["low"] < lo - TICK), None)
        desc["or"][k, "days"] += 1
        if j is None:
            continue
        up, w = rest[j]["high"] > hi + TICK, hi - lo
        desc["or"][k, "broken"] += 1
        desc["or"][k, "false"] += any((b["close"] < hi) if up else (b["close"] > lo) for b in rest[j : j + 7])
        desc["or"][k, "continued"] += any((b["high"] >= hi + w) if up else (b["low"] <= lo - w) for b in rest[j:])
    boxes = lv["range"]
    for hi_lv, lo_lv in zip(boxes[::2], boxes[1::2]):
        hi, lo, rest = hi_lv["price"], lo_lv["price"], day[hi_lv["known"] :]
        desc["range"]["boxes"] += 1
        j = next((j for j, b in enumerate(rest) if b["close"] > hi or b["close"] < lo), None)
        if j is None:
            continue
        up, w = rest[j]["close"] > hi, hi - lo
        desc["range"]["broken"] += 1
        for b in rest[j + 1 :]:
            if (b["high"] >= hi + w) if up else (b["low"] <= lo - w):
                desc["range"]["continued"] += 1
                break
            if (b["close"] < lo) if up else (b["close"] > hi):
                desc["range"]["reversed"] += 1
                break


def simulate(day: list[dict], s: dict, target: float) -> dict:
    reason, price, at = exit_of(day, {**s, "target": target})
    sign = 1 if s["side"] == "Long" else -1
    held = [b for b in day if s["time"] <= b["date"] and _at(b["date"], 5) <= at] or [day[-1]]
    adverse = max((s["entry"] - b["low"]) if sign > 0 else (b["high"] - s["entry"]) for b in held)
    favour = max((b["high"] - s["entry"]) if sign > 0 else (s["entry"] - b["low"]) for b in held)
    points = (price - s["entry"]) * sign
    mins = (datetime.strptime(at, "%Y-%m-%d %H:%M") - datetime.strptime(s["time"], "%Y-%m-%d %H:%M")).seconds // 60
    return {"exit": reason, "exit_time": at[11:], "points": round(points, 2), "r": round((points - COST) / s["risk"], 3),
            "mae_r": round(max(adverse, 0) / s["risk"], 3), "mfe_r": round(max(favour, 0) / s["risk"], 3), "hold_min": mins}  # fmt: skip


def tod(t: str) -> str:
    k = min((int(t[:2]) * 60 + int(t[3:]) - 570) // 60, 4)
    return f"{(570 + 60 * k) // 60:02d}:30-{(630 + 60 * k) // 60:02d}:30"


def trades(sigs: list[dict], sess: dict, reg: dict, target: float | str) -> list[dict]:
    out = []
    for s in sigs:
        d, sign = s["time"][:10], 1 if s["side"] == "Long" else -1
        price = s["entry"] + sign * (target if target != "liquidity" else 2.0) * s["risk"]
        if target == "liquidity" and s.get("opp") is not None:
            price = s["opp"]
        r = reg[d]
        out.append({"strategy": s["strategy"], "date": d, "side": s["side"], "time": s["time"][11:], "entry": s["entry"],
                    "stop": s["stop"], "target": round(price, 2), "risk": s["risk"], "event_id": s.get("event_id"),
                    **simulate(sess[d][0], s, price), "tod": tod(s["time"][11:]), **r,
                    **s["features"], "hour": int(s["time"][11:13])})  # fmt: skip
    return out


# Statistics


def stats(ts: list[dict], days: list[str]) -> dict:
    rs, by_day = [t["r"] for t in ts], defaultdict(float)
    for t in ts:
        by_day[t["date"]] += t["r"]
    eq, curve = 0.0, []
    for d in days:
        eq += by_day.get(d, 0.0)
        curve.append({"equity": eq})
    p = performance(rs, curve, 100, len(by_day))  # capital 100 R: drawdown % reads as R
    ci, n = p["expectancy_ci95"], len(rs)
    return {"Trades": n, "Per year": round(n / max(len(days), 1) * 250, 1),
            "Win %": round(sum(r > 0 for r in rs) / n * 100, 1) if n else None,
            "Avg R": round(sum(rs) / n, 3) if n else None, "Avg R 95% CI": f"{ci[0]} to {ci[1]}" if ci else None,
            "_ci_low": ci[0] if ci else None, "PF": p["profit_factor"], "Total R": round(sum(rs), 1),
            "Net pts": round(sum(t["points"] - COST for t in ts)), "Max DD R": p["max_drawdown_pct"],
            "Sharpe": p["sharpe"], "Sortino": p["sortino"],
            "MAE R": round(statistics.fmean(t["mae_r"] for t in ts), 2) if n else None,
            "MFE R": round(statistics.fmean(t["mfe_r"] for t in ts), 2) if n else None,
            "Hold min": round(statistics.fmean(t["hold_min"] for t in ts)) if n else None}  # fmt: skip


def split(ts: list[dict], days: list[str]) -> dict[str, tuple[list[dict], list[str]]]:
    return {"All": (ts, days), "In-sample (to 2020)": ([t for t in ts if t["date"] < SPLIT], [d for d in days if d < SPLIT]),
            "Out-of-sample (2021+)": ([t for t in ts if t["date"] >= SPLIT], [d for d in days if d >= SPLIT])}  # fmt: skip


def walk_forward(by_target: dict, days: list[str]) -> tuple[list[dict], list[tuple]]:
    """Each year from 2018 uses the target with the most R over the 3 years before it."""
    out, picks = [], []
    for y in range(2018, int(days[-1][:4]) + 1):
        prior = {tg: sum(t["r"] for t in ts if y - 3 <= int(t["date"][:4]) < y) for tg, ts in by_target.items()}
        best = max(prior, key=prior.get)
        picks.append((y, best))
        out += [t for t in by_target[best] if int(t["date"][:4]) == y]
    return out, picks


def monte_carlo(rs: list[float], runs: int = 1000, seed: int = 7) -> dict:
    """Trades resampled with replacement: spread of total R and max drawdown in R."""
    rnd, totals, dds = random.Random(seed), [], []
    for _ in range(runs if rs else 0):
        eq = peak = dd = 0.0
        for r in rnd.choices(rs, k=len(rs)):
            eq += r
            peak, dd = max(peak, eq), max(dd, max(peak, eq) - eq)
        totals.append(eq)
        dds.append(dd)
    if not totals:
        return {}
    q = lambda xs, p: round(sorted(xs)[int(p * (len(xs) - 1))], 1)
    return {"Total R p5": q(totals, 0.05), "Total R p50": q(totals, 0.5), "Total R p95": q(totals, 0.95),
            "Max DD R p50": q(dds, 0.5), "Max DD R p95": q(dds, 0.95),
            "P(total R <= 0) %": round(sum(t <= 0 for t in totals) / len(totals) * 100, 1)}  # fmt: skip


def verdict(full: dict, ins: dict, oos: dict, wf: dict) -> str:
    """PASS: positive in- and out-of-sample, OOS PF >= 1.2, bootstrap 95% CI of R per trade above 0, walk-forward
    positive. CONDITIONAL PASS: positive OOS and positive in-sample or walk-forward. FAIL otherwise."""
    if full["Trades"] < 30 or oos["Trades"] < 15:
        return "INSUFFICIENT DATA"
    if (ins["Avg R"] > 0 and oos["Avg R"] > 0 and (oos["PF"] or 0) >= 1.2 and (full["_ci_low"] or 0) > 0
            and wf["Total R"] > 0):  # fmt: skip
        return "PASS"
    if oos["Avg R"] > 0 and (ins["Avg R"] > 0 or wf["Total R"] > 0):
        return "CONDITIONAL PASS"
    return "FAIL"


def breakdown(ts: list[dict], key: str) -> list[dict]:
    groups = defaultdict(list)
    for t in ts:
        groups[t[key]].append(t["r"])
    return [{key: g, "Trades": len(rs), "Win %": round(sum(r > 0 for r in rs) / len(rs) * 100, 1),
             "Avg R": round(statistics.fmean(rs), 3), "Total R": round(sum(rs), 1)}
            for g, rs in sorted(groups.items(), key=lambda x: str(x[0]))]  # fmt: skip


def first_per_event(ts: list[dict], busy: bool = False) -> list[dict]:
    """The first trade of each liquidity event; with `busy`, also none while an earlier trade is still open."""
    out, seen, until = [], set(), ""
    for t in sorted(ts, key=lambda t: (t["date"], t["time"], t["strategy"])):
        if t["event_id"] in seen:
            continue
        seen.add(t["event_id"])
        if busy and f"{t['date']} {t['time']}" < until:
            continue
        until = f"{t['date']} {t['exit_time']}"
        out.append(t)
    return out


def feature_study(pool: list[dict]) -> tuple[list[dict], list[str], list[dict]]:
    """Per feature: R by in-sample tercile (or value when <= 3 values), in- and out-of-sample. Features whose best and
    worst in-sample groups differ by 0.1R+ with 30+ trades each make the score (chosen in-sample only); the score is
    then judged out-of-sample."""
    ins, oos = [t for t in pool if t["date"] < SPLIT], [t for t in pool if t["date"] >= SPLIT]
    rows, chosen, groupers = [], [], {}
    for f in FEATURES:
        vals = sorted(t[f] for t in ins if t.get(f) is not None)
        if len(vals) < 90:
            continue
        if len(set(vals)) <= 3:
            grp = lambda x, f=f: None if x.get(f) is None else str(x[f])
        else:
            c1, c2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
            grp = lambda x, f=f, c1=c1, c2=c2: (None if x.get(f) is None
                                               else f"low <{c1:g}" if x[f] < c1 else f"mid <{c2:g}" if x[f] < c2 else f"high >={c2:g}")  # fmt: skip
        g_in, g_out = defaultdict(list), defaultdict(list)
        for ts, g in ((ins, g_in), (oos, g_out)):
            for t in ts:
                if (k := grp(t)) is not None:
                    g[k].append(t["r"])
        if len(g_in) < 2:
            continue
        keys = sorted(g_in, key=lambda k: statistics.fmean(g_in[k]))
        worst, best = keys[0], keys[-1]
        spread = statistics.fmean(g_in[best]) - statistics.fmean(g_in[worst])
        holds = bool(g_out[best] and g_out[worst]) and statistics.fmean(g_out[best]) > statistics.fmean(g_out[worst])
        if spread >= 0.1 and len(g_in[best]) >= 30 and len(g_in[worst]) >= 30:
            chosen.append(f)
            groupers[f] = (grp, best)
        for k in sorted(g_in):
            rows.append({"Feature": f, "Group": k, "IS trades": len(g_in[k]), "IS avg R": round(statistics.fmean(g_in[k]), 3),
                         "OOS trades": len(g_out[k]), "OOS avg R": round(statistics.fmean(g_out[k]), 3) if g_out[k] else None,
                         "IS best/worst": "best" if k == best else "worst" if k == worst else "",
                         "Holds OOS": ("yes" if holds else "no") if k == best else ""})  # fmt: skip
    for t in pool:
        t["score"] = sum(grp(t) == best for grp, best in groupers.values())
    scores = []
    for s in sorted({t["score"] for t in pool}):
        row = {"Score": s}
        for name, part in (("IS", ins), ("OOS", oos)):
            rs = [t["r"] for t in part if t["score"] == s]
            row |= {f"{name} trades": len(rs), f"{name} avg R": round(statistics.fmean(rs), 3) if rs else None}
        scores.append(row)
    return rows, chosen, scores


# Charts


def charts(base: dict[str, list[dict]], days: list[str]) -> None:
    from matplotlib.figure import Figure

    (OUT / "charts").mkdir(parents=True, exist_ok=True)
    years = [k for k, d in enumerate(days) if k == 0 or d[:4] != days[k - 1][:4]]

    def save(fig, name):
        fig.savefig(OUT / "charts" / name, bbox_inches="tight", dpi=110)

    curves = {}
    for k, ts in base.items():
        by_day, eq, curve = defaultdict(float), 0.0, []
        for t in ts:
            by_day[t["date"]] += t["r"]
        for d in days:
            eq += by_day.get(d, 0.0)
            curve.append(eq)
        curves[k] = curve
    for name, fn, title in (("equity_curves.png", lambda c: c, "Cumulative R after costs (1R per trade)"),
                            ("drawdowns.png", _dd, "Drawdown in R")):  # fmt: skip
        fig = Figure(figsize=(12, 6))
        ax = fig.add_subplot()
        for k, c in curves.items():
            ax.plot(fn(c), label=NAMES.get(k, k), linewidth=1)
        ax.axvline(days.index(next(d for d in days if d >= SPLIT)), color="grey", linestyle="--", linewidth=0.8)
        ax.set_xticks(years, [days[k][:4] for k in years], fontsize=8)
        ax.set_title(f"{title}; dashed line = out-of-sample start")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        save(fig, name)
    for name, key, title in (("time_of_day.png", "tod", "Avg R by entry time"), ("trend_regime.png", "trend", "Avg R by day type (ex post)"),
                             ("vol_regime.png", "vol", "Avg R by volatility regime (ex ante)")):  # fmt: skip
        groups = sorted({t[key] for ts in base.values() for t in ts})
        fig = Figure(figsize=(12, 5))
        ax, width = fig.add_subplot(), 0.8 / len(base)
        for n, (k, ts) in enumerate(base.items()):
            vals = [statistics.fmean([t["r"] for t in ts if t[key] == g] or [0]) for g in groups]
            ax.bar([x + n * width for x in range(len(groups))], vals, width, label=NAMES.get(k, k))
        ax.set_xticks([x + 0.4 - width / 2 for x in range(len(groups))], groups, fontsize=8)
        ax.axhline(0, color="black", linewidth=0.6)
        ax.set_title(title)
        ax.legend(fontsize=7)
        save(fig, name)


def _dd(curve: list[float]) -> list[float]:
    peak, out = 0.0, []
    for x in curve:
        peak = max(peak, x)
        out.append(x - peak)
    return out


def comparison_chart(rows: list[dict]) -> None:
    from matplotlib.figure import Figure

    fig = Figure(figsize=(12, 5))
    ax = fig.add_subplot()
    xs = range(len(rows))
    for n, col in enumerate(("IS avg R", "OOS avg R", "WF avg R")):
        ax.bar([x + n * 0.27 for x in xs], [r[col] or 0 for r in rows], 0.27, label=col)
    ax.set_xticks([x + 0.27 for x in xs], [r["Strategy"] for r in rows], fontsize=8, rotation=15)
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_title("R per trade after costs: in-sample, out-of-sample, walk-forward")
    ax.legend()
    fig.savefig(OUT / "charts" / "strategy_comparison.png", bbox_inches="tight", dpi=110)


# Reports


TABLES: dict[str, list[dict]] = {}  # md() text -> its rows, so write() puts the same tables in the workbook
SHEETS: dict[str, list] = {}  # report title -> its text lines and tables, in order


def md(rows: list[dict]) -> str:
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    if not rows:
        return "_none_\n"
    keys = list(dict.fromkeys(k for r in rows for k in r))
    fmt = lambda v: "" if v is None else f"{v:,.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    out = "\n".join(["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys),
                     *("| " + " | ".join(fmt(r.get(k)) for k in keys) + " |" for r in rows)]) + "\n"  # fmt: skip
    TABLES[out] = rows
    return out


def write(name: str, title: str, *parts: str) -> None:
    (OUT / name).write_text(f"# {title}\n\n" + "\n".join(parts))
    sheet = SHEETS.setdefault(title, [])
    for part in parts:
        table = max((t for t in TABLES if part.startswith(t)), key=len, default="")
        if table:
            sheet.append(TABLES[table])
        sheet += [line for line in part[len(table) :].splitlines() if line.strip() and not line.startswith("![")]


def excel(path: Path, trades: list[dict], charts: bool = True) -> None:
    """The reports' tables, one sheet per report, then `trades` and (with `charts`) the research charts."""
    from openpyxl import (
        Workbook,  # not a project dependency: run with `uv run --with openpyxl`
    )
    from openpyxl.drawing.image import Image
    from openpyxl.styles import Font, PatternFill

    wb, head = Workbook(), PatternFill("solid", fgColor="1F4E78")
    wb.remove(wb.active)

    def table(ws, rows: list[dict]) -> None:
        keys = list(dict.fromkeys(k for r in rows for k in r if not k.startswith("_")))
        ws.append(keys)
        for c in ws[ws.max_row]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), head
        for r in rows:
            ws.append([v if v is None or isinstance(v, int | float | str) else str(v) for v in (r.get(k) for k in keys)])

    first = "Strategy comparison"
    for title in sorted(SHEETS, key=lambda t: t != first):
        ws = wb.create_sheet(title[:31])
        ws.column_dimensions["A"].width = 34
        for item in SHEETS[title]:
            if isinstance(item, str):
                ws.append([item.lstrip("# ")])
                ws.cell(ws.max_row, 1).font = Font(bold=item.startswith("#"))
            else:
                table(ws, item)
                ws.append([])
    ws = wb.create_sheet("Trades")
    table(ws, trades)
    ws.freeze_panes, ws.auto_filter.ref = "A2", ws.dimensions
    if charts:
        ws = wb.create_sheet("Charts")
        for n, png in enumerate(sorted((OUT / "charts").glob("*.png"))):
            ws.add_image(Image(png), f"A{1 + 34 * n}")
    wb.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/research_intraday_liquidity.py")
    parser.add_argument("--history", type=Path, default=Path("data/intraday/NIFTY_1MIN_2015-2025.csv"))
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    sess = sessions(args.history)
    reg = regimes(sess)
    sigs, days, desc = signals(sess, get_settings())
    group_events([s for k in NAMES for s in sigs[k, "base"]])
    span = f"{days[0]} to {days[-1]}"
    print(f"{len(days)} sessions {span}; base signals:", {k: len(sigs[k, "base"]) for k in NAMES})

    by_target = {k: {tg: trades(sigs[k, "base"], sess, reg, tg) for tg in TARGETS} for k in NAMES}
    base = {k: by_target[k][BASE_TARGET[k]] for k in NAMES}
    variants = {k: {v: trades(sigs[k, v], sess, reg, BASE_TARGET[k]) for v in VARIANTS if (k, v) in sigs} for k in NEW}
    every = [t for k in NAMES for t in base[k]]
    portfolios = {"Combined: all, first per event": first_per_event(every, busy=True)}

    summary, verdicts, wfs = [], {}, {}
    for k, name in NAMES.items():
        parts = {p: stats(*v) for p, v in split(base[k], days).items()}
        wf_trades, picks = walk_forward(by_target[k], days)
        wf = stats(wf_trades, [d for d in days if d >= "2018"])
        wfs[k] = (wf, picks)
        verdicts[k] = verdict(parts["All"], parts["In-sample (to 2020)"], parts["Out-of-sample (2021+)"], wf)
        summary.append({"Strategy": name, "Trades": parts["All"]["Trades"], "Per year": parts["All"]["Per year"],
                        "Win %": parts["All"]["Win %"], "PF": parts["All"]["PF"], "Avg R": parts["All"]["Avg R"],
                        "Total R": parts["All"]["Total R"], "Max DD R": parts["All"]["Max DD R"],
                        "Sharpe": parts["All"]["Sharpe"], "Sortino": parts["All"]["Sortino"],
                        "IS avg R": parts["In-sample (to 2020)"]["Avg R"], "OOS avg R": parts["Out-of-sample (2021+)"]["Avg R"],
                        "WF avg R": wf["Avg R"], "Verdict": verdicts[k], "_key": k})  # fmt: skip
    keep = [k for k in NAMES if verdicts[k] in ("PASS", "CONDITIONAL PASS")]
    if keep:
        portfolios[f"Combined: {', '.join(keep)}"] = first_per_event([t for t in every if t["strategy"] in keep], busy=True)
    for name, ts in portfolios.items():
        parts = {p: stats(*v) for p, v in split(ts, days).items()}
        summary.append({"Strategy": name, **{c: parts["All"][c] for c in ("Trades", "Per year", "Win %", "PF", "Avg R",
                        "Total R", "Max DD R", "Sharpe", "Sortino")}, "IS avg R": parts["In-sample (to 2020)"]["Avg R"],
                        "OOS avg R": parts["Out-of-sample (2021+)"]["Avg R"], "WF avg R": None, "Verdict": "",
                        "_key": name})  # fmt: skip
    charts({**base, "Combined (all)": portfolios["Combined: all, first per event"]}, days)
    comparison_chart(summary[: len(NAMES)])

    def section(k: str) -> str:
        parts = split(base[k], days)
        long_short = [{"Side": s, **stats([t for t in base[k] if t["side"] == s], days)} for s in ("Long", "Short")]
        tg = [{"Target": f"{x}R" if x != "liquidity" else "opposing liquidity (2R if none)",
               **stats(by_target[k][x], days)} for x in TARGETS]  # fmt: skip
        var = [{"Variant": v, **stats(ts, days)} for v, ts in variants.get(k, {}).items()]
        wf, picks = wfs[k]
        return "\n".join([
            f"## {NAMES[k]}: {verdicts[k]}\n",
            f"Baseline: {BASE_TARGET[k]:g}R target, minimum risk {MIN_RISK} pts, {COST:g} pts cost a trade, {span}.\n",
            md([{"Sample": p, **stats(*v)} for p, v in parts.items()] + [{"Sample": "Walk-forward 2018+", **wf}]),
            f"Walk-forward targets by year: {', '.join(f'{y}: {p}' for y, p in picks)}\n",
            "### Long vs short\n", md(long_short),
            "### Targets (one at a time, all else baseline)\n", md(tg),
            *(["### Confirmation and minimum risk (one at a time, all else baseline)\n", md(var)] if var else []),
            "### Monte Carlo (1,000 resamples of the baseline trades)\n", md([monte_carlo([t["r"] for t in base[k]])]),
        ])  # fmt: skip

    charts_md = ("![Equity curves](charts/equity_curves.png)\n\n![Drawdowns](charts/drawdowns.png)\n\n"
                 "![Strategy comparison](charts/strategy_comparison.png)\n")  # fmt: skip
    data_md = (f"Data: Kaggle NIFTY 1-minute bars rolled to 5m, {span} ({len(days)} sessions; the first session and NIFTY "
               f"expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). "
               f"In-sample to 2020, out-of-sample 2021 on. Costs: {COST:g} index pts a round trip. Results are index "
               "points and R; option P&L is not reported because no historical option prices are available.\n")  # fmt: skip

    # 1
    level_rows = [{"Level type": k, "Levels per session": round(n / len(days), 1)} for k, n in desc["levels"].items()]
    swing = base["swing"]
    write("LIQUIDITY_DETECTION_ANALYSIS.md", "Liquidity detection analysis", data_md,
          "## Definitions (fixed before testing, none optimised)\n",
          f"- Swing: 2-left/2-right swing highs/lows on 5m and 15m. A swing is usable only from the first 5m bar after its "
          f"second right-hand bar has closed. External (major) = the session's extreme when confirmed; internal (minor) otherwise.\n"
          f"- Opening range: 09:15-09:30 and 09:15-09:45 high/low, usable from 09:30 / 09:45.\n"
          f"- Equal highs/lows: 2+ confirmed 5m swings within {EQ_TOL} x ATR(14, 5m), {EQ_GAP}+ bars apart, with a pullback of "
          f"{EQ_PULL} x ATR between them and no trade through the level in between. Usable once the last swing is confirmed.\n"
          f"- Consolidation: {CONS_BARS} bars spanning at most {CONS_ATR} x ATR with neighbouring bars overlapping {CONS_OVERLAP:.0%}+ on "
          "average; usable from the next bar; a window inside the current box does not create a new one.\n"
          "- Previous hour: high/low of the last completed 60m candle (09:15-10:15, ...), usable only during the following hour.\n"
          f"- Sweep: a 5m bar trades {TICK:g} pt past an intact level (the previous 5m close was on the near side). A level is used once.\n"
          f"- Entry: close back past every swept level within {RECLAIM_BARS} bars, 5m structure shift (close past the last "
          f"confirmed 5m swing) within {MSS_BARS} bars of the sweep, close on the trade's side of VWAP, entries 09:30-14:30 on bar close.\n"
          "- Stop: sweep extreme - max(2 pts, 0.1 ATR). Exits as the existing strategy: stop to entry after +1R, 15:10 exit.\n",
          "## No look-ahead\n",
          "Every level carries the index of the first bar allowed to use it; signals use completed 5m bars only; 15m "
          "structure uses completed 15m bars; the previous hour is used only after it closes. Unit tests in "
          "backend/tests/test_intraday_liquidity.py check these timings.\n",
          "## Levels found\n", md(level_rows),
          "## Internal vs external, higher vs lower timeframe (swing strategy, baseline trades)\n",
          md(breakdown(swing, "external")) + "\n(external 1 = the session's extreme when the swing was confirmed)\n",
          md(breakdown(swing, "tf")) + "\n(tf 5 = 5m swing, 15 = 15m swing: lower vs higher timeframe)\n")  # fmt: skip
    # 2-6
    write("INTRADAY_SWING_LIQUIDITY.md", "Intraday swing liquidity", data_md, section("swing"),
          "### Internal vs external\n", md(breakdown(swing, "external")), "### 5m vs 15m swing\n", md(breakdown(swing, "tf")))
    or_rows = [{"Range": k, "Sessions": desc["or"][k, "days"], "Broken %": round(desc["or"][k, "broken"] / desc["or"][k, "days"] * 100, 1),
                "False break % of breaks": round(desc["or"][k, "false"] / max(desc["or"][k, "broken"], 1) * 100, 1),
                "Continued 1 range % of breaks": round(desc["or"][k, "continued"] / max(desc["or"][k, "broken"], 1) * 100, 1)}
               for k in ("or15", "or30") if desc["or"][k, "days"]]  # fmt: skip
    write("OPENING_RANGE_LIQUIDITY.md", "Opening range liquidity", data_md,
          "## Opening range behaviour (all sessions)\n", md(or_rows),
          "False break = a close back inside the range within 30 min of the first break. Continued = price reached one "
          "more range width past the break the same day.\n", section("or15"), section("or30"))  # fmt: skip
    eq = base["equal"]
    write("EQUAL_HIGH_LOW_RESEARCH.md", "Equal highs and lows", data_md, section("equal"),
          "### By number of touches\n", md(breakdown(eq, "touches")),
          "### Compared with any swing\n", md([{"Strategy": NAMES[k], **stats(base[k], days)} for k in ("swing", "equal")]))
    r = desc["range"]
    write("CONSOLIDATION_LIQUIDITY.md", "Consolidation liquidity", data_md,
          "## Box behaviour\n", md([{"Boxes": r["boxes"], "Broken (close outside)": r["broken"],
                                     "Continued one box height %": round(r["continued"] / max(r["broken"], 1) * 100, 1),
                                     "Reversed to a close past the far side %": round(r["reversed"] / max(r["broken"], 1) * 100, 1)}]),
          section("range"))  # fmt: skip
    hour_events = defaultdict(set)
    for t in every:
        hour_events[t["event_id"]].add(t["strategy"])
    unique = lambda k: [t for t in base[k] if hour_events[t["event_id"]] == {k}]
    shared = lambda k: [t for t in base[k] if len(hour_events[t["event_id"]]) > 1]
    write("PREVIOUS_HOUR_LIQUIDITY.md", "Previous hour liquidity", data_md, section("hour"),
          "### Value beyond PDH/PDL and swing signals\n",
          md([{"Hour signals": "only this strategy in the event", **stats(unique("hour"), days)},
              {"Hour signals": "event shared with another strategy", **stats(shared("hour"), days)}]))
    # 7
    pool = first_per_event([t for k in NEW for t in base[k]])
    rows, chosen, scores = feature_study(pool)
    write("LIQUIDITY_QUALITY_SCORING.md", "Liquidity quality scoring", data_md,
          f"Pool: the first baseline trade of each liquidity event across the intraday strategies ({len(pool)} trades), so "
          "one price move counts once. Each feature is split into in-sample terciles (or its values). No weights are assumed.\n",
          "## Per-feature R\n", md(rows),
          f"## Score\nFeatures chosen in-sample (best vs worst group 0.1R+ apart, 30+ trades each): {', '.join(chosen) or 'none'}. "
          "Score = how many of them a trade has in its best in-sample group. The out-of-sample columns are the test.\n",
          md(scores))  # fmt: skip
    # 8, 9
    best = lambda col, pick=max, rows=summary[: len(NAMES)]: pick((r for r in rows if r["Trades"] >= 30 and r[col] is not None),
                                                                   key=lambda r: r[col], default={"Strategy": "none"})["Strategy"]  # fmt: skip
    def side_avg(k: str, side: str) -> float:
        rs = [t["r"] for t in base[k] if t["side"] == side]
        return statistics.fmean(rs) if len(rs) >= 30 else -9

    side_best = {s: max(NAMES, key=lambda k, s=s: side_avg(k, s)) for s in ("Long", "Short")}
    overlap, corr = [], []
    ev = {k: {t["event_id"] for t in base[k]} for k in NAMES}
    by_date = {k: defaultdict(float) for k in NAMES}
    for k in NAMES:
        for t in base[k]:
            by_date[k][t["date"]] += t["r"]
    daily = {k: [by_date[k].get(d, 0.0) for d in days] for k in NAMES}
    for a in NAMES:
        overlap.append({"Strategy": a, **{b: round(len(ev[a] & ev[b]) / max(len(ev[a]), 1) * 100) for b in NAMES}})
        corr.append({"Strategy": a, **{b: round(statistics.correlation(daily[a], daily[b]), 2) for b in NAMES}})
    redundant = max(NAMES, key=lambda a: max(len(ev[a] & ev[b]) / max(len(ev[a]), 1) for b in NAMES if b != a))
    regime_rows = []
    for key in ("trend", "vol", "open", "orb"):
        for k, name in NAMES.items():
            for row in breakdown(base[k], key):
                regime_rows.append({"Regime": row[key], "Strategy": name, **{c: row[c] for c in ("Trades", "Win %", "Avg R", "Total R")}})

    def best_in(names: tuple[str, ...]) -> str:
        rows = [r for r in regime_rows if r["Regime"] in names]
        agg = defaultdict(list)
        for r in rows:
            agg[r["Strategy"]].append((r["Trades"], r["Avg R"] * r["Trades"]))
        cand = {s: sum(x[1] for x in v) / sum(x[0] for x in v) for s, v in agg.items() if sum(x[0] for x in v) >= 30}
        return f"{max(cand, key=cand.get)} ({max(cand.values()):+.3f}R a trade)" if cand else "none with 30+ trades"

    s_ = {r["_key"]: r for r in summary}
    fails = [NAMES[k] for k in NAMES if verdicts[k] == "FAIL"]
    edge = [f"{NAMES[k]} ({verdicts[k]})" for k in NEW if verdicts[k] in ("PASS", "CONDITIONAL PASS")]
    max_corr = max((abs(c[b]), c["Strategy"], b) for c in corr for b in NAMES if b != c["Strategy"])
    comb = list(portfolios)[-1]
    solo = max((r for r in summary[: len(NAMES)] if r["Verdict"] in ("PASS", "CONDITIONAL PASS")),
               key=lambda r: r["Sharpe"] or 0, default=None)  # fmt: skip
    confluence = [k for k in NAMES if (stats(shared(k), days)["Avg R"] or 0) > (stats(unique(k), days)["Avg R"] or 0)]
    answers = [
        f"1. Measurable edge: {', '.join(edge) or 'no intraday setup passed'}.",
        (f"2. Swing vs PDH/PDL: swing {s_['swing']['OOS avg R']}R vs PDH/PDL {s_['pdh_pdl']['OOS avg R']}R a trade out-of-sample; "
        f"Sharpe {s_['swing']['Sharpe']} vs {s_['pdh_pdl']['Sharpe']} over the whole period."),
        (f"3. Opening range: 15m {verdicts['or15']}, 30m {verdicts['or30']}; 15m only-this-strategy events {stats(unique('or15'), days)['Avg R']}R, "
        f"30m {stats(unique('or30'), days)['Avg R']}R a trade."),
        f"4. Equal highs/lows: {s_['equal']['Avg R']}R a trade vs {s_['swing']['Avg R']}R for any swing ({verdicts['equal']}).",
        f"5. Previous hour: {verdicts['hour']}; its only-this-strategy events {stats(unique('hour'), days)['Avg R']}R a trade.",
        f"6. Trending days (ex post): {best_in(('trending bull', 'trending bear'))}.",
        f"7. Sideways days (ex post): {best_in(('sideways',))}.",
        (f"8. Independence: highest daily R correlation {max_corr[0]} ({max_corr[1]} / {max_corr[2]}); most redundant: {NAMES[redundant]} "
        "(see the event overlap table)."),
        f"9. Portfolio: '{comb}': {s_[comb]['Trades']} trades, {s_[comb]['Avg R']}R a trade, Sharpe {s_[comb]['Sharpe']}, "
        f"max drawdown {s_[comb]['Max DD R']}R"
        + (f"; best single passing setup {solo['Strategy']}: {solo['Avg R']}R a trade, Sharpe {solo['Sharpe']}, max drawdown "
           f"{solo['Max DD R']}R" if solo else "")
        + f". Signals sharing their event with another strategy beat solo ones for {len(confluence)} of {len(NAMES)} strategies "
        "(confluence of liquidity types matters more than any one type).",
        f"10. Reject: {', '.join(fails) or 'none'}.",
    ]  # fmt: skip
    picks = md([{"Question": q, "Answer": a} for q, a in (
        ("Best individual setup (total R)", best("Total R")), ("Most consistent (out-of-sample R)", best("OOS avg R")),
        ("Best risk-adjusted (Sharpe)", best("Sharpe")), ("Lowest drawdown", best("Max DD R", min)),
        ("Best long", NAMES[side_best["Long"]]), ("Best short", NAMES[side_best["Short"]]),
        ("Most redundant (event overlap)", NAMES[redundant]))])  # fmt: skip
    write("STRATEGY_COMPARISON.md", "Strategy comparison", data_md, md(summary), charts_md,
          "Verdict rules: PASS = positive in- and out-of-sample, OOS profit factor 1.2+, bootstrap 95% interval of R per "
          "trade above zero and positive walk-forward. CONDITIONAL PASS = positive out-of-sample and positive in-sample or "
          "walk-forward. FAIL otherwise. INSUFFICIENT DATA = under 30 trades or under 15 out-of-sample.\n",
          "## Picks\n", picks, "## Final verdict\n", "\n".join(answers) + "\n")  # fmt: skip
    write("PORTFOLIO_CORRELATION_ANALYSIS.md", "Portfolio and correlation", data_md,
          "Liquidity events: signals on the same day and side whose sweep-to-entry windows overlap share one event id "
          "(app.intraday_liquidity.group_events). Portfolios take the first signal of each event and never hold two trades "
          "at once, so one price move is one trade.\n",
          "## Event overlap: % of the row strategy's events that also have a signal from the column strategy\n", md(overlap),
          "## Daily R correlation\n", md(corr),
          "## Unique vs shared events\n",
          md([{"Strategy": NAMES[k], "Unique trades": len(unique(k)), "Unique avg R": stats(unique(k), days)["Avg R"],
               "Shared trades": len(shared(k)), "Shared avg R": stats(shared(k), days)["Avg R"]} for k in NAMES]),
          "## Portfolios\n", md([{"Portfolio": n, **stats(ts, days)} for n, ts in portfolios.items()]))  # fmt: skip
    # 10
    write("BACKTEST_RESULTS.md", "Backtest results", data_md, md(summary), charts_md,
          *(section(k) for k in NAMES),
          "## Time of day\n![Time of day](charts/time_of_day.png)\n",
          md([{"Strategy": NAMES[k], **row} for k in NAMES for row in breakdown(base[k], "tod")]),
          "## Regimes\nTrend and failed-opening-break labels use the whole day (ex post) and describe where a setup worked; "
          "they are not tradeable filters. Volatility and opening momentum are known by the entry.\n",
          "![Trend](charts/trend_regime.png)\n\n![Volatility](charts/vol_regime.png)\n", md(regime_rows))  # fmt: skip
    # 11
    risk_rows = []
    for k, name in NAMES.items():
        risks = sorted(t["risk"] for t in base[k])
        streak = worst = 0
        for t in sorted(base[k], key=lambda t: (t["date"], t["time"])):
            streak = streak + 1 if t["r"] < 0 else 0
            worst = max(worst, streak)
        mc = monte_carlo([t["r"] for t in base[k]])
        risk_rows.append({"Strategy": name, "Stop pts p10": risks[len(risks) // 10] if risks else None,
                          "Stop pts median": risks[len(risks) // 2] if risks else None,
                          "Stop pts p90": risks[9 * len(risks) // 10] if risks else None,
                          "Max DD R": s_[k]["Max DD R"], "MC max DD R p95": mc.get("Max DD R p95"),
                          "Longest losing streak": worst, "MAE R": stats(base[k], days)["MAE R"],
                          "MFE R": stats(base[k], days)["MFE R"]})  # fmt: skip
    write("RISK_ANALYSIS.md", "Risk analysis", data_md, md(risk_rows),
          "MAE/MFE: average worst/best excursion of a trade while open, in R. MC: 1,000 resamples of the trades.\n",
          "## Minimum-risk filter on intraday setups\n",
          md([{"Strategy": NAMES[k], "Min risk": v, **{c: stats(ts, days)[c] for c in ("Trades", "Avg R", "Total R", "Max DD R")}}
              for k in NEW for v, ts in variants[k].items() if v.startswith("min risk") or v == "base"]),
          "(base = 30 pts)\n", "![Drawdowns](charts/drawdowns.png)\n",
          "## Not modelled\nSlippage beyond the flat cost, option IV changes and theta, gaps inside a 5m bar, real VWAP.\n")  # fmt: skip
    book = OUT / f"NIFTY_Intraday_Liquidity_Research_{date.today()}.xlsx"
    excel(book, sorted(every, key=lambda t: (t["date"], t["time"], t["strategy"])))
    print("\n".join(answers))
    print("wrote", OUT, "and", book.name)


if __name__ == "__main__":
    main()
