"""Search for a winning version of the liquidity setups without fooling ourselves.

Every combination of strategy (the 7 researched + "confluence": the first trade of each event where 2+ strategies
fired), target and filters known at entry is scored on 2015-2020 only (t-stat of R per trade, 60+ trades). The rule
ranked first there is the pick, fixed before 2021-2025 is looked at; its 2021-2025 result is the verdict. Also
reports whether in-sample rank predicts out-of-sample at all across every rule tried, and what re-running the search
every year on the 3 years before (then trading the pick for that year) would have earned from 2018.

    uv run --with openpyxl python scripts/find_winning_setup.py
"""

from __future__ import annotations

import itertools
import math
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.research_intraday_liquidity as rs
from app.config import get_settings
from app.intraday_liquidity import group_events

MIN_IS, TOP = 60, 20
FILTERS = {
    "side": {"both": None, "long": lambda t: t["side"] == "Long", "short": lambda t: t["side"] == "Short"},
    "open": {"any": None, "with the day's open": lambda t: t.get("with_open") == 1},
    "VWAP": {"any": None, "1+ ATR past VWAP": lambda t: (t.get("vwap_atr") or 0) >= 1},
    "volatility": {"any": None, **{v: (lambda t, v=v: t["vol"] == v) for v in ("low vol", "normal vol", "high vol")}},
    "opening": {"any": None, **{o: (lambda t, o=o: t["open"] == o and t["time"] >= "09:45")  # known from 09:45
                                for o in ("strong open", "normal open")}},
    "time": {"any": None, "before 12:00": lambda t: t["time"] < "12:00", "12:00 on": lambda t: t["time"] >= "12:00"},
}  # fmt: skip


def tstat(r: list[float]) -> float:
    if len(r) < 2 or not (sd := statistics.stdev(r)):
        return -math.inf
    return statistics.fmean(r) / (sd / math.sqrt(len(r)))


