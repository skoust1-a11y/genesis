"""uv run python -m app.suggest [--strategy ID]

The trade each active strategy's rules would open on the latest market day in the imported data,
sent to the alert channels (Telegram and/or email) and printed. Doubles as a check that alerts reach
you. Prices are end-of-day closes, so it is a plan for the next session, not a live quote; risk and
portfolio limits of a deployment are not applied.
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from app import alerts
from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.engine.sim import dte_window
from app.market.calendar import event_lookup
from app.market.source import open_market_source
from app.strategies.rules import check_entry
from app.strategies.schema import StrategyDocument, parse_strategy_markdown
from app.strategies.store import StrategyStore
from app.trading.decisions import build_entry_proposal, describe_check, proposal_lots


def suggestion(doc: StrategyDocument, settings: Settings) -> str:
    rules = doc.rules
    if rules is None:
        return f"{doc.name}: no valid rules block ({'; '.join(doc.rules_errors)})"
    underlying = rules.instrument.underlying
    source = open_market_source(underlying, settings, event_lookup(settings))
    try:
        dates = source.dates()
        market = source.feed(dates[-1:], dte_window(rules)).snapshot(underlying) if dates else None
    finally:
        source.close()
    if market is None:
        return f"{doc.name}: no {underlying} market data to check"

    checks = check_entry(rules, market)
    iv_rank = "n/a" if market.iv_rank is None else f"{market.iv_rank:g}"
    lines = [
        f"{doc.name} ({underlying} {rules.instrument.structure}), data of {market.as_of} [{source.data_source}]",
        f"spot {market.spot:g}, IV rank {iv_rank}, expiry {market.expiry} ({market.dte} DTE)",
        *(f"{'ok  ' if c.passed else 'FAIL'} {describe_check(c)}" for c in checks),
    ]
    if not all(c.passed for c in checks):
        return "\n".join([*lines, "=> no trade: entry conditions not met"])
    try:
        proposal = build_entry_proposal(rules, market, settings, proposal_lots(rules, None))
    except ValueError as exc:
        return "\n".join([*lines, f"=> entry signal, but no trade can be sized: {exc}"])
    lines.append(f"=> SUGGESTED: {proposal['lots']} lot(s) x {proposal['lot_size']}")
    for leg in proposal["legs"]:
        price = "no quote" if leg["quote"] is None else f"@ {leg['quote']:g}"
        lines.append(f"  {leg['side']} {leg['tradingsymbol']} x{leg['quantity']} {price}{' (illiquid)' if leg['illiquid'] else ''}")
    if econ := proposal["economics"]:
        lines.append(
            f"  premium {econ.credit:+,.0f}, target {econ.target_pnl:+,.0f}, stop {econ.stop_loss_pnl:+,.0f}, "
            f"max loss {econ.max_loss:,.0f}, R:R {econ.risk_reward:.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.suggest")
    parser.add_argument("--strategy", help="one strategy id (default: every strategy that is not archived)")
    args = parser.parse_args(argv)
    settings = get_settings()

    session = get_sessionmaker()()
    try:
        store = StrategyStore(session)
        strategies = [store.get_strategy(args.strategy)] if args.strategy else store.list()
        docs = [
            parse_strategy_markdown(store.latest_version(s.id).markdown) for s in strategies if s.archived_at is None
        ]
    except FileNotFoundError:
        parser.exit(2, f"error: no strategy {args.strategy}\n")
    finally:
        session.close()
    if not docs:
        parser.exit(1, "no active strategies\n")

    body = "\n\n".join(suggestion(doc, settings) for doc in docs)
    print(body)
    sent = alerts.send(settings, "[genesis] suggested trades", body)
    if sent:
        print(f"\nsent via {', '.join(sent)}")
    elif alerts.channels(settings):
        print("\nnot sent: delivery failed (see the error above)")
    else:
        print("\nnot sent: set ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID in .env")
    return 0 if sent else 1


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())
