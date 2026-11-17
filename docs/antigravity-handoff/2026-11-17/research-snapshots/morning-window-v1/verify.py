"""Independent ledger and risk reconciliation for isolated structure replay."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import ROUND_DOWN
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
from app.trading import paper_fees as fees


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=ROOT / "logs/resimulation-20261117/morning-window-v1/accounts",
    )
    args = parser.parse_args()
    result = json.loads((args.directory / "results.json").read_text())
    summary = {}
    tariff = result["fee_policy"]
    policy_id = tariff["policy_id"]
    fees.POLICIES_BY_ID[policy_id] = fees.FeePolicySpec(
        policy_id,
        tariff["verified_from"],
        tariff["verified_through"],
        dict(tariff["rates"]),
        (),
        "MODEL_VERIFICATION_ONLY",
    )
    curves = json.loads((args.directory / "equity.json").read_text())
    for lane, data in result["lanes"].items():
        trades = {t["id"]: t for t in data["trades"]}
        fills = sorted(
            [(f, t) for t in trades.values() for f in t["fills"]],
            key=lambda item: item[0]["id"],
        )
        cash = D(50000)
        held = None
        minimum = cash
        loss_by_day = defaultdict(lambda: D(0))
        pnl = defaultdict(lambda: D(0))
        total_fee = D(0)
        for record, t in fills:
            f = record["body"]
            qty = f["qty"]
            price = D(f["price"])
            fee = D(f["charges_inr"])
            day = record["at"][:10]
            assert (
                type(qty) is int
                and qty == t["lots"] * 65
                and 1 <= t["lots"] <= t["research_selection"]["lot_cap"]
            )
            recalculated = fees.order_charges(
                price=price,
                quantity=qty,
                side=f["side"],
                market_day=day,
                policy_id=policy_id,
                allow_historical_brokerage_assumption=True,
            )
            assert D(recalculated["total"]) == fee
            assert price == (
                D(f["ask"]) + D(".05")
                if f["side"] == "BUY"
                else max(D(".05"), D(f["bid"]) - D(".05"))
            )
            if f["side"] == "BUY":
                assert held is None
                assert t["signal_at"] <= t["opened_at"] == record["at"]
                assert record["at"][11:16] < "15:00"
                assert 10 <= D(str(t["risk_points"])) <= 55
                limit = D(t["research_selection"]["risk_budget_limit"])
                assert limit in (D(2500), D(3000))
                budget = min(limit, cash * limit / D(50000)).quantize(
                    D(".01"), rounding=ROUND_DOWN
                )
                assert D(t["stop_risk"]) <= budget
                full = D(t["full_premium_loss"])
                assert price * qty + fee <= full <= D(5000)
                assert full <= cash and full + loss_by_day[day] <= D(5000)
                held = t["id"]
                delta = -price * qty - fee
            else:
                assert held == t["id"] and t["state"] == "CLOSED"
                delta = price * qty - fee
                held = None
            cash += delta
            pnl[t["id"]] += delta
            total_fee += fee
            minimum = min(minimum, cash)
            assert cash >= 0
            if f["side"] == "SELL":
                loss_by_day[day] += max(D(0), -pnl[t["id"]])
        assert held is None and data["stats"]["held"] == 0
        assert all(D(t["net"]) == pnl[t["id"]] for t in trades.values())
        net = sum(pnl.values(), D(0))
        assert cash == D(data["stats"]["cash_inr"]) == D(50000) + net
        assert net == D(data["stats"]["closed_net_pnl_inr"]) and total_fee == D(
            data["fees"]
        )
        peak = D(50000)
        dd = D(0)
        for point in curves:
            if point["lane"] != lane:
                continue
            assert point["held"] in (0, 1) and D(point["cash"]) >= 0
            equity = D(point["equity"])
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        assert dd == D(data["max_marked_drawdown"])
        summary[lane] = {
            "trades": len(trades),
            "fills": len(fills),
            "net": str(net),
            "fees": str(total_fee),
            "min_cash": str(minimum),
            "max_marked_drawdown": str(dd),
            "one_position": True,
            "risk_cash_fee_checks": "PASS",
        }
    (args.directory / "verification.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
