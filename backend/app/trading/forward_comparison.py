"""Supervise the authorized two-account PAPER experiment; existing history is read-only."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import signal
import threading
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from app.db import crew_journal as cj
from app.trading import forward_fees, paper_exit_policy, paper_profit_trail, paper_stats
from app.trading.forward_candidate_account import CandidateAccount, encode
from app.trading.forward_feed import ForwardInputs
from app.trading.forward_health import ScanCoverage, baseline_issues
from app.trading.option_quote import IST
from app.trading.research_runtime import ResearchRuntime

log = logging.getLogger(__name__)


def shutdown_safe_view(view, stop):
    """A feed already in flight may complete after a shutdown request."""
    return replace(view, setups=[]) if stop.is_set() else view


def candidate_admission_blocker(stop, inputs):
    if stop.is_set():
        return "SUPERVISOR_STOPPING"
    return "PROFIT_MONITOR_FAILED" if inputs.profit_watch_error else None


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def record_inputs(run_root, lane, now, payload):
    """Append-only receipts contain market data only, never auth headers or credentials."""
    directory = Path(run_root) / "evidence" / now.date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.strftime('%H%M%S%f')}-{lane}.json.gz"
    raw = encode({"lane": lane, "observed_at": now.isoformat(), **payload}).encode()
    with gzip.open(path, "xb") as stream:
        stream.write(raw)
    return {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def execution_settings(config, manifest):
    policy = manifest.get("exit_quote_policy", paper_exit_policy.LEGACY)
    digest = paper_exit_policy.identity(policy)
    if manifest.get("exit_quote_policy_identity") != digest and (
        policy != paper_exit_policy.LEGACY or manifest.get("exit_quote_policy_identity") is not None
    ):
        raise ValueError("Paper exit policy/source changed; create a new experiment")
    percent = manifest.get("candidate_profit_giveback_pct")
    trail_digest = paper_profit_trail.identity(percent)
    if manifest.get("candidate_profit_policy_identity") != trail_digest and (
        percent is not None or manifest.get("candidate_profit_policy_identity") is not None
    ):
        raise ValueError("Candidate profit policy/source changed; create a new experiment")
    return config.model_copy(update={"paper_exit_quote_policy": policy,
        "paper_candidate_profit_giveback_pct": Decimal(str(percent)) if percent is not None else None}), policy, digest


def start_profit_watch(candidate, inputs):
    """Protective quote-only exits continue independently of slow candle/network scans."""
    if candidate.settings.paper_candidate_profit_giveback_pct is None:
        return

    def watch():
        while not inputs.stop_event.wait(1):
            now = datetime.now(IST)
            try:
                def fees(stamp=now):
                    policy = forward_fees.policy(stamp.date().isoformat())
                    return policy.policy_id if policy else None

                candidate.monitor_profit(now=now, chain_source=inputs.stream.chain, fee_policy=fees)
                inputs.profit_watch_error = None
            except Exception as exc:  # noqa: BLE001 - block new admissions, retain exit monitoring/reconciliation
                inputs.profit_watch_error = type(exc).__name__
                log.error("Paper profit monitor failed: %s", type(exc).__name__)

    inputs.profit_watch_thread = threading.Thread(target=watch, name="paper-profit-exits", daemon=True)
    inputs.profit_watch_thread.start()


def baseline_stats(path):
    c = cj.connect_readonly(path)
    try:
        stats = paper_stats.account_stats(c, "SHARED")
        counts = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT state,count(*) FROM crew_paper_trades WHERE account_id='SHARED' GROUP BY state"
            )
        }
        reasons = Counter(
            reason
            for row in c.execute(
                "SELECT reasons FROM crew_paper_decisions WHERE account_id='SHARED' AND outcome<>'OPENED'"
            )
            for reason in json.loads(row[0])
        )
        return {
            "label": "Baseline",
            "strategy_version": "scanner-v0.1",
            "capital_inr": "50000.00",
            "equity_inr": stats["accounting"]["equity_inr"],
            "cash_inr": stats["accounting"]["cash_inr"],
            "closed_net_pnl_inr": stats["net_pnl_inr"],
            "entries": sum(counts.values()),
            "closed": counts.get("CLOSED", 0),
            "rejections": [
                {"reason": key, "count": count}
                for key, count in sorted(reasons.items())
            ],
            "held": sum(v for k, v in counts.items() if k != "CLOSED"),
            "unresolved": sum(
                v
                for k, v in counts.items()
                if k not in ("OPEN", "CLOSED", "PARTIALLY_EXITED")
            ),
        }
    finally:
        c.close()


def copy_baseline_notifications(candidate, baseline_path):
    """Publish shared-account events only. Family accounts remain diagnostic, never summed."""
    source = cj.connect_readonly(baseline_path)
    try:
        with candidate.connect() as dest:
            prior = dest.execute(
                "SELECT value FROM cursors WHERE name='baseline'"
            ).fetchone()
            cursor = prior[0] if prior else 0
            events = source.execute(
                "SELECT e.*,t.strategy_id,t.direction,t.strike,t.option_type,t.expiry,"
                "t.entry_spot,t.stop_spot,t.target_spot FROM crew_paper_events e JOIN crew_paper_trades t "
                "USING(paper_trade_id) WHERE e.event_id>? AND t.account_id='SHARED' ORDER BY e.event_id",
                (cursor,),
            ).fetchall()
            for e in events:
                body = (
                    f"GENESIS PAPER | BASELINE | {candidate.run_id}\n{e['event_type']} {e['reason']}\n"
                    f"{e['occurred_at']} IST\n{e['strategy_id']} | {e['direction']}\n"
                    f"{e['strike']:g} {e['option_type']} | expiry {e['expiry']}\n"
                    f"Spot entry {e['entry_spot']:.2f} | stop {e['stop_spot']:.2f} | target {e['target_spot']:.2f}\n"
                    f"Fill premium {e['price_inr'] or 'UNPRICED'} | qty {e['qty'] or 'unpriced'}\n"
                    "PAPER ONLY. No real broker order."
                )
                dest.execute(
                    "INSERT OR IGNORE INTO outbox(key,body,created_at,actionable) VALUES (?,?,?,?)",
                    (
                        f"baseline:{e['event_id']}",
                        body,
                        e["occurred_at"],
                        int(e["event_type"] == "ENTRY"),
                    ),
                )
            if events:
                dest.execute(
                    "INSERT OR REPLACE INTO cursors VALUES ('baseline',?)",
                    (events[-1]["event_id"],),
                )
    finally:
        source.close()


def dispatch(candidate, config):
    """One durable send attempt. UNKNOWN or rejected messages are never blindly retried."""
    from app.telegram_transport import TelegramTransport, delivery_authorized

    chat = config.signals_telegram_chat_id or config.alert_telegram_chat_id or ""
    if not config.telegram_delivery_enabled or not delivery_authorized(chat):
        return
    with candidate.connect() as c:
        rows = c.execute(
            "SELECT key,body,created_at,actionable FROM outbox WHERE state='PENDING' ORDER BY rowid"
        ).fetchall()
    for row in rows:
        with candidate.connect() as c:
            stamp = datetime.fromisoformat(row["created_at"])
            if stamp.utcoffset() is None:
                stamp = stamp.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - stamp).total_seconds()
            if row["actionable"] and not 0 <= age <= 120:
                c.execute(
                    "UPDATE outbox SET state='SUPPRESSED',result=? WHERE key=? AND state='PENDING'",
                    (encode({"reason": "ENTRY_ALERT_STALE"}), row["key"]),
                )
                continue
            # Persist before network IO. A crash after this point is an UNKNOWN outcome.
            claimed = c.execute(
                "UPDATE outbox SET state='UNKNOWN' WHERE key=? AND state='PENDING'",
                (row["key"],),
            )
            if claimed.rowcount != 1:
                continue
        result = TelegramTransport(config.alert_telegram_bot_token or "").send(
            chat, row["body"]
        )
        with candidate.connect() as c:
            c.execute(
                "UPDATE outbox SET state=?,result=? WHERE key=?",
                (
                    result.status,
                    encode(
                        {
                            "http_status": result.http_status,
                            "message_id": result.message_id,
                            "exception": result.exc_class,
                        }
                    ),
                    row["key"],
                ),
            )


def initialize_baseline(path, config):
    runtime = ResearchRuntime(
        path,
        lambda now: None,
        settings=config.model_copy(
            update={
                "telegram_delivery_enabled": False,
                "paper_capital_inr": Decimal(50000),
            }
        ),
    )
    try:
        runtime.start()
    finally:
        runtime.stop("COMPARISON_INITIALIZED")


def run(state_root, config):
    """Called by the existing launch-agent entry point when active-run.json is present."""
    root = Path(state_root).resolve()
    manifest = json.loads((root / "active-run.json").read_text())
    if (
        manifest.get("mode") != "PAPER_COMPARISON"
        or config.live_trading_enabled
        or config.sandbox_allow_live
        or config.paper_capital_inr != Decimal(50000)
        or manifest["notification_chat_id"]
        != (config.signals_telegram_chat_id or config.alert_telegram_chat_id)
    ):
        raise ValueError("Invalid paper comparison configuration")
    run_root = Path(manifest["directory"]).resolve()
    if not run_root.is_relative_to(root / "runs"):
        raise ValueError(
            "Comparison journals must live under the declared runs directory"
        )
    config, exit_policy, expected_policy = execution_settings(config, manifest)
    from app.trading import paper_registry
    from app.trading.forward_zone_candidate import RULE_HASH

    if (
        manifest["baseline_registry_hash"] != paper_registry.REGISTRY_HASH
        or manifest["candidate_rules_hash"] != RULE_HASH
    ):
        raise ValueError("Strategy identity changed; create a new experiment")
    for relative, expected in manifest["reference_hashes"].items():
        if (
            hashlib.sha256((Path(config.data_dir) / relative).read_bytes()).hexdigest()
            != expected
        ):
            raise ValueError(
                "Reference data changed; review this experiment before resuming"
            )
    from app.trading.dual_agent_shadow_account import DualAgentShadowAccount
    from app.trading.enhanced_master_policy import EnhancedMasterAccount, EnhancedMasterConfig

    candidate = EnhancedMasterAccount(
        run_root / "candidate.db",
        config,
        run_id=manifest["run_id"],
        config=EnhancedMasterConfig(
            enable_ema_cross_strategy=True,
            stop_budget_limit=Decimal("2500.00"),
            daily_loss_limit=Decimal("2500.00"),
            legacy_management=False,
            time_rotation_enabled=True,
            enable_liquidity_hopping=False,
            max_lots_otm=4,
            max_lots_atm=2,
            max_daily_trades=10,
        ),
    )
    dual_agent_shadow = DualAgentShadowAccount(run_root / "dual_agent_shadow.db")
    baseline_path = run_root / "baseline.db"
    if not baseline_path.exists():
        initialize_baseline(baseline_path, config)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runtime = inputs = None
    session_day = None
    latest = {}
    fee_attempt = 0.0
    coverage = ScanCoverage(run_root)
    input_issues = []
    try:
        while not stop.is_set():
            now = datetime.now(IST)
            from app.market.hours import holidays

            active = (
                now.weekday() < 5
                and now.date().isoformat() not in holidays(config)
                and "09:10" <= now.strftime("%H:%M") < "15:40"
            )
            if (
                active
                and forward_fees.policy(now.date().isoformat()) is None
                and time.monotonic() - fee_attempt > 300
            ):
                fee_attempt = time.monotonic()
                try:
                    forward_fees.attest(now=now)
                except (OSError, ValueError, httpx.HTTPError) as exc:
                    log.warning(
                        "Comparison fee evidence unavailable: %s", type(exc).__name__
                    )
            if runtime and (not active or session_day != now.date()):
                runtime.stop("COMPARISON_SESSION_END")
                inputs.close()
                runtime = inputs = None
            if active:
                if runtime is None:
                    from dotenv import dotenv_values

                    from app.brokers.upstox_stream import SOURCE

                    backend = Path(__file__).resolve().parents[2]
                    import os

                    token = os.environ.get(
                        "BROKER_UPSTOX_ACCESS_TOKEN"
                    ) or dotenv_values(backend / ".env").get(
                        "BROKER_UPSTOX_ACCESS_TOKEN"
                    )
                    inputs = ForwardInputs(config, token)
                    inputs.start()
                    start_profit_watch(candidate, inputs)

                    # The immutable baseline keeps its own outbox non-deliverable; shared events
                    # are copied above into the experiment's separately labelled outbox.
                    def baseline_feed(clock, source=inputs):
                        view = shutdown_safe_view(source(clock), stop)
                        input_issues[:] = baseline_issues(view)
                        record_inputs(
                            run_root,
                            "baseline",
                            clock,
                            {
                                "df_5m": view.df_5m.to_dict(orient="records"),
                                "chain": view.chain,
                                "delta_evidence": {
                                    k: asdict(v) for k, v in view.delta_evidence.items()
                                },
                                "setups": [asdict(v) for v in view.setups],
                                "rejections": view.rejections,
                                "key_levels": view.key_levels,
                                "headlines": source.scanner.news_agent.cached_headlines,
                                "provider_live_prints": source.scanner.feed_agent.live_prints,
                            },
                        )
                        return view

                    baseline_feed.sources = inputs.sources
                    runtime = ResearchRuntime(
                        baseline_path,
                        baseline_feed,
                        settings=config.model_copy(
                            update={"telegram_delivery_enabled": False}
                        ),
                        trusted_delta_sources=frozenset({SOURCE}),
                    )
                    runtime.start()
                    session_day = now.date()
                latest["baseline"] = runtime.tick()
                raw = inputs.scanner.feed_agent.fetch_timeframe_candles(
                    "1m", limit_bars=400
                )
                page = inputs.stream.chain().get("optionChain", {})
                evidence = inputs.stream.evidence()
                candidate_now = datetime.now(IST)
                chain = {
                    "contracts": page.get("contracts", []),
                    "expiry": page.get("expiry") or inputs.stream.expiry,
                    "fetched_at": candidate_now,
                }
                headlines = inputs.scanner.news_agent.cached_headlines
                news_safe = (
                    bool(headlines)
                    and inputs.scanner.news_agent.evaluate_news_sentiment(
                        current_time_str=now.strftime("%H:%M")
                    ).is_trading_safe
                )
                policy = forward_fees.policy(now.date().isoformat())
                record_inputs(
                    run_root,
                    "candidate",
                    candidate_now,
                    {
                        "raw_1m": raw.to_dict(orient="records"),
                        "chain": chain,
                        "delta_evidence": {k: asdict(v) for k, v in evidence.items()},
                        "news_safe": news_safe,
                        "headlines": headlines,
                        "fee_policy_id": policy.policy_id if policy else None,
                    },
                )
                latest["candidate"] = candidate.tick(
                    now=candidate_now,
                    raw_1m=raw,
                    chain=chain,
                    evidence=evidence,
                    news_safe=news_safe,
                    fee_policy=policy.policy_id if policy else None,
                    admission_blocker=candidate_admission_blocker(stop, inputs),
                )
                try:
                    latest["dual_agent_shadow"] = dual_agent_shadow.tick(
                        now=candidate_now,
                        raw_1m=raw,
                        chain=chain,
                        news_safe=news_safe,
                        fee_policy=policy.policy_id if policy else None,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate shadow account from primary accounts
                    log.warning("Dual-agent shadow tick failed: %s", type(exc).__name__)
                    latest["dual_agent_shadow"] = {"state": "ERROR", "error": type(exc).__name__}
            copy_baseline_notifications(candidate, baseline_path)
            dispatch(candidate, config)
            accounts = {
                "baseline": baseline_stats(baseline_path),
                "candidate": candidate.stats(),
                "dual_agent_shadow": dual_agent_shadow.stats(),
            }
            if active and now.strftime("%H:%M") >= "15:30":
                text = f"GENESIS PAPER COMPARISON | {manifest['run_id']}\n{now.date()} end-of-day cumulative results\n"
                for account in accounts.values():
                    equity = (
                        account["equity_inr"]
                        if account["equity_inr"] is not None
                        else "UNKNOWN (held/unpriced)"
                    )
                    text += (
                        f"{account['label']}: {account['entries']} entries, {account['closed']} closed; "
                        f"closed net INR {account['closed_net_pnl_inr']}; equity {equity}; "
                        f"unresolved {account['unresolved']}\n"
                    )
                text += "Separate INR 50,000 paper accounts. No real orders."
                with candidate.connect() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO outbox(key,body) VALUES (?,?)",
                        (f"summary:{now.date()}", text),
                    )
            blockers = [
                "New forward experiment: market-session performance is not yet established."
            ]
            if inputs and not inputs.stream.market_open:
                blockers.append(
                    "Waiting for fresh full snapshots from an open derivatives market."
                )
            if inputs and not inputs.scanner.news_agent.cached_headlines:
                blockers.append("Recent news is unavailable; new entries are blocked.")
            if forward_fees.policy(now.date().isoformat()) is None:
                blockers.append(
                    "Today's tariff evidence is unavailable; paper fills are blocked."
                )
            if (
                accounts["baseline"]["unresolved"]
                or accounts["candidate"]["unresolved"]
            ):
                blockers.append(
                    "An account has an unresolved exit; its exposure is retained and requires reconciliation."
                )
            completed_at = datetime.now(IST)
            health = None
            if active:
                issues = list(input_issues)
                if inputs.profit_watch_error:
                    issues.append("PROFIT_MONITOR_FAILED")
                if latest.get("baseline", {}).get("skipped"):
                    issues.append("BASELINE:" + latest["baseline"]["skipped"])
                candidate_tick = latest.get("candidate", {})
                if candidate_tick.get("state") == "WAITING_FOR_1M":
                    issues.extend("CANDIDATE:" + reason for reason in candidate_tick.get("reasons", []))
                if not news_safe:
                    issues.append("CANDIDATE_NEWS_UNAVAILABLE_OR_UNSAFE")
                if forward_fees.policy(now.date().isoformat()) is None:
                    issues.append("FEE_POLICY_UNVERIFIED")
                health = coverage.complete(completed_at, issues)
                blockers.extend(health["current_issues"])
                if not health["continuous_coverage"]:
                    blockers.append(f"Session coverage incomplete: {health['gap_count']} scan gap(s) over 45 seconds.")
            value = {
                "state": ("DEGRADED" if health and (health["current_issues"] or not health["continuous_coverage"])
                          else "SCANNING") if active else "WAITING_FOR_MARKET",
                "updated_at": completed_at.isoformat(),
                "coverage": health,
                "live_orders": False,
                "capital_per_account_inr": 50000,
                "last_tick": latest,
                "blockers": blockers,
                "experiment": {
                    "run_id": manifest["run_id"],
                    "started_at": manifest["started_at"],
                    "mode": "PAPER_COMPARISON",
                    "accounts": accounts,
                    "candidate_rules": detector_rules(),
                    "exit_quote_policy": exit_policy,
                    "exit_quote_policy_identity": expected_policy,
                    "candidate_profit_profile": paper_profit_trail.profile(config.paper_candidate_profit_giveback_pct),
                },
                "stream": inputs.stream.state if inputs else "WAITING",
                "news": inputs.news if inputs else {"headlines": 0},
            }
            atomic_json(root / "status.json", value)
            atomic_json(run_root / "status.json", value)
            stop.wait(10)
    finally:
        if runtime:
            runtime.stop("COMPARISON_STOPPED")
        if inputs:
            inputs.close()
        candidate.close()
        dual_agent_shadow.close()
        # Never leave a fresh SCANNING heartbeat after a controlled shutdown.
        status = root / "status.json"
        if status.exists():
            value = json.loads(status.read_text())
            value.update(state="STOPPED", updated_at=datetime.now(IST).isoformat())
            atomic_json(status, value)


def detector_rules():
    from app.trading.forward_zone_candidate import RULES

    return RULES
