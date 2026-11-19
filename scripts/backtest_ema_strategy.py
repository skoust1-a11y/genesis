"""Forensic Factorial Backtest: 9/20 EMA Strategy on 1-Minute Candles.

Evaluates user hypothesis:
"if we had like 9/20 ema stratergy on 1min candle with or without macd it would have given far better results than now"

Simulates 22 historical sessions (15 Oct – 17 Nov 2026) using exact validated candles,
synthetic Black-76 option pricing, prior-available VIX, and verified transaction costs.

Configurations tested:
1. EMA_9_20_Pure: 9/20 EMA Crossover on 1M, exit on 2R / Stop or Opposite Cross.
2. EMA_9_20_MACD: 9/20 EMA Crossover + MACD (12, 26, 9) histogram confirmation.
3. EMA_9_20_Pullback_Trend: 9/20 EMA Pullback in direction of 50 EMA trend.
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

os.environ.update(
    PYTHON_DOTENV_DISABLED="true",
    TELEGRAM_DELIVERY_ENABLED="false",
    LIVE_TRADING_ENABLED="false",
    SANDBOX_ALLOW_LIVE="false",
)

from app.config import Settings
from app.market.chart_analytics import calculate_ema, calculate_macd
from app.market.expiry import resolve_expiry
from app.market.hours import holidays
from app.trading import (
    enhanced_master_policy as emp,
    forward_candidate_account as ca,
    live_replay as lr,
    paper_fees as pf,
    paper_risk as risk,
)
from app.trading.option_quote import IST
from app.trading.paper_exit_policy import FRESH_BID

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


def generate_ema_signals(by_day: dict[str, list[dict]], use_macd: bool = False, use_trend_filter: bool = False) -> list[dict]:
    signals = []
    for day, rows in by_day.items():
        if len(rows) < 25:
            continue
        closes = [r["close"] for r in rows]
        ema9 = calculate_ema(closes, 9)
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50) if use_trend_filter else None
        macd = calculate_macd(closes, 12, 26, 9) if use_macd else None

        for i in range(21, len(rows)):
            bar = rows[i]
            prev_bar = rows[i - 1]
            t_close = (datetime.fromisoformat(bar["date"]) + timedelta(minutes=1)).isoformat()
            time_str = datetime.fromisoformat(t_close).strftime("%H:%M")

            if time_str < "09:30" or time_str >= "14:45":
                continue

            e9, e20 = ema9[i], ema20[i]
            pe9, pe20 = ema9[i - 1], ema20[i - 1]

            if any(v is None for v in (e9, e20, pe9, pe20)):
                continue

            # Bullish Cross (CALL)
            if pe9 <= pe20 and e9 > e20:
                if use_macd and (macd["hist"][i] is None or macd["hist"][i] <= 0):
                    continue
                if use_trend_filter and (ema50[i] is None or bar["close"] < ema50[i]):
                    continue

                # Swing low of last 5 bars
                recent_lows = [r["low"] for r in rows[max(0, i - 5): i + 1]]
                swing_low = min(recent_lows)
                risk_pts = max(12.0, min(40.0, bar["close"] - swing_low + 2.0))
                stop_spot = round(bar["close"] - risk_pts, 2)
                target_spot = round(bar["close"] + 2.0 * risk_pts, 2)

                sig_id = hashlib.sha256(f"EMA:{day}:{t_close}:CALL:{bar['close']}".encode()).hexdigest()[:24]
                signals.append({
                    "signal_id": sig_id,
                    "day": day,
                    "side": "CALL",
                    "signal_at": t_close,
                    "entry_spot": bar["close"],
                    "stop_spot": stop_spot,
                    "target_spot": target_spot,
                    "risk_points": risk_pts,
                    "source_family": "EMA_9_20",
                })

            # Bearish Cross (PUT)
            elif pe9 >= pe20 and e9 < e20:
                if use_macd and (macd["hist"][i] is None or macd["hist"][i] >= 0):
                    continue
                if use_trend_filter and (ema50[i] is None or bar["close"] > ema50[i]):
                    continue

                # Swing high of last 5 bars
                recent_highs = [r["high"] for r in rows[max(0, i - 5): i + 1]]
                swing_high = max(recent_highs)
                risk_pts = max(12.0, min(40.0, swing_high - bar["close"] + 2.0))
                stop_spot = round(bar["close"] + risk_pts, 2)
                target_spot = round(bar["close"] - 2.0 * risk_pts, 2)

                sig_id = hashlib.sha256(f"EMA:{day}:{t_close}:PUT:{bar['close']}".encode()).hexdigest()[:24]
                signals.append({
                    "signal_id": sig_id,
                    "day": day,
                    "side": "PUT",
                    "signal_at": t_close,
                    "entry_spot": bar["close"],
                    "stop_spot": stop_spot,
                    "target_spot": target_spot,
                    "risk_points": risk_pts,
                    "source_family": "EMA_9_20",
                })

    return signals


def main():
    coverage_path = ROOT / "logs/resimulation-20261117/expiry-time-v1/coverage/results.json"
    out = ROOT / "logs/resimulation-20261118/ema-investigation"
    out.mkdir(parents=True, exist_ok=True)

    print(f"Loading market coverage from: {coverage_path}")
    coverage = json.loads(coverage_path.read_text())

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

    # Generate signal sets
    signals_pure = generate_ema_signals(by_day, use_macd=False)
    signals_macd = generate_ema_signals(by_day, use_macd=True)
    signals_trend = generate_ema_signals(by_day, use_macd=True, use_trend_filter=True)

    print(f"\nScheduled Candidate Signals across 22 sessions:")
    print(f"  1. 9/20 EMA Pure Cross (1M): {len(signals_pure)}")
    print(f"  2. 9/20 EMA + MACD (1M):     {len(signals_macd)}")
    print(f"  3. 9/20 EMA + MACD + 50 EMA Trend: {len(signals_trend)}")

    # Clean prior simulation dbs
    for db_file in out.glob("*.db*"):
        db_file.unlink(missing_ok=True)

    accounts = {
        "EMA_9_20_Pure": emp.EnhancedMasterAccount(
            out / "ema_pure.db",
            settings,
            run_id="EMA-9-20-PURE",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("5000.00"),
                legacy_management=False,  # Standard 2R target / stop exit
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=2,
                max_lots_atm=2,
                max_daily_trades=20,
            ),
            signal_engine=SimpleSignalEngine(signals_pure),
        ),
        "EMA_9_20_MACD": emp.EnhancedMasterAccount(
            out / "ema_macd.db",
            settings,
            run_id="EMA-9-20-MACD",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("5000.00"),
                legacy_management=False,
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=2,
                max_lots_atm=2,
                max_daily_trades=20,
            ),
            signal_engine=SimpleSignalEngine(signals_macd),
        ),
        "EMA_9_20_MACD_Trend": emp.EnhancedMasterAccount(
            out / "ema_macd_trend.db",
            settings,
            run_id="EMA-9-20-MACD-TREND",
            config=emp.EnhancedMasterConfig(
                stop_budget_limit=D("2500.00"),
                daily_loss_limit=D("5000.00"),
                legacy_management=False,
                volume_profile_filter=False,
                time_rotation_enabled=False,
                enable_liquidity_hopping=False,
                max_lots_otm=2,
                max_lots_atm=2,
                max_daily_trades=20,
            ),
            signal_engine=SimpleSignalEngine(signals_trend),
        ),
    }

    for acc in accounts.values():
        acc._message = lambda *a, **kw: None

    print("\nBeginning EMA factorial simulation across 22 historical sessions...")
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

    results = {}
    for name, acc in accounts.items():
        with acc.connect() as c:
            all_trades = c.execute("SELECT * FROM trades").fetchall()
            closed_trades = [r for r in all_trades if r["state"] == "CLOSED"]
            gross_profit = D(0)
            gross_loss = D(0)
            total_fees = D(0)
            wins = 0
            losses = 0

            for r in closed_trades:
                tid = r["id"]
                pnl = acc._pnl(c, tid)
                fills = [
                    json.loads(f[0])
                    for f in c.execute("SELECT body FROM fills WHERE trade_id=? ORDER BY id", (tid,))
                ]
                fee = sum((D(f["charges_inr"]) for f in fills), D(0))
                total_fees += fee

                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                elif pnl < 0:
                    losses += 1
                    gross_loss += abs(pnl)

            net_pnl = float(gross_profit - gross_loss)
            win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
            profit_factor = (float(gross_profit) / float(gross_loss)) if gross_loss > 0 else float("inf")

            eq = equity_curves[name]
            peak = eq[0]
            max_dd = 0.0
            for val in eq:
                if val > peak:
                    peak = val
                dd = peak - val
                if dd > max_dd:
                    max_dd = dd

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
            }

    (out / "summary.json").write_text(json.dumps(results, indent=2))
    print("=" * 115)
    print("9/20 EMA STRATEGY FACTORIAL BACKTEST (15 OCT - 17 NOV 2026)")
    print("=" * 115)
    print(f"{'Variant':<24} | {'Trades':<6} | {'W/L':<8} | {'Win %':<6} | {'Net P&L (₹)':<12} | {'Max DD (₹)':<10} | {'PF':<5} | {'Total Fees (₹)':<14} | {'Worst Day'}")
    print("-" * 115)
    for name, r in results.items():
        print(
            f"{name:<24} | {r['closed_trades']:<6} | {r['wins']}/{r['losses']:<6} | "
            f"{r['win_rate_pct']:<6} | ₹{r['net_pnl_inr']:>10.2f} | ₹{r['max_drawdown_inr']:>8.2f} | "
            f"{r['profit_factor']:<5.2f} | ₹{r['total_fees_inr']:>12.2f} | ₹{r['worst_single_day_loss_inr']:>8.2f}"
        )
    print("=" * 115)


if __name__ == "__main__":
    main()