def main() -> None:
    sess = rs.sessions(Path("data/intraday/NIFTY_1MIN_2015-2025.csv"))
    reg = rs.regimes(sess)
    sigs, days, _ = rs.signals(sess, get_settings())
    group_events([s for k in rs.NAMES for s in sigs[k, "base"]])
    pools = {}
    for tg in rs.TARGETS:
        every = []
        for k in rs.NAMES:
            pools[k, tg] = rs.trades(sigs[k, "base"], sess, reg, tg)
            every += pools[k, tg]
        n_strats = Counter()
        for e, k in {(t["event_id"], t["strategy"]) for t in every}:
            n_strats[e] += 1
        pools["confluence", tg] = rs.first_per_event([t for t in every if n_strats[t["event_id"]] >= 2])

    results = []
    for (k, tg), pool in pools.items():
        masks = {dim: {opt: None if f is None else {i for i, t in enumerate(pool) if f(t)} for opt, f in opts.items()}
                 for dim, opts in FILTERS.items()}  # fmt: skip
        for combo in itertools.product(*(opts.items() for opts in masks.values())):
            idx = set(range(len(pool)))
            for _, m in combo:
                if m is not None:
                    idx &= m
            ins = [pool[i]["r"] for i in idx if pool[i]["date"] < rs.SPLIT]
            if len(ins) < MIN_IS:
                continue
            oos = [pool[i]["r"] for i in idx if pool[i]["date"] >= rs.SPLIT]
            rule = ", ".join(opt for (dim, (opt, _)) in zip(FILTERS, combo) if opt not in ("both", "any")) or "no filter"
            results.append({"Strategy": rs.NAMES.get(k, "Confluence (2+ strategies, one event)"),
                            "Target": f"{tg}R" if tg != "liquidity" else "opposing liquidity", "Filters": rule,
                            "IS trades": len(ins), "IS avg R": round(statistics.fmean(ins), 3), "IS t": round(tstat(ins), 2),
                            "OOS trades": len(oos), "OOS avg R": round(statistics.fmean(oos), 3) if oos else None,
                            "OOS total R": round(sum(oos), 1), "_trades": [pool[i] for i in sorted(idx)]})  # fmt: skip
            years = results[-1]["_years"] = {}
            for i in idx:
                years.setdefault(int(pool[i]["date"][:4]), []).append(pool[i]["r"])
    rolling, roll_picks = [], []
    for y in range(2018, int(days[-1][:4]) + 1):
        def past(r, y=y):
            prior = [x for z in range(y - 3, y) for x in r["_years"].get(z, [])]
            return tstat(prior) if len(prior) >= MIN_IS / 2 else -math.inf

        best = max(results, key=past)
        roll_picks.append({"Year": y, "Rule picked on the 3 years before": f"{best['Strategy']}, {best['Target']}, "
                           f"{best['Filters']}", "Trades": len(best["_years"].get(y, [])),
                           "R that year": round(sum(best["_years"].get(y, [])), 1)})  # fmt: skip
        rolling += [t for t in best["_trades"] if int(t["date"][:4]) == y]
    roll = rs.stats(rolling, [d for d in days if d >= "2018"])
    results.sort(key=lambda r: -r["IS t"])
    top, pick = results[:TOP], results[0]
    scored = [r for r in results if r["OOS avg R"] is not None]
    corr = statistics.correlation([r["IS avg R"] for r in scored], [r["OOS avg R"] for r in scored])
    base_rate = sum(r["OOS avg R"] > 0 for r in scored) / len(scored) * 100
    top_rate = sum((r["OOS avg R"] or 0) > 0 for r in top) / len(top) * 100

    ts = pick["_trades"]
    parts = {p: rs.stats(*v) for p, v in rs.split(ts, days).items()}
    oos = parts["Out-of-sample (2021+)"]
    years = [{"Year": y, "Trades": len(r := [t["r"] for t in ts if t["date"][:4] == y]), "Total R": round(sum(r), 1)}
             for y in sorted({t["date"][:4] for t in ts})]  # fmt: skip
    low = float(oos["Avg R 95% CI"].split()[0]) if oos["Avg R 95% CI"] else -1
    verdict = ("WINNER: positive out-of-sample with a 95% interval above zero" if oos["Avg R"] > 0 and low > 0
               else "PROMISING, NOT PROVEN: positive out-of-sample but the 95% interval includes zero" if oos["Avg R"] > 0
               else "NO WINNER: the best in-sample rule lost out-of-sample")  # fmt: skip
    rs.write("WINNING_SETUP_SEARCH.md", "Winning setup search",
             f"{len(results):,} rules with {MIN_IS}+ in-sample trades: {len(pools)} strategy/target pools x filters "
             f"({', '.join(FILTERS)}). Ranked by the t-stat of R per trade on 2015-2020 only; 2021-2025 untouched until "
             f"the pick was fixed. Same data, exits and {rs.COST:g}-pt cost as the research.\n",
             f"## Verdict: {verdict}\n",
             f"Pick (ranked 1st in-sample): {pick['Strategy']}, target {pick['Target']}, filters: {pick['Filters']}.\n",
             rs.md([{"Sample": p, **v} for p, v in parts.items()]),
             "### The pick by year\n", rs.md(years),
             "### Monte Carlo (1,000 resamples)\n", rs.md([rs.monte_carlo([t["r"] for t in ts])]),
             "## Does in-sample rank predict out-of-sample?\n",
             rs.md([{"Correlation of IS and OOS avg R across all rules": round(corr, 3),
                     "Top 20 in-sample positive OOS %": round(top_rate, 1),
                     "All rules positive OOS %": round(base_rate, 1)}]),
             "If the top 20 are positive out-of-sample no more often than all rules, ranking found luck, not an edge.\n",
             "## Searching every year: what 'find the winner, then trade it' would have earned\n",
             "Each year from 2018 trades the rule with the best t-stat over the 3 years before it.\n",
             rs.md([{"Sample": "Re-picked yearly, 2018+", **roll}]), rs.md(roll_picks),
             f"## Top {TOP} in-sample rules and how they did after\n",
             rs.md(top))  # fmt: skip
    book = rs.OUT / f"NIFTY_Winning_Setup_Search_{date.today()}.xlsx"
    rs.SHEETS = {k: v for k, v in rs.SHEETS.items() if k == "Winning setup search"}
    rs.excel(book, sorted(ts, key=lambda t: (t["date"], t["time"])), charts=False)
    print(verdict)
    print(f"pick: {pick['Strategy']} | {pick['Target']} | {pick['Filters']}")
    for p, v in parts.items():
        print(f"  {p}: {v['Trades']} trades, {v['Avg R']}R/trade, CI {v['Avg R 95% CI']}, PF {v['PF']}")
    print(f"{len(results):,} rules; IS/OOS correlation {corr:.3f}; top-20 positive OOS {top_rate:.0f}% vs all {base_rate:.0f}%")
    print(f"re-picked yearly 2018+: {roll['Trades']} trades, {roll['Avg R']}R/trade, total {roll['Total R']}R, "
          f"CI {roll['Avg R 95% CI']}, max DD {roll['Max DD R']}R")
    for p in roll_picks:
        print(f"  {p['Year']}: {p['Rule picked on the 3 years before']} -> {p['R that year']}R ({p['Trades']} trades)")
    for r in top[:10]:
        print(f"  {r['Strategy'][:28]:28} {r['Target']:>8} {r['Filters'][:60]:60} IS {r['IS avg R']:+.3f} (t {r['IS t']}) "
              f"OOS {r['OOS avg R']:+.3f} n={r['OOS trades']}")
    print("wrote", book)


if __name__ == "__main__":
    main()
