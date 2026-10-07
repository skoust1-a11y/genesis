"""Broker credentials: encrypted in the live database first, `.env` second.

Each field (e.g. `api_key`, `access_token`) is looked up in `broker_credentials` for the
account: the latest row for the field wins, and a revoked row removes it. A field with no
usable row comes from `BROKER_<BROKER>_<FIELD>` in the environment or `.env`. Values are
encrypted with Fernet under `BROKER_CREDENTIALS_KEY`; without the key nothing can be saved
and stored values cannot be read, so lookups fall back to `.env`. Secrets are never returned
by the API or written to the audit log, only a short sha256 fingerprint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from app.config import Settings
from app.db.hashing import sha256_hex
from app.db.models import BrokerAccount
from app.live.store import LiveStore

if TYPE_CHECKING:
    from app.brokers.registry import BrokerSpec

MAX_VALUE_LENGTH = 4096


@dataclass
class FieldStatus:
    name: str
    label: str
    secret: bool
    required: bool
    source: str | None  # "db" | "env" | None
    fingerprint: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def fingerprint(value: str) -> str:
    return sha256_hex(value)[:12]


def env_name(broker: str, field: str) -> str:
    return f"BROKER_{broker}_{field}".upper()


@lru_cache
def _dotenv(path: str) -> dict[str, str | None]:
    return dotenv_values(path) if Path(path).exists() else {}


def env_value(broker: str, field: str) -> str | None:
    name = env_name(broker, field)
    return os.environ.get(name) or _dotenv(".env").get(name) or None


def _fernet(settings: Settings):
    if not settings.broker_credentials_key:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(settings.broker_credentials_key.encode())
    except ValueError as exc:
        raise ValueError("BROKER_CREDENTIALS_KEY is not a valid Fernet key") from exc


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _decrypt(fernet, ciphertext: str) -> str | None:
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


def resolve(store: LiveStore, account: BrokerAccount, spec: BrokerSpec, settings: Settings) -> dict[str, str]:
    """Every credential field that has a value, from the DB first and `.env` second."""
    rows = store.broker_credentials(account.id)
    fernet = _fernet(settings)
    out = {}
    for f in spec.credential_fields:
        row = rows.get(f.name)
        value = None
        if row is not None and not row.revoked and row.ciphertext and fernet is not None:
            value = _decrypt(fernet, row.ciphertext)
        value = value or env_value(spec.name, f.name)
        if value:
            out[f.name] = value
    return out


def missing_fields(spec: BrokerSpec, values: dict[str, str]) -> list[str]:
    return [f.name for f in spec.credential_fields if f.required and not values.get(f.name)]


def status(
    store: LiveStore | None, account: BrokerAccount | None, spec: BrokerSpec, settings: Settings
) -> list[FieldStatus]:
    """Where each field would come from, without values. With no account, only `.env` is considered."""
    rows = store.broker_credentials(account.id) if store and account else {}
    fernet = _fernet(settings)
    out = []
    for f in spec.credential_fields:
        row = rows.get(f.name)
        usable = row is not None and not row.revoked and row.ciphertext and fernet is not None
        if usable and _decrypt(fernet, row.ciphertext) is not None:
            out.append(FieldStatus(f.name, f.label, f.secret, f.required, "db", row.fingerprint,
                                   row.created_at.isoformat()))  # fmt: skip
        elif env_value(spec.name, f.name):
            out.append(FieldStatus(f.name, f.label, f.secret, f.required, "env",
                                   fingerprint(env_value(spec.name, f.name) or "")))  # fmt: skip
        else:
            out.append(FieldStatus(f.name, f.label, f.secret, f.required, None))
    return out


def configured(fields: list[FieldStatus]) -> bool:
    return all(f.source is not None for f in fields if f.required)


def save(store: LiveStore, account: BrokerAccount, spec: BrokerSpec, settings: Settings, values: dict[str, str]):
    """Encrypt and store new values. Each field is a new row; the previous one stays, superseded."""
    fernet = _fernet(settings)
    if fernet is None:
        raise ValueError("set BROKER_CREDENTIALS_KEY to store credentials in the database (or use .env)")
    known = {f.name for f in spec.credential_fields}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown credential fields for {spec.name}: {', '.join(sorted(unknown))}")
    if not values:
        raise ValueError("no credential values given")
    rows = []
    for name, value in values.items():
        value = (value or "").strip()
        if not value:
            raise ValueError(f"{name} is empty; use DELETE to remove a stored value")
        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(f"{name} is too long")
        rows.append(
            store.add_broker_credential(
                broker_account_id=account.id,
                field=name,
                ciphertext=fernet.encrypt(value.encode()).decode(),
                fingerprint=fingerprint(value),
                revoked=False,
            )
        )
    return rows


def revoke(store: LiveStore, account: BrokerAccount, spec: BrokerSpec, fields: list[str] | None = None):
    """Revoke stored values (all fields when `fields` is empty); lookups then fall back to `.env`."""
    known = [f.name for f in spec.credential_fields]
    names = fields or [n for n, row in store.broker_credentials(account.id).items() if not row.revoked]
    unknown = set(names) - set(known)
    if unknown:
        raise ValueError(f"unknown credential fields for {spec.name}: {', '.join(sorted(unknown))}")
    return [
        store.add_broker_credential(broker_account_id=account.id, field=name, ciphertext=None, revoked=True)
        for name in names
    ]
