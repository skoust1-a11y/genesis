"""Independent same-signal recorded-book shadows; no portfolio/trade delivery."""

import gzip
import hashlib
import json
import socket
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


def offline(*args, **kwargs):
    raise RuntimeError("No network in recorded shadow")


socket.socket.connect = offline
socket.getaddrinfo = offline
from app.brokers.upstox_stream import SOURCE
from app.config import Settings
from app.trading import (
    paper_exec as px,
)
from app.trading import (
    paper_fees as fees,
)
from app.trading import (
    paper_profit_trail as trail,
)
from app.trading import (
    paper_risk as risk,
)
from app.trading.forward_fees import from_id
from app.trading.option_quote import IST

OUT = Path(__file__).resolve().parent
from research_limits import install

install()
c = json.loads((OUT.parent / "expiry-time-v1/coverage/results.json").read_text())
signals = [
    next(
        s
        for s in c["signals"]
        if s["signal_at"][11:16] == "13:38"
        and s["day"] == "2026-11-17"
        and s["side"] == "CALL"
    ),
    next(
        s
        for s in c["zone_signals"]
        if s["signal_at"][11:16] == "13:52" and s["day"] == "2026-11-17"
    ),
]
signals = [{**s, "research_budget": budget} for budget in (2500, 3000) for s in signals]
m = json.loads((ROOT / "logs/paper-forward/active-run.json").read_text())
files = sorted(
    (Path(m["directory"]) / "evidence/2026-11-17").glob("*-candidate.json.gz")
)
settings = Settings(
    _env_file=None,
    data_dir=str(ROOT / "logs/paper-forward/data"),
    live_trading_enabled=False,
    sandbox_allow_live=False,
    paper_exit_quote_policy="fresh-bid-v1",
)
lanes = {}
receipts = {}
for file in files:
    if file.name[:6] < "133800":
        continue
    try:
        raw = file.read_bytes()
        x = json.loads(gzip.decompress(raw))
    except (EOFError, OSError):
        continue
    now = datetime.fromisoformat(x["observed_at"])
    receipts[file.name] = hashlib.sha256(raw).hexdigest()
    chain = {
        **x["chain"],
        "fetched_at": datetime.fromisoformat(x["chain"]["fetched_at"]),
    }
    fee = x["fee_policy_id"]
    if fee not in fees.POLICIES_BY_ID:
        fees.POLICIES_BY_ID[fee] = from_id(fee)
    evidence = {
        k: risk.DeltaEvidence(
            D(v["value"]),
            datetime.fromisoformat(v["observed_at"]),
            v["contract"],
            v["source"],
        )
        for k, v in x["delta_evidence"].items()
    }
    bar = None
    for r in reversed(x["raw_1m"]):
        stamp = datetime.fromisoformat(r["date"])
        stamp = (
            stamp.replace(tzinfo=IST)
            if stamp.utcoffset() is None
            else stamp.astimezone(IST)
        )
        if stamp.date() == now.date() and stamp + timedelta(minutes=1) <= now:
            bar = {**r, "date": stamp}
            break
    for signal in signals:
        signal_at = datetime.fromisoformat(signal["signal_at"])
        key = signal_at.strftime("%H:%M") + "_" + str(signal["research_budget"])
        if key not in lanes and 0 <= (now - signal_at).total_seconds() <= 90:
            lanes[key] = {}
            for name, step in [("ITM", -1), ("ATM", 0), ("OTM", 2)]:
                strike = round(signal["entry_spot"] / 50) * 50 + step * 50
                q = px.contract_quote(
                    chain,
                    strike=strike,
                    option_type="CE",
                    expiry="2026-11-17",
                    underlying="NIFTY",
                )
                sized = risk.size(
                    quote=q,
                    evidence=evidence.get(q.tradingsymbol) if q else None,
                    trusted_sources=frozenset({SOURCE}),
                    now=now,
                    settings=settings,
                    underlying_entry=D(str(signal["entry_spot"])),
                    underlying_stop=D(str(signal["stop_spot"])),
                    stop_budget=D(signal["research_budget"]),
                    max_lots=4 if name == "OTM" else 2,
                    gross_daily_loss=D(0),
                    existing_reservations=D(0),
                    available_cash=D(50000),
                    cost_policy_id=fee,
                )
                lanes[key][name] = {
                    "signal": signal,
                    "strike": strike,
                    "entry_at": now.isoformat(),
                    "eligible": sized.entry is not None,
                    "rejection": sized.reason,
                    "entry": asdict(sized.entry) if sized.entry else None,
                    "risk": str(sized.estimated_stop_risk) if sized.entry else None,
                    "peak": D(0),
                    "closed": False,
                }
            continue
        for name, lane in lanes.get(key, {}).items():
            if not lane["eligible"] or lane["closed"]:
                continue
            q = px.contract_quote(
                chain,
                strike=lane["strike"],
                option_type="CE",
                expiry="2026-11-17",
                underlying="NIFTY",
            )
            q, why = px.exit_executable(q, now, settings)
            if q is None:
                continue
            entry = lane["entry"]
            sell = px.fill(
                q,
                "SELL",
                entry["lots"],
                now=now,
                settings=settings,
                ticks=1,
                cost_policy_id=fee,
            )
            net = (
                -D(entry["price"]) * entry["qty"]
                - D(entry["charges_inr"])
                + sell.value
                - sell.charges_inr
            )
            mark = trail.evaluate(
                previous_peak=lane["peak"],
                current_net=net,
                initial_risk=lane["risk"],
                percent=20,
            )
            reason = None
            if now.strftime("%H:%M") >= "15:10":
                reason = "SQUARE_OFF"
            elif bar and (
                bar["close"] <= signal["stop_spot"]
                or bar["date"] >= datetime.fromisoformat(lane["entry_at"])
                and bar["low"] <= signal["stop_spot"]
            ):
                reason = "STOP"
            elif mark.triggered:
                reason = "PEAK_PROFIT_GIVEBACK"
            lane.update(peak=mark.peak, last_net=str(net), last_at=now.isoformat())
            if reason:
                lane.update(
                    closed=True,
                    exit_at=now.isoformat(),
                    exit_reason=reason,
                    exit=asdict(sell),
                    net=str(net),
                )
result = {
    "scope": "Recorded main-loop books about 11 seconds apart, not full 1-second live monitor. Each signal and strike independently sized on fresh 50,000 capital with no previous losses/positions. No portfolio P&L aggregation. Research budgets 2500/3000; ATM/ITM cap 2 lots, OTM cap 4. Premium and daily caps remain 5000. Active policy unchanged. ITM is one strike below nearest-50 ATM for these CALLs; OTM two above. Real verified daily fees; no messages, live orders, policy changes or synthetic quote substitution. Only late-session examples available; cannot establish morning-vs-late superiority.",
    "lanes": lanes,
    "source_hashes": receipts,
    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
}
(OUT / "recorded-results.json").write_text(
    json.dumps(result, indent=2, default=str) + "\n"
)
for key, items in lanes.items():
    print(
        key,
        {
            k: {
                a: v.get(a)
                for a in [
                    "strike",
                    "eligible",
                    "rejection",
                    "risk",
                    "entry_at",
                    "net",
                    "last_net",
                    "peak",
                    "exit_at",
                    "exit_reason",
                ]
            }
            for k, v in items.items()
        },
    )
