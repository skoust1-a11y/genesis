"""Brokers a user can pick, and how a deployment gets its adapter and market feed.

Each broker is a `BrokerSpec`: its name, the modes it supports, whether it provides quotes
(so it can drive `BrokerFeed`), the credential fields it needs, and a factory. Adding a real
broker means writing one adapter module and calling `register` for it here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.brokers import credentials
from app.brokers.base import BrokerAdapter, LiveBrokerAdapter, QuoteSource
from app.brokers.paper import PaperBroker
from app.brokers.sandbox import SandboxBroker
from app.config import Settings
from app.db.models import BrokerAccount
from app.engine.costs import CostProfile
from app.live.store import LiveStore
from app.market.feed import BrokerFeed, EventLookup, MarketFeed, ReplayFeed

FEEDS = ("replay", "broker")


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = True
    required: bool = True


@dataclass(frozen=True)
class BrokerSpec:
    name: str
    label: str
    has_quotes: bool
    factory: Callable[..., BrokerAdapter]  # (credentials, settings, profile, clock, account) -> adapter
    modes: Callable[[Settings], tuple[str, ...]]
    credential_fields: tuple[CredentialField, ...] = field(default_factory=tuple)
    description: str = ""

    def as_dict(self, settings: Settings) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "modes": list(self.modes(settings)),
            "has_quotes": self.has_quotes,
            "credential_fields": [f.__dict__.copy() for f in self.credential_fields],
        }


_REGISTRY: dict[str, BrokerSpec] = {}


def register(spec: BrokerSpec) -> BrokerSpec:
    _REGISTRY[spec.name] = spec
    return spec


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def specs() -> list[BrokerSpec]:
    return list(_REGISTRY.values())


def get_spec(name: str) -> BrokerSpec:
    spec = _REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"no adapter is registered for broker {name!r} (available: {known})")
    return spec


def check_account(broker: str, mode: str, settings: Settings) -> BrokerSpec:
    """The broker's spec, if it is registered and supports `mode`."""
    spec = get_spec(broker)
    if mode not in spec.modes(settings):
        raise ValueError(f"broker {broker!r} does not support {mode} accounts (supports: {', '.join(spec.modes(settings))})")
    return spec


def get_adapter(
    store: LiveStore,
    account: BrokerAccount,
    settings: Settings,
    profile: CostProfile,
    clock: Callable[[], datetime],
) -> BrokerAdapter:
    spec = check_account(account.broker, account.mode, settings)
    values = credentials.resolve(store, account, spec, settings)
    missing = credentials.missing_fields(spec, values)
    if missing:
        raise ValueError(
            f"broker {spec.name!r} is not configured: missing {', '.join(missing)} "
            f"(save them for the account or set {', '.join(credentials.env_name(spec.name, m) for m in missing)})"
        )
    adapter = spec.factory(values, settings, profile, clock, account)
    if adapter.mode != account.mode:
        raise ValueError(f"adapter mode {adapter.mode!r} does not match account mode {account.mode!r}")
    if adapter.mode == "live" and not isinstance(adapter, LiveBrokerAdapter):
        raise ValueError(f"broker {spec.name!r} lacks get_order/margin/reconciliation, which live trading needs")
    return adapter


def get_feed(
    kind: str,
    adapter: BrokerAdapter,
    underlying: str,
    settings: Settings,
    clock: Callable[[], datetime],
    events: EventLookup | None,
    dte_window: tuple[int, int] | None = None,
) -> MarketFeed:
    if kind == "replay":
        return ReplayFeed(underlying, strike_step=settings.strike_step(underlying) or 50, clock=clock, events=events)
    if kind == "broker":
        if not isinstance(adapter, QuoteSource):
            raise ValueError(f"broker {adapter.name!r} provides no quotes; use --feed replay")
        return BrokerFeed(
            adapter, settings, clock=clock, events=events, iv_rank=getattr(adapter, "iv_rank", None),
            dte_window=dte_window,
        )  # fmt: skip
    raise ValueError(f"feed must be one of {', '.join(FEEDS)}")


def _paper(values, settings, profile, clock, account) -> BrokerAdapter:
    return PaperBroker(profile, clock=clock)


def _sandbox(values, settings: Settings, profile, clock, account) -> BrokerAdapter:
    state = Path(settings.sandbox_state_dir or Path(settings.data_dir) / "sandbox") / f"{account.id}.json"
    return SandboxBroker.from_sample(mode=account.mode, profile=profile, clock=clock, state_path=state, settings=settings)


register(
    BrokerSpec(
        name="paper",
        label="Paper",
        description="Fills every LIMIT order immediately at its price plus slippage. Use with the replay feed.",
        has_quotes=False,
        factory=_paper,
        modes=lambda settings: ("paper",),
    )
)
register(
    BrokerSpec(
        name="sandbox",
        label="Sandbox",
        description="A simulated broker with quotes, asynchronous and partial fills, rejections, margin and "
        "stop orders. Live mode only with SANDBOX_ALLOW_LIVE=true.",
        has_quotes=True,
        factory=_sandbox,
        modes=lambda settings: ("paper", "live") if settings.sandbox_allow_live else ("paper",),
        credential_fields=(CredentialField("api_key", "API key (optional, unused)", secret=True, required=False),),
    )
)
