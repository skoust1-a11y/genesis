"""What must hold before a deployment trades with real money.

Checked at approval (shown as preview blockers) and again when the Trade Crew starts:
LIVE_TRADING_ENABLED, a broker registered for live with every credential resolving, a
defined-risk structure listed in LIVE_ALLOWED_STRUCTURES and, at approval, the paper track
record LIVE_MIN_PAPER_DAYS asks for. The crew additionally needs a SEBI algo id and the
broker feed.
"""

from __future__ import annotations

from app.brokers import credentials, registry
from app.config import Settings
from app.db.models import BrokerAccount
from app.live.store import LiveStore
from app.market.feed import IST


def account_blockers(store: LiveStore | None, account: BrokerAccount, settings: Settings) -> list[str]:
    """The broker is registered for the account's mode and its required credentials resolve."""
    try:
        spec = registry.check_account(account.broker, account.mode, settings)
    except ValueError as exc:
        return [str(exc)]
    fields = credentials.status(store, account, spec, settings)
    missing = [f.name for f in fields if f.required and f.source is None]
    if missing:
        return [f"broker {spec.name!r} is not configured: missing {', '.join(missing)}"]
    return []


def structure_blocker(structure: str | None, settings: Settings) -> str | None:
    allowed = settings.allowed_live_structures
    if structure not in allowed:
        return (
            f"structure {structure!r} is not allowed live (LIVE_ALLOWED_STRUCTURES={','.join(sorted(allowed)) or '-'}); "
            "only defined-risk structures by default"
        )
    return None


def live_blockers(store: LiveStore | None, account: BrokerAccount | None, structure: str | None,
                  settings: Settings) -> list[str]:  # fmt: skip
    out = []
    if not settings.live_trading_enabled:
        out.append("LIVE_TRADING_ENABLED is not true")
    if account is not None:
        if account.mode != "live":
            out.append(f"broker account {account.id} is a {account.mode} account, not live")
        else:
            out += account_blockers(store, account, settings)
    blocker = structure_blocker(structure, settings)
    if blocker:
        out.append(blocker)
    return out


def paper_days(store: LiveStore, strategy_version_id: int) -> int:
    """Distinct IST trading days the Trade Crew ran on the broker feed for paper deployments of this
    strategy version that were never halted."""
    clean = []
    for d in store.deployments():
        if d.mode != "paper" or d.strategy_version_id != strategy_version_id:
            continue
        if any(e.event == "halted" for e in store.deployment_events(d.id)):
            continue
        clean.append(d.id)
    if not clean:
        return 0
    return len({hb.at.astimezone(IST).date() for hb in store.heartbeats(clean, feed="broker")})


def paper_record_blocker(store: LiveStore, strategy_version_id: int, settings: Settings) -> str | None:
    needed = settings.live_min_paper_days
    if not needed:
        return None
    days = paper_days(store, strategy_version_id)
    if days < needed:
        return (
            f"LIVE_MIN_PAPER_DAYS={needed}: this version has {days} trading day(s) of paper trading on the broker "
            "feed without a halt"
        )
    return None
