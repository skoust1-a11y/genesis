"""Paper scanner supervisor. No broker order adapter or legacy journal is used."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
STATE = ROOT / "logs" / "paper-forward"
log = logging.getLogger("genesis.paper_forward")
sys.path.insert(0, str(BACKEND))


def settings():
    from app.config import get_settings

    # Pin the operational mode independently of inherited shell variables.
    os.chdir(BACKEND)
    os.environ.update(LIVE_TRADING_ENABLED="false", SANDBOX_ALLOW_LIVE="false",
                      PAPER_CAPITAL_INR="50000", TELEGRAM_DELIVERY_ENABLED="true",
                      DATA_DIR=str(STATE / "data"), GENESIS_TELEGRAM_ARMED="1")
    get_settings.cache_clear()
    config = get_settings()
    if config.live_trading_enabled or config.sandbox_allow_live:
        raise RuntimeError("Live orders must be disabled")
    return config


def notify(config, body):
    from app.telegram_transport import TelegramTransport

    outcome = TelegramTransport(config.alert_telegram_bot_token or "").send(
        config.signals_telegram_chat_id or config.alert_telegram_chat_id or "", body)
    print(json.dumps({"telegram": outcome.status, "http_status": outcome.http_status,
                      "message_id": outcome.message_id, "exception": outcome.exc_class}), flush=True)
    return outcome.status == "DELIVERED"


def status(state, *, inputs=None, last_tick=None):
    from app.trading.forward_operations import session_evidence
    from app.trading.option_quote import IST
    day = datetime.now(IST).date().isoformat()
    data = {"state": state, "updated_at": datetime.now(IST).isoformat(), "live_orders": False,
            "capital_per_account_inr": 50000, "last_tick": last_tick,
            "blockers": ["Real market-session signal-to-fill verification remains pending."]}
    if inputs:
        data.update(stream=inputs.stream.state, last_stream_tick=inputs.stream.last_tick, news=inputs.news)
        data["session_evidence"] = session_evidence(STATE / "journal.db", day, inputs.last_observation)
        if inputs.last_observation:
            history = STATE / "sessions"
            history.mkdir(exist_ok=True)
            evidence_temp = history / (day + ".json.tmp")
            evidence_temp.write_text(json.dumps(data["session_evidence"], default=str, indent=2) + "\n")
            evidence_temp.replace(history / (day + ".json"))
        if data["session_evidence"]["verified"]:
            data["blockers"] = []
        if not inputs.stream.market_open:
            data["blockers"].append("Waiting for an open derivatives market and fresh live snapshots.")
        if not inputs.headlines:
            data["blockers"].append("New entries are blocked until recent headlines are available.")
    from app.trading.forward_fees import policy
    fees = policy(day)
    data["fee_policy"] = fees.policy_id if fees else None
    if fees is None:
        data["blockers"].append("Today's paper fee tariff has not been verified; paper fills remain blocked.")
    if not inputs:
        history = sorted((STATE / "sessions").glob("*.json"))
        if history:
            data["session_evidence"] = json.loads(history[-1].read_text())
            if data["session_evidence"].get("verified"):
                data["blockers"] = [x for x in data["blockers"] if not x.startswith("Real market-session")]
    temp = STATE / "status.json.tmp"
    temp.write_text(json.dumps(data, default=str, indent=2) + "\n")
    temp.replace(STATE / "status.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Offline startup/recovery check")
    parser.add_argument("--notify", action="store_true", help="Send one configuration notification")
    parser.add_argument("--probe", action="store_true", help="Read market data once without trading")
    parser.add_argument("--probe-inputs", action="store_true", help="Verify stream and news without paper entries")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = settings()
    if not any((args.check, args.notify, args.probe, args.probe_inputs)) and (STATE / "active-run.json").exists():
        from app.trading.forward_comparison import run
        return run(STATE, config)
    from app.market.hours import holidays
    from app.trading.option_quote import IST
    from app.trading.research_runtime import ResearchRuntime, scanner_feed

    STATE.mkdir(parents=True, exist_ok=True)
    if args.probe_inputs:
        from app.trading.forward_feed import ForwardInputs
        from dotenv import dotenv_values
        token = os.environ.get("BROKER_UPSTOX_ACCESS_TOKEN") or dotenv_values(BACKEND / ".env").get("BROKER_UPSTOX_ACCESS_TOKEN")
        inputs = ForwardInputs(config, token)
        try:
            inputs.stream.start()
            inputs.refresh_news()
            time.sleep(2)
            print(json.dumps({"stream": inputs.stream.state, "market_open": inputs.stream.market_open,
                              "contracts": len(inputs.stream.contracts), "news": inputs.news}), flush=True)
            return 0 if inputs.stream.state != "DISCONNECTED" and inputs.news["headlines"] else 2
        finally:
            inputs.close()
    if args.notify:
        return 0 if notify(config, "Genesis paper forward testing configured.\n"
                          "Virtual capital: INR 50,000 per account.\n"
                          "Scanner: scanner-v0.1; market-hours operation.\n"
                          "Paper signals and simulated fills only. No real broker orders.\n"
                          "Archived historical results are preserved separately.") else 2
    if args.probe:
        view = scanner_feed()(datetime.now(IST))
        print(json.dumps({"bars_5m": len(view.df_5m), "contracts": len((view.chain or {}).get("contracts", [])),
                          "setups": len(view.setups), "rejections": view.rejections}), flush=True)
        return 0 if not view.df_5m.empty and (view.chain or {}).get("contracts") else 2
    if args.check:
        runtime = ResearchRuntime(STATE / "journal.db", lambda now: None, settings=config)
        try:
            report = runtime.start()
            print(json.dumps({"startup": "OK", "paper_capital": str(config.paper_capital_inr),
                              "live_orders": False, "market_day": report["market_day"],
                              "journal": str(runtime.path)}), flush=True)
        finally:
            runtime.stop()
        return 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runtime = None
    inputs = None
    session_day = None
    last_tick = None
    last_fee_attempt = 0
    log.info("Paper supervisor running; live orders disabled; capital INR 50000 per account")
    try:
        while not stop.is_set():
            now = datetime.now(IST)
            active = now.weekday() < 5 and now.date().isoformat() not in holidays(config) and "09:10" <= now.strftime("%H:%M") < "15:40"
            from app.trading.forward_fees import attest, policy
            if active and policy(now.date().isoformat()) is None and time.monotonic() - last_fee_attempt >= 300:
                last_fee_attempt = time.monotonic()
                try:
                    attest(now=now)
                except Exception as exc:  # noqa: BLE001 - absent proof blocks fills; continue monitoring existing exits
                    log.warning("Paper tariff verification unavailable: %s", type(exc).__name__)
            if runtime and (not active or session_day != now.date()):
                runtime.stop()
                runtime = None
                inputs.close()
                inputs = None
            if active:
                if runtime is None:
                    from app.brokers.upstox_stream import SOURCE
                    from app.trading.forward_feed import ForwardInputs
                    from dotenv import dotenv_values
                    token = os.environ.get("BROKER_UPSTOX_ACCESS_TOKEN") or dotenv_values(BACKEND / ".env").get("BROKER_UPSTOX_ACCESS_TOKEN")
                    inputs = ForwardInputs(config, token)
                    inputs.start()
                    runtime = ResearchRuntime(STATE / "journal.db", inputs, settings=config,
                                              trusted_delta_sources=frozenset({SOURCE}))
                    runtime.start()
                    session_day = now.date()
                    notify(config, f"Genesis PAPER session started: {session_day}.\n"
                           "INR 50,000 per account. No real orders. Signals depend on valid market data.")
                result = runtime.tick()
                last_tick = result
                log.info("Paper tick: %s", json.dumps(result, default=str))
            status("SCANNING" if active else "WAITING_FOR_MARKET", inputs=inputs, last_tick=last_tick)
            stop.wait(30)
    finally:
        if runtime:
            runtime.stop()
        if inputs:
            inputs.close()
        status("STOPPED", last_tick=last_tick)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
