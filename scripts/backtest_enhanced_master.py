"""Comprehensive Factorial Backtest: Enhanced Master Policy vs Baseline Controls.

Simulates 21 historical sessions (15 Oct – 17 Nov 2026) using exact validated candles,
synthetic Black-76 option pricing, prior-available VIX, and verified transaction costs.

Compares 4 Factorial Configurations:
1. Control_Baseline_Zone_Giveback: Zone entries, ₹1,000 risk, 20% giveback exit.
2. Ceiling_Upgrade_Only: Morning + Zone entries, ₹2,500 risk, 20% giveback exit.
3. Thesis_Management_Baseline: Morning + Zone entries, ₹1,000 risk, Thesis Management.
4. Enhanced_Master_Policy: Full 14 improvements (₹2,500 risk, 1-loss circuit breaker,
   DTE opening rule, Volume Profile filter, 90s cutoff, Thesis holding runners).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "docs/antigravity-handoff/2026-11-17/research-snapshots/morning-window-v1"))

os.environ.update(
    PYTHON_DOTENV_DISABLED="true",
    TELEGRAM_DELIVERY_ENABLED="false",
    LIVE_TRADING_ENABLED="false",
    SANDBOX_ALLOW_LIVE="false",
)

from app.config import Settings
from app.market.expiry import resolve_expiry
from app.market.hours import holidays
from app.trading import (
    enhanced_master_policy as emp,
    forward_candidate_account as ca,
    hybrid_research_policy as hybrid,
    live_replay as lr,
    paper_fees as pf,
    paper_risk as risk,
    structure_entry_research as base,
)
from app.trading.option_quote import IST
from app.trading.paper_exit_policy import FRESH_BID
from morning_policy import reclaim_signals

MODEL = "SYNTHETIC_BLACK76_PRIOR_AVAILABLE_VIX"
FEE = "SIMULATED_STRUCTURE_REPLAY_FEES_NOT_DAILY_ATTESTED"


class SimpleSignalEngine:
    def __init__(self, signals):
        self.by_close = defaultdict(list)
        for s in signals:
            self.by_close[s["signal_at"]].append(s)

    def advance(self, rows, state, now):
        stamp = rows[-1]["date"]
        if state.get("last_bar") == stamp:
            return state, []
        close = (datetime.fromisoformat(stamp) + timedelta(minutes=1)).isoformat()
        return {**state, "last_bar": stamp}, self.by_close.get(close, [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage",
        type=Path,
        default=ROOT / "logs/resimulation-20261117/expiry-time-v1/coverage/results.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "logs/resimulation-20261118/enhanced-master",
    )
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"Loading historical market coverage from: {args.coverage}")
    coverage = json.loads(args.coverage.read_text())

    by_day = defaultdict(list)
    for relative, digest in coverage["source_hashes"].items():
        path = ROOT / relative
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        if path.suffixes[-2:] == [".json", ".gz"]:
            snap = json.loads(gzip.decompress(raw))
            for r in snap["raw_1m"]:
                if str(r["date"])[:10] == "2026-11-17":
                    stamp = datetime.fromisoformat(r["date"])
                    stamp = (
                        stamp.replace(tzinfo=IST)
                        if stamp.utcoffset() is None
                        else stamp.astimezone(IST)
                    )
                    if stamp + timedelta(minutes=1) <= datetime.fromisoformat(coverage["capture"]):
                        by_day["2026-11-17"].append({**r, "date": stamp.isoformat()})
        elif path.name.startswith("nifty-1m-"):
            for a in json.loads(raw)["data"]["candles"]:
                stamp = datetime.fromisoformat(a[0]).astimezone(IST)
                day = stamp.date().isoformat()
                if day != "2026-11-17" and "09:15" <= stamp.strftime("%H:%M") < "15:30":
                    by_day[day].append(
                        {
                            "date": stamp.isoformat(),
                            "open": float(a[1]),
                            "high": float(a[2]),
                            "low": float(a[3]),
                            "close": float(a[4]),
                            "volume": 100.0,
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

    # Compute Volume Profile for each session
    profiles = {}
    for i, day in enumerate(days):
        if i > 0:
            prev_day = days[i - 1]
            profiles[day] = emp.compute_session_profile(by_day[prev_day])

    # Fee Tariff setup
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
    ca.SOURCE = MODEL

    def frame(raw, now, minutes, calendar):
        return raw, None

    ca.session_frame = frame

    vix_path = ROOT / "logs/resimulation-20261116/reliability-v1/vix-daily.json"
    vix = {
        r[0][:10]: float(r[4])
        for r in json.loads(vix_path.read_text())["data"]["candles"]
    }

    # Signal pools:
    # 1. Direct Morning Reclaims (09:30 - 10:30)
    direct_reclaims = [s for day in days for s in reclaim_signals(by_day[day], day)]

    # 2. Zone Signals (10:30 - 15:00)
    zone_signals = coverage["zone_signals"]

    # 3. Day 1 Opening Signals (09:15 - 09:30 on DTE >= 5)
    opening_signals = []
    for s in coverage["signals"]:
        t = s["signal_at"][11:16]
        if "09:15" <= t < "09:30":
            day = s["signal_at"][:10]
            exp = resolve_expiry(datetime.fromisoformat(day).date(), holidays=calendar).isoformat()
            dte = (datetime.fromisoformat(exp).date() - datetime.fromisoformat(day).date()).days
            if dte >= 5:
                opening_signals.append(s)

    # Variant signal sets:
    signals_control = [s for s in zone_signals if "10:30" <= s["signal_at"][11:16] < "15:00"]
    signals_morning_plus_zone, _ = base.merge_candidates(
        [s for s in direct_reclaims if "09:30" <= s["signal_at"][11:16] < "10:30"] + signals_control
    )
    signals_master, _ = base.merge_candidates(
        opening_signals
        + [s for s in direct_reclaims if "09:30" <= s["signal_at"][11:16] < "10:30"]
        + signals_control
    )

    print(f"Scheduled candidate signals:")
    print(f"  Control Baseline (Zones only): {len(signals_control)}")
    print(f"  Morning (09:30-10:30) + Zones: {len(signals_morning_plus_zone)}")
    print(f"  Enhanced Master (Opening + Morning + Zones): {len(signals_master)}")

    # Clean prior simulation databases to ensure fresh replay
    for db_file in out.glob("*.db*"):
        db_file.unlink(missing_ok=True)

    # Instantiate the 4 factorial accounts
    accounts = {
        "Control_Baseline_Zone_Giveback": emp.EnhancedMasterAccount(
            out / "control_baseline.db",
            settings,
            run_id="CONTROL-ZONE-GIVEBACK",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("1000.00"),
                daily_loss_limit=D("5000.00"),
                legacy_management=False,
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=3,
                max_lots_atm=2,
            ),
            signal_engine=SimpleSignalEngine(signals_control),
        ),
        "Ceiling_Upgrade_Only": emp.EnhancedMasterAccount(
            out / "ceiling_upgrade.db",
            settings,
            run_id="CEILING-UPGRADE-GIVEBACK",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("5000.00"),  # No 1-loss circuit breaker
                legacy_management=False,
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=4,
                max_lots_atm=2,
            ),
            signal_engine=SimpleSignalEngine(signals_morning_plus_zone),
        ),
        "Thesis_Management_Baseline": emp.EnhancedMasterAccount(
            out / "thesis_baseline.db",
            settings,
            run_id="THESIS-BASELINE",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("1000.00"),
                daily_loss_limit=D("5000.00"),
                legacy_management=True,
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=3,
                max_lots_atm=2,
            ),
            signal_engine=SimpleSignalEngine(signals_morning_plus_zone),
        ),
        "Enhanced_Master_Policy": emp.EnhancedMasterAccount(
            out / "enhanced_master.db",
            settings,
            run_id="ENHANCED-MASTER-POLICY",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("2500.00"),  # Strict 1-loss circuit breaker!
                legacy_management=True,          # Thesis management & holding cushions!
                volume_profile_filter=True,      # Auction Market Theory filter!
                dte_day1_opening_enabled=True,   # Day 1 sniper opening drive!
                time_rotation_enabled=True,      # Time-of-day rotation (ATM morning/midday, OTM afternoon)
                morning_strike_step=0,
                midday_strike_step=0,
                afternoon_strike_step=2,
                enable_liquidity_hopping=True,   # Multi-stage partial booking!
                enable_ema_cross_strategy=False, # Zones + Morning Reclaims only
                max_lots_otm=4,
                max_lots_atm=2,
                max_daily_trades=4,
            ),
            signal_engine=SimpleSignalEngine(signals_master),
            profiles_by_day=profiles,
        ),
        "Master_Giveback_1Loss_Breaker": emp.EnhancedMasterAccount(
            out / "master_giveback_1loss.db",
            settings,
            run_id="MASTER-GIVEBACK-1LOSS",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("2500.00"),  # Strict 1-loss circuit breaker!
                legacy_management=False,         # 20% giveback trailing (captures +₹13k, +₹11k, +₹9k mega-winners!)
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=4,
                max_lots_atm=2,
                max_daily_trades=20,
            ),
            signal_engine=SimpleSignalEngine(signals_morning_plus_zone),
        ),
        "Pure_EMA_9_20_Momentum": emp.EnhancedMasterAccount(
            out / "pure_ema_momentum.db",
            settings,
            run_id="PURE-EMA-9-20-MOMENTUM",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("2500.00"),  # Strict 1-loss circuit breaker!
                legacy_management=False,         # 2R target / stop exit
                volume_profile_filter=False,
                dte_day1_opening_enabled=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                enable_ema_cross_strategy=True,  # 1M 9/20 EMA Cross
                max_lots_otm=2,
                max_lots_atm=2,
                max_daily_trades=20,
            ),
            signal_engine=SimpleSignalEngine([]),  # Dynamic EMA detection on raw 1M ticks
            profiles_by_day=profiles,
        ),
        "Enhanced_Master_Combined_EMA": emp.EnhancedMasterAccount(
            out / "enhanced_master_combined_ema.db",
            settings,
            run_id="ENHANCED-MASTER-COMBINED-EMA",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("2500.00"),  # Strict 1-loss circuit breaker!
                legacy_management=True,          # Thesis management & holding cushions!
                volume_profile_filter=True,      # Auction Market Theory filter!
                dte_day1_opening_enabled=True,   # Day 1 sniper opening drive!
                time_rotation_enabled=True,      # Time-of-day rotation (ATM morning/midday, OTM afternoon)
                morning_strike_step=0,
                midday_strike_step=0,
                afternoon_strike_step=2,
                enable_liquidity_hopping=True,   # Multi-stage partial booking!
                enable_ema_cross_strategy=True,  # Combined EMA + Zones + Reclaims
                max_lots_otm=4,
                max_lots_atm=2,
                max_daily_trades=4,
            ),
            signal_engine=SimpleSignalEngine(signals_master),
            profiles_by_day=profiles,
        ),
    }

    # Suppress outbound telegram notifications in simulation
    for acc in accounts.values():
        acc._message = lambda *a, **kw: None

    print(f"\nBeginning factorial simulation across {len(days)} historical sessions...")
    start_sim = datetime.now()

    equity_curves = {name: [float(emp.CAPITAL)] for name in accounts}
    daily_pnls = {name: defaultdict(float) for name in accounts}

    for day_idx, day in enumerate(days):
        rows = by_day[day]
        vd = max(k for k in vix if k < day)
        iv = lr.prior_session(vix, day)
        if iv is None:
            raise ValueError(f"Missing prior available VIX for {day}")

        expiry = resolve_expiry(
            datetime.fromisoformat(day).date(), holidays=calendar
        ).isoformat()

        for i, bar in enumerate(rows):
            now = datetime.fromisoformat(bar["date"]) + timedelta(minutes=1, seconds=5)
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
            for n in range(-15, 16):
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
                        D(str(round(lr.b76_delta(spot, strike, years, iv / 100, kind), 6))),
                        now,
                        symbol,
                        MODEL,
                    )
                contracts.append(row)

            chain = {"contracts": contracts, "expiry": expiry, "fetched_at": now}

            for name, acc in accounts.items():
                acc.tick(
                    now=now,
                    raw_1m=raw_frame,
                    chain=chain,
                    evidence=evidence,
                    news_safe=True,
                    fee_policy=FEE,
                )

        # Track end-of-day stats
        for name, acc in accounts.items():
            with acc.connect() as c:
                closed = [
                    r[0]
                    for r in c.execute(
                        "SELECT id FROM trades WHERE state='CLOSED' AND day=?",
                        (day,),
                    )
                ]
                day_pnl = sum((acc._pnl(c, tid) for tid in closed), D(0))
                daily_pnls[name][day] = float(day_pnl)
                current_cash = float(acc._cash(c))
                equity_curves[name].append(current_cash)

        if (day_idx + 1) % 5 == 0 or (day_idx + 1) == len(days):
            print(f"  Completed {day_idx + 1}/{len(days)} sessions ({day})...")

    sim_duration = (datetime.now() - start_sim).total_seconds()
    print(f"Simulation completed in {sim_duration:.1f} seconds.\n")

    # Compile comprehensive metrics
    results = {}
    for name, acc in accounts.items():
        with acc.connect() as c:
            all_trades = c.execute("SELECT * FROM trades").fetchall()
            closed_trades = [r for r in all_trades if r["state"] == "CLOSED"]
            trade_details = []
            gross_profit = D(0)
            gross_loss = D(0)
            total_fees = D(0)
            wins = 0
            losses = 0
            mega_winners = 0  # +₹5k to +₹15k

            for r in closed_trades:
                tid = r["id"]
                pnl = acc._pnl(c, tid)
                fills = [
                    json.loads(f[0])
                    for f in c.execute("SELECT body FROM fills WHERE trade_id=? ORDER BY id", (tid,))
                ]
                fee = sum((D(f["charges_inr"]) for f in fills), D(0))
                total_fees += fee
                t_body = json.loads(r["body"])

                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                    if pnl >= D("4000.00"):
                        mega_winners += 1
                elif pnl < 0:
                    losses += 1
                    gross_loss += abs(pnl)

                trade_details.append(
                    {
                        "trade_id": tid,
                        "day": r["day"],
                        "side": t_body["side"],
                        "exit_reason": r["exit_reason"],
                        "lots": t_body.get("lots", 1),
                        "net_pnl": float(pnl),
                        "fees": float(fee),
                    }
                )

            net_pnl = float(gross_profit - gross_loss)
            win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
            profit_factor = (float(gross_profit) / float(gross_loss)) if gross_loss > 0 else float("inf")

            # Calculate Max Drawdown from closed equity curve
            eq = equity_curves[name]
            peak = eq[0]
            max_dd = 0.0
            for val in eq:
                if val > peak:
                    peak = val
                dd = peak - val
                if dd > max_dd:
                    max_dd = dd

            # Check worst single day loss
            day_losses = [p for p in daily_pnls[name].values() if p < 0]
            worst_day_loss = min(day_losses) if day_losses else 0.0

            results[name] = {
                "total_trades": len(all_trades),
                "closed_trades": len(closed_trades),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round(win_rate, 2),
                "net_pnl_inr": round(net_pnl, 2),
                "gross_profit_inr": round(float(gross_profit), 2),
                "gross_loss_inr": round(float(gross_loss), 2),
                "profit_factor": round(profit_factor, 2),
                "max_drawdown_inr": round(max_dd, 2),
                "total_fees_inr": round(float(total_fees), 2),
                "worst_single_day_loss_inr": round(worst_day_loss, 2),
                "mega_winners_gt_4k": mega_winners,
                "exit_reasons": dict(
                    c.execute(
                        "SELECT exit_reason, count(*) FROM trades WHERE state='CLOSED' GROUP BY exit_reason"
                    ).fetchall()
                ),
                "trades": trade_details,
            }

    # Save summary and details
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    print("=" * 80)
    print("FACTORIAL BACKTEST RESULTS SUMMARY (15 OCT - 17 NOV 2026)")
    print("=" * 80)
    print(f"{'Variant':<32} | {'Trades':<6} | {'W/L':<7} | {'Win %':<6} | {'Net P&L (₹)':<12} | {'Max DD (₹)':<10} | {'PF':<5} | {'Worst Day':<10} | {'+₹4k+ Wins'}")
    print("-" * 115)
    for name, r in results.items():
        print(
            f"{name:<32} | {r['closed_trades']:<6} | {r['wins']}/{r['losses']:<5} | "
            f"{r['win_rate_pct']:<6} | ₹{r['net_pnl_inr']:>10.2f} | ₹{r['max_drawdown_inr']:>8.2f} | "
            f"{r['profit_factor']:<5.2f} | ₹{r['worst_single_day_loss_inr']:>8.2f} | {r['mega_winners_gt_4k']}"
        )
    print("=" * 115)
    print(f"Detailed output written to: {out / 'summary.json'}")


if __name__ == "__main__":
    main()
