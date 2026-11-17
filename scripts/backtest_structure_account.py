"""Offline matched account comparison: fixed-OTM current vs expanded entries.

Synthetic option quotes, prior-available VIX, assumed fees/news. No broker orders
or delivery. Uses the deployed account as control and a frozen, previously
validated research account with the identical risk and profit-trail functions.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.update(
    PYTHON_DOTENV_DISABLED="true",
    TELEGRAM_DELIVERY_ENABLED="false",
    LIVE_TRADING_ENABLED="false",
    SANDBOX_ALLOW_LIVE="false",
)


def offline(*args, **kwargs):
    raise RuntimeError("Account replay has no network access")


socket.socket.connect = offline
socket.getaddrinfo = offline

from app.config import Settings
from app.market.expiry import resolve_expiry
from app.market.hours import holidays
from app.trading import (
    forward_candidate_account as ca,
)
from app.trading import (
    live_replay as lr,
)
from app.trading import (
    paper_exec as px,
)
from app.trading import (
    paper_fees as pf,
)
from app.trading import (
    paper_risk as risk,
)
from app.trading import structure_entry_research as entries
from app.trading.option_quote import IST
from app.trading.paper_exit_policy import FRESH_BID
from app.trading.paper_profit_trail import profile

MODEL = "SYNTHETIC_BLACK76_PRIOR_AVAILABLE_VIX"
FEE = "SIMULATED_STRUCTURE_REPLAY_FEES_NOT_DAILY_ATTESTED"


class Engine:
    broader = True

    def __init__(self, signals):
        self.by_close = defaultdict(list)
        for s in signals:
            self.by_close[s["signal_at"]].append(s)

    def advance(self, rows, state, now):
        stamp = rows[-1]["date"]
        if state["last_bar"] == stamp:
            return state, []
        close = (datetime.fromisoformat(stamp) + timedelta(minutes=1)).isoformat()
        return {**state, "last_bar": stamp}, self.by_close.get(close, [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage",
        type=Path,
        default=ROOT / "logs/resimulation-20261117/structure-entries-v1/results.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "logs/resimulation-20261117/structure-account-v1",
    )
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if list(out.glob("*.db")):
        raise RuntimeError("Refusing to overwrite an existing replay account")
    coverage = json.loads(args.coverage.read_text())
    if (
        coverage["policy_source_sha256"]
        != hashlib.sha256(Path(entries.__file__).read_bytes()).hexdigest()
    ):
        raise RuntimeError("Refresh coverage after entry-policy source changes")
    expanded, suppressed = entries.merge_candidates(
        coverage["signals"] + coverage["zone_signals"]
    )
    by_day = defaultdict(list)
    # Reuse exact frozen spot sources from the coverage receipt, never a newer snapshot.
    for relative, digest in coverage["source_hashes"].items():
        path = ROOT / relative
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        if path.suffixes[-2:] == [".json", ".gz"]:
            import gzip

            snap = json.loads(gzip.decompress(raw))
            for r in snap["raw_1m"]:
                if str(r["date"])[:10] == "2026-11-17":
                    stamp = datetime.fromisoformat(r["date"])
                    stamp = (
                        stamp.replace(tzinfo=IST)
                        if stamp.utcoffset() is None
                        else stamp.astimezone(IST)
                    )
                    if stamp + timedelta(minutes=1) <= datetime.fromisoformat(
                        coverage["capture"]
                    ):
                        by_day["2026-11-17"].append({**r, "date": stamp.isoformat()})
        elif path.name.startswith("nifty-1m-"):
            for a in json.loads(raw)["data"]["candles"]:
                stamp = datetime.fromisoformat(a[0]).astimezone(IST)
                day = stamp.date().isoformat()
                if day != "2026-11-17" and "09:15" <= stamp.strftime("%H:%M") < "15:30":
                    by_day[day].append(
                        {
                            "date": stamp.isoformat(),
                            "open": a[1],
                            "high": a[2],
                            "low": a[3],
                            "close": a[4],
                        }
                    )
    days = [d["day"] for d in coverage["days"]]
    for d in coverage["days"]:
        unique = {r["date"]: r for r in by_day[d["day"]]}
        rows = [unique[k] for k in sorted(unique)][: d["bars"]]
        assert len(rows) == d["bars"]
        by_day[d["day"]] = rows
    settings = lr.replay_settings(
        Settings(_env_file=None, data_dir=str(ROOT / "logs/paper-forward/data")),
        paper=True,
    ).model_copy(
        update={
            "paper_exit_quote_policy": FRESH_BID,
            "paper_candidate_profit_giveback_pct": D(20),
        }
    )
    calendar = holidays(settings)
    vix_path = ROOT / "logs/resimulation-20261116/reliability-v1/vix-daily.json"
    vix = {
        r[0][:10]: float(r[4])
        for r in json.loads(vix_path.read_text())["data"]["candles"]
    }
    # An explicit model tariff; deliberately cannot be mistaken for live daily evidence.
    spec = pf.FeePolicySpec(
        FEE,
        days[0],
        days[-1],
        dict(pf.RATES),
        (),
        "ASSUMED_20_RUPEE_BROKERAGE_AND_RATES",
    )
    pf.POLICIES_BY_ID[FEE] = spec
    original_resolve = pf.resolve_policy

    def model_fee(day, *, policy_id=None, allow_historical_brokerage_assumption=False):
        if policy_id == FEE and days[0] <= day <= days[-1]:
            return spec
        return original_resolve(
            day,
            policy_id=policy_id,
            allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
        )

    pf.resolve_policy = model_fee
    frozen = ROOT / "logs/resimulation-20261117/hybrid-additions-v1/research_account.py"
    module_spec = importlib.util.spec_from_file_location(
        "structure_frozen_account", frozen
    )
    research = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = research
    module_spec.loader.exec_module(research)
    ca.SOURCE = research.SOURCE = MODEL

    # Prefixes were validated in coverage; preserve identical records for all accounts.
    def frame(raw, now, minutes, calendar):
        return raw, None

    ca.session_frame = research.session_frame = frame
    accounts = {
        "current": ca.CandidateAccount(
            out / "current.db", settings, run_id="OFFLINE-STRUCTURE-CONTROL"
        ),
        "control_adapter": research.HybridAccount(
            out / "control_adapter.db",
            settings,
            run_id="OFFLINE-ADAPTER-CHECK",
            signal_engine=Engine(coverage["zone_signals"]),
            legacy_management=False,
            dynamic_selection=False,
            hopping=False,
        ),
        "expanded": research.HybridAccount(
            out / "expanded.db",
            settings,
            run_id="OFFLINE-STRUCTURE-EXPANDED",
            signal_engine=Engine(expanded),
            legacy_management=False,
            dynamic_selection=False,
            hopping=False,
        ),
    }
    # Research queues are never delivered. Suppress message insertion entirely.
    for account in accounts.values():
        account._message = lambda *a, **kw: None
    curve, progress, iv_assumptions = [], [], []
    initial = {name: D(50000) for name in accounts}
    peak = dict(initial)
    drawdown = {name: D(0) for name in accounts}
    try:
        for day in days:
            rows = by_day[day]
            vd = max(k for k in vix if k < day)
            iv = lr.prior_session(vix, day)
            if iv is None:
                raise ValueError("Missing prior available VIX")
            iv_assumptions.append({"day": day, "vix_day": vd, "value": iv})
            expiry = resolve_expiry(
                datetime.fromisoformat(day).date(), holidays=calendar
            ).isoformat()
            for i, bar in enumerate(rows):
                now = datetime.fromisoformat(bar["date"]) + timedelta(
                    minutes=1, seconds=5
                )
                prefix = rows[: i + 1]
                raw_frame = SimpleNamespace(
                    empty=False, iterrows=lambda prefix=prefix: enumerate(prefix)
                )
                spot = bar["close"]
                years = max(
                    (
                        datetime.fromisoformat(expiry + "T15:30:00+05:30") - now
                    ).total_seconds(),
                    0,
                ) / (365 * 86400)
                contracts = []
                evidence = {}
                for n in range(-20, 21):
                    strike = round(spot / 50) * 50 + n * 50
                    row = {"underlying": "NIFTY", "strikePrice": strike, "lotSize": 65}
                    for kind in ("CE", "PE"):
                        premium = lr.black76(spot, strike, years, iv / 100, kind)
                        bid = lr._ticks(premium * 0.995, False)
                        ask = max(bid + lr.TICK, lr._ticks(premium * 1.005, True))
                        symbol = f"MODEL-NIFTY-{expiry}-{strike}{kind}"
                        row[kind.lower()] = {
                            "tradingSymbol": symbol,
                            "growwContractId": symbol,
                            "marketLot": 65,
                            "liveData": {
                                "bid": str(bid),
                                "ask": str(ask),
                                "ltp": premium,
                                "quote_time": now.isoformat(),
                            },
                        }
                        evidence[symbol] = risk.DeltaEvidence(
                            D(
                                str(
                                    round(
                                        lr.b76_delta(
                                            spot, strike, years, iv / 100, kind
                                        ),
                                        6,
                                    )
                                )
                            ),
                            now,
                            symbol,
                            MODEL,
                        )
                    contracts.append(row)
                chain = {"contracts": contracts, "expiry": expiry, "fetched_at": now}
                for name, account in accounts.items():
                    account.tick(
                        now=now,
                        raw_1m=raw_frame,
                        chain=chain,
                        evidence=evidence,
                        news_safe=True,
                        fee_policy=FEE,
                    )
                    with account.connect() as c:
                        closed = [
                            r[0]
                            for r in c.execute(
                                "SELECT id FROM trades WHERE state='CLOSED'"
                            )
                        ]
                        realized = sum((account._pnl(c, tid) for tid in closed), D(0))
                        held = c.execute(
                            "SELECT id FROM trades WHERE state<>'CLOSED'"
                        ).fetchall()
                        marks = [
                            c.execute(
                                "SELECT marked_net FROM profit_trails WHERE trade_id=?",
                                (r[0],),
                            ).fetchone()
                            for r in held
                        ]
                        if any(m is None for m in marks):
                            # Mark a just-opened position against the same executable bid.
                            nets = []
                            for h in held:
                                t = json.loads(
                                    c.execute(
                                        "SELECT body FROM trades WHERE id=?", (h[0],)
                                    ).fetchone()[0]
                                )
                                q = px.contract_quote(
                                    chain,
                                    strike=t["strike"],
                                    option_type=t["option_type"],
                                    expiry=t["expiry"],
                                    underlying="NIFTY",
                                )
                                sell = px.fill(
                                    q,
                                    "SELL",
                                    t["lots"],
                                    now=now,
                                    settings=settings,
                                    ticks=1,
                                    cost_policy_id=FEE,
                                )
                                nets.append(
                                    account._pnl(c, h[0])
                                    + sell.value
                                    - sell.charges_inr
                                )
                        else:
                            nets = [D(m[0]) for m in marks]
                        equity = D(50000) + realized + sum(nets, D(0))
                        peak[name] = max(peak[name], equity)
                        drawdown[name] = max(drawdown[name], peak[name] - equity)
                        curve.append(
                            {
                                "at": now.isoformat(),
                                "lane": name,
                                "equity": str(equity),
                                "cash": str(account._cash(c)),
                                "held": len(held),
                            }
                        )
            progress.append(
                {
                    "day": day,
                    "bars": len(rows),
                    "stats": {k: a.stats() for k, a in accounts.items()},
                }
            )
            (out / "progress.json").write_text(json.dumps(progress, indent=2) + "\n")
            print(
                day,
                {
                    k: (v["entries"], v["closed_net_pnl_inr"], v["held"])
                    for k, v in progress[-1]["stats"].items()
                },
                flush=True,
            )
        results = {}
        for name, account in accounts.items():
            with account.connect() as c:
                trades = []
                for row in c.execute("SELECT * FROM trades ORDER BY rowid"):
                    t = {**dict(row), **json.loads(row["body"])}
                    t.pop("body")
                    t["fills"] = [
                        {**dict(f), "body": json.loads(f["body"])}
                        for f in c.execute(
                            "SELECT * FROM fills WHERE trade_id=? ORDER BY id",
                            (t["id"],),
                        )
                    ]
                    t["net"] = (
                        str(account._pnl(c, t["id"]))
                        if t["state"] == "CLOSED"
                        else None
                    )
                    trades.append(t)
                decisions = [
                    {**dict(r), "signal": json.loads(r["signal"])}
                    for r in c.execute("SELECT * FROM decisions ORDER BY rowid")
                ]
                fees = sum(
                    (D(f["body"]["charges_inr"]) for t in trades for f in t["fills"]),
                    D(0),
                )
            pnls = [D(t["net"]) for t in trades if t["net"] is not None]
            won = sum((p for p in pnls if p > 0), D(0))
            lost = -sum((p for p in pnls if p < 0), D(0))
            results[name] = {
                "stats": account.stats(),
                "max_marked_drawdown": str(drawdown[name]),
                "fees": str(fees),
                "wins": sum(p > 0 for p in pnls),
                "losses": sum(p < 0 for p in pnls),
                "profit_factor": str(won / lost) if lost else None,
                "trades": trades,
                "decisions": decisions,
            }
        # The precomputed closed-bar adapter must exactly reproduce the deployed detector control.
        for key in ("entries", "closed", "held", "closed_net_pnl_inr", "cash_inr"):
            assert (
                results["current"]["stats"][key]
                == results["control_adapter"]["stats"][key]
            ), key
        for a, b in zip(
            results["current"]["trades"],
            results["control_adapter"]["trades"],
            strict=True,
        ):
            assert a["id"] == b["id"] and a["net"] == b["net"]
            assert [f["body"] for f in a["fills"]] == [f["body"] for f in b["fills"]]
        # Check the earlier common-period control before including today's modeled extension.
        prior = json.loads(
            (
                ROOT / "logs/resimulation-20261117/hybrid-additions-v1/results.json"
            ).read_text()
        )["variants"]["current_trail1r"]["stats"]
        common = [
            t
            for t in results["current"]["trades"]
            if t["day"] <= "2026-11-16" and t["net"] is not None
        ]
        assert (
            str(sum((D(t["net"]) for t in common), D(0))) == prior["closed_net_pnl_inr"]
        )
        assert len(common) == prior["closed"]
        report = {
            "coverage_sha256": hashlib.sha256(args.coverage.read_bytes()).hexdigest(),
            "coverage_capture": coverage["capture"],
            "model_source": MODEL,
            "iv_by_day": iv_assumptions,
            "fee_policy": spec.snapshot(),
            "profile": profile(D(20)),
            "rules": entries.RULES,
            "suppressed": suppressed,
            "lanes": results,
            "source_hashes": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (
                    Path(__file__),
                    Path(ca.__file__),
                    frozen,
                    Path(entries.__file__),
                    vix_path,
                )
            },
            "scope": "Modeled options P&L; 1-minute closes, Black76 using prior-available VIX and synthetic 1% spread plus tick rounding. Flat assumed fees/news. No real historical books, IV surface, intraminute profit monitoring or execution liquidity. Fixed two-strike OTM selection in both lanes. One-position, max 3 lots, existing risk budgets and 20% net-profit giveback after 1R. All output journals isolated; no live changes or delivery.",
        }
        (out / "results.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n"
        )
        (out / "equity.json").write_text(json.dumps(curve) + "\n")
        lines = [
            "# Matched account backtest",
            "",
            report["scope"],
            "",
            "| Lane | Closed | Net after fees | Fees | Max marked drawdown | Wins / losses |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name in ("current", "expanded"):
            r = results[name]
            lines.append(
                f"| {name} | {r['stats']['closed']} | {r['stats']['closed_net_pnl_inr']} | {r['fees']} | {r['max_marked_drawdown']} | {r['wins']} / {r['losses']} |"
            )
        lines += ["", "## Comparable period through 16 November", ""]
        for name in ("current", "expanded"):
            ts = [
                t
                for t in results[name]["trades"]
                if t["day"] <= "2026-11-16" and t["net"] is not None
            ]
            lines.append(
                f"- {name}: {len(ts)} closed; modeled net {sum((D(t['net']) for t in ts), D(0))}."
            )
        lines += [
            "",
            "## Issues and limitations",
            "",
            "- November 17 uses the last available VIX close from November 13; November 16 VIX is absent. Treat today as an exploratory extension, separately from the common period.",
            "- Entry rules were motivated by November 17. This is an in-sample diagnostic, not independent validation.",
            "- Model delta/IV can change which options fit the risk budget. Actual quote replay can differ materially.",
            "- One-minute model marks miss one-second peaks; the trail trigger can overshoot its 20% threshold.",
            "- Entry simulation uses ask plus one tick, exit bid minus one tick; stops fill at the next observed model quote, not an invented stop premium.",
            "- One minute OHLC can show a stop touch without its timestamp; no favorable ordering is inferred.",
            "- Equity drawdown uses one-minute liquidation marks and estimated exit fees. No option-margin leverage, partial lots, liquidity hopping or dynamic strike selection.",
            "- The adapter control matched every deployed-control fill and net result. Earlier control through November 16 matched the prior replay exactly.",
            "- Active paper strategy is unchanged.",
            "",
            "## Rejected candidates",
            "",
        ]
        for name in ("current", "expanded"):
            lines.append(f"- {name}: {results[name]['stats']['rejections']}")
        (out / "report.md").write_text("\n".join(lines) + "\n")
        print("COMPLETE", out, flush=True)
    finally:
        for account in accounts.values():
            account.close()


if __name__ == "__main__":
    main()
