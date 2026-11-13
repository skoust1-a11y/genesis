"""Opt-in, lexical preflight. Imports no runtime settings and performs no I/O."""

import re
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from pathlib import PurePosixPath


class Environment(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class Resource(str, Enum):
    RISK_CONFIG = "risk_config"
    SAFETY_WALL = "safety_wall"
    EXECUTION_JOURNAL = "execution_journal"
    PREMARKET_PLAN = "premarket_plan"
    RESEARCH_DATABASE = "research_database"
    PAPER_DATABASE = "paper_database"
    MARKET_DATABASE = "market_database"
    LEGACY_TRADE_DATABASE = "legacy_trade_database"
    ODDS_DATABASE = "odds_database"
    CREW_RESEARCH_JOURNAL = "crew_research_journal"
    STRATEGY_CONFIG = "strategy_config"
    REFERENCE_DATA = "reference_data"
    LOGS = "logs"
    OPERATIONAL_EXPORTS = "operational_exports"


DATABASES = frozenset(r for r in Resource if r.value.endswith("database"))


@dataclass(frozen=True)
class Authority:
    resource: Resource
    location: str  # Absolute POSIX path, or sqlite:////absolute/path for databases.


@dataclass(frozen=True)
class Deployment:
    deployment_id: str
    account_id: str
    environment: Environment
    permitted_root: str
    authorities: tuple[Authority, ...]


@dataclass(frozen=True)
class StateAuthorityContract:
    deployments: tuple[Deployment, ...]


class AuthorityError(ValueError):
    """The supplied contract cannot safely assign state ownership."""


def _path(value: str) -> PurePosixPath:
    # Reject ambiguous spellings rather than silently canonicalizing configuration.
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or any(c in value for c in "\\%?#:$~")
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or any(p in ("", ".", "..") for p in value[1:].split("/"))
    ):
        raise AuthorityError("Expected an unambiguous absolute POSIX path")
    return PurePosixPath(value)


def _location(authority: Authority) -> PurePosixPath:
    value = authority.location
    if not isinstance(value, str):
        raise AuthorityError("Location must be a string")
    if "://" in value:
        if authority.resource not in DATABASES or not value.startswith("sqlite:////"):
            raise AuthorityError(
                "Only absolute sqlite://// URLs are permitted for databases"
            )
        value = value[len("sqlite:///") :]
    return _path(value)


def _overlaps(a: PurePosixPath, b: PurePosixPath) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def preflight(contract: StateAuthorityContract) -> None:
    """Raise AuthorityError on invalid declarations; never read or alter any resource.

    Validate the entire deployment set together. All resource ownership is exclusive,
    including reference data and market data; no implicit sharing exemptions exist.
    Success says nothing about file existence, symlinks, permissions or runtime safety.
    """
    if not isinstance(contract, StateAuthorityContract) or not contract.deployments:
        raise AuthorityError("At least one explicit deployment is required")
    identities: set[str] = set()
    accounts: dict[str, Environment] = {}
    roots: list[PurePosixPath] = []
    paths: list[PurePosixPath] = []
    for deployment in contract.deployments:
        if not isinstance(deployment, Deployment):
            raise AuthorityError("Expected a Deployment")
        for identity in (deployment.deployment_id, deployment.account_id):
            if not isinstance(identity, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", identity
            ):
                raise AuthorityError(
                    "Explicit deployment and account identifiers are required"
                )
        if not isinstance(deployment.environment, Environment):
            raise AuthorityError(
                "Expected an explicit research, paper or live environment"
            )
        if deployment.deployment_id in identities:
            raise AuthorityError("Duplicate or conflicting deployment identity")
        identities.add(deployment.deployment_id)
        previous = accounts.setdefault(deployment.account_id, deployment.environment)
        if previous != deployment.environment:
            raise AuthorityError("Account identity is shared across environments")
        root = _path(deployment.permitted_root)
        if any(_overlaps(root, other) for other in roots):
            raise AuthorityError("Deployment boundaries overlap")
        roots.append(root)
        assigned: set[Resource] = set()
        for authority in deployment.authorities:
            if not isinstance(authority, Authority) or not isinstance(
                authority.resource, Resource
            ):
                raise AuthorityError("Unknown resource authority")
            if authority.resource in assigned:
                raise AuthorityError("Duplicate resource assignment")
            assigned.add(authority.resource)
            path = _location(authority)
            if path == root or not path.is_relative_to(root):
                raise AuthorityError(
                    "Resource must be strictly inside its deployment boundary"
                )
            paths.append(path)
        if assigned != set(Resource):
            raise AuthorityError("Missing required state authority")
    if any(_overlaps(a, b) for a, b in combinations(paths, 2)):
        raise AuthorityError("Duplicate or overlapping resource ownership")
