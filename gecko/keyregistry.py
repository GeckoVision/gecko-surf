"""Gecko API-key registry + resolver — the hosted-plane access credential (Layer 1 ext.).

The hosted ``gecko login`` mints a **Gecko API key** (``gecko_sk_…``) and the paid MCP
surface verifies it on the SAME :data:`~gecko.keyauth.AccountResolver` seam Layer 1 already
uses. This module is the identity/key metadata store — the control-plane record that maps a
key to a developer account — and the resolver that reads it. It never holds a plaintext key,
a response payload, or a secret: only ``{sha256(key) -> account_id, created, enabled, label}``
(invariant #1).

Two seams keep it offline-falsifiable (Pattern B):

* :class:`KeyRegistry` — an injected Protocol; :class:`InMemoryKeyRegistry` is the test fake,
  :class:`MongoKeyRegistry` the production impl (reuses the ``MONGODB_URI`` already wired).
* :class:`GeckoKeyResolver` — an :data:`~gecko.keyauth.AccountResolver`: a presented key →
  ``sha256`` → registry lookup → the stable ``account_id`` **iff a record exists AND is
  enabled**, else ``None`` (fail-closed). It never logs, echoes, returns, or persists the key.

The key is OUR credential (256-bit CSPRNG), so a salt-free ``sha256`` digest is the lookup
value — deterministic so a presented key maps to its record, and irreversible so the store
holds nothing usable if read.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "KEY_PREFIX",
    "GeckoKeyResolver",
    "InMemoryKeyRegistry",
    "KeyRegistry",
    "KeyRegistryError",
    "MongoKeyRegistry",
    "RegistryAllowlist",
    "hash_key",
    "mint_key",
    "registry_from_env",
]

#: Every minted key carries this prefix so a resolver can cheaply reject a non-Gecko token
#: (e.g. a Privy JWT) before any hashing/lookup.
KEY_PREFIX = "gecko_sk_"

#: 43 base62 chars ≈ 256 bits of entropy (62**43 > 2**256). The CSPRNG is ``secrets``.
_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_KEY_BODY_LEN = 43

#: The Mongo db/collection the production registry uses (sibling of the events sink).
_MONGO_DB = "gecko_registry"
_MONGO_COLLECTION = "gecko_keys"
#: The events push-ssm sentinel that means "unset" — never treat it as a live URI.
_UNSET_SENTINEL = "__unset__"


class KeyRegistryError(Exception):
    """A registry-store operation failed. MUST NEVER contain a key value — the leak suite
    asserts this. Names only the operation/reason."""


def mint_key() -> str:
    """Mint a fresh Gecko secret key: ``gecko_sk_`` + 43 base62 chars (256-bit entropy).

    Returned to the caller **exactly once** (the login endpoint hands it back and stores only
    its hash); it can never be re-retrieved. Pure — no I/O, never logs the value it returns.
    """
    body = "".join(secrets.choice(_BASE62) for _ in range(_KEY_BODY_LEN))
    return f"{KEY_PREFIX}{body}"


def hash_key(key: str) -> str:
    """The ``sha256`` hex digest of a presented key — the ONLY form the registry stores or
    looks up. Salt-free (the key is a 256-bit secret, so a digest is not brute-forceable) and
    deterministic (a presented key maps to its stored record). Never logs the key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@runtime_checkable
class KeyRegistry(Protocol):
    """The injected key-metadata store (the swappable seam).

    Holds only ``{key_hash -> account_id, created, enabled, label}`` — never a plaintext key.
    ``account_for`` is the read the gate needs; the rest support key minting + founder ops.
    """

    def store_key(self, *, key_hash: str, account_id: str, label: str) -> None:
        """Persist a freshly minted key's HASH → account, ``enabled=True``, stamped ``created``."""
        ...

    def account_for(self, key_hash: str) -> str | None:
        """The account id for ``key_hash`` **iff a record exists AND is enabled**, else ``None``."""
        ...

    def set_account_enabled(self, account_id: str, enabled: bool) -> int:
        """Toggle ``enabled`` on every key for ``account_id``; return the number changed."""
        ...

    def enabled_accounts(self) -> list[str]:
        """The account ids that hold at least one enabled key (sorted) — never a key."""
        ...


@dataclass
class InMemoryKeyRegistry:
    """Offline test fake — a dict of ``key_hash -> record``. No plaintext key ever enters it.

    ``field(repr=False)`` on the store keeps a stray ``repr`` from surfacing a hash in a log.
    """

    _by_hash: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _clock: Callable[[], float] = time.time

    def store_key(self, *, key_hash: str, account_id: str, label: str) -> None:
        self._by_hash[key_hash] = {
            "account_id": account_id,
            "created": self._clock(),
            "enabled": True,
            "label": label,
        }

    def account_for(self, key_hash: str) -> str | None:
        record = self._by_hash.get(key_hash)
        if record is None or not record.get("enabled"):
            return None
        account = record.get("account_id")
        return account if isinstance(account, str) and account else None

    def set_account_enabled(self, account_id: str, enabled: bool) -> int:
        changed = 0
        for record in self._by_hash.values():
            if (
                record.get("account_id") == account_id
                and record.get("enabled") != enabled
            ):
                record["enabled"] = enabled
                changed += 1
        return changed

    def enabled_accounts(self) -> list[str]:
        return sorted(
            {
                str(r["account_id"])
                for r in self._by_hash.values()
                if r.get("enabled") and r.get("account_id")
            }
        )


@dataclass
class MongoKeyRegistry:
    """Production impl over a duck-typed Mongo collection (``_id`` = the key hash).

    Never stores a plaintext key. The collection is duck-typed so tests could run it against
    an in-memory double, but the true offline fake is :class:`InMemoryKeyRegistry`.
    """

    collection: Any
    _clock: Callable[[], float] = time.time

    def store_key(self, *, key_hash: str, account_id: str, label: str) -> None:
        self.collection.insert_one(
            {
                "_id": key_hash,
                "account_id": account_id,
                "created": self._clock(),
                "enabled": True,
                "label": label,
            }
        )

    def account_for(self, key_hash: str) -> str | None:
        doc = self.collection.find_one({"_id": key_hash, "enabled": True})
        if not isinstance(doc, dict):
            return None
        account = doc.get("account_id")
        return account if isinstance(account, str) and account else None

    def set_account_enabled(self, account_id: str, enabled: bool) -> int:
        result = self.collection.update_many(
            {"account_id": account_id}, {"$set": {"enabled": enabled}}
        )
        return int(getattr(result, "modified_count", 0))

    def enabled_accounts(self) -> list[str]:
        return sorted(
            str(a) for a in self.collection.distinct("account_id", {"enabled": True})
        )


@dataclass(frozen=True)
class GeckoKeyResolver:
    """An :data:`~gecko.keyauth.AccountResolver`: a presented ``gecko_sk_…`` key → account id.

    Callable ``(token) -> account_id | None``. A token without the ``gecko_sk_`` prefix (e.g. a
    Privy JWT) resolves to ``None`` here — this resolver owns ONLY registry keys. Otherwise it
    hashes the key and returns the account id **iff a record exists AND is enabled** (the
    registry enforces both), else ``None`` (fail-closed). The key is NEVER logged or echoed; a
    registry error is swallowed to a deny, never surfaced with the key attached.
    """

    registry: KeyRegistry

    def __call__(self, token: str) -> str | None:
        if not token or not token.startswith(KEY_PREFIX):
            return None
        try:
            return self.registry.account_for(hash_key(token))
        except Exception:  # noqa: BLE001 - a store error must fail closed, never leak the key
            logger.warning("gecko key registry lookup failed (redacted)")
            return None


@dataclass
class RegistryAllowlist:
    """A registry-backed :class:`~gecko.keyauth.Allowlist` for the HOSTED plane.

    ``enabled`` lives on the registry record (design), so this adapter mirrors
    :class:`~gecko.keyauth.FileAllowlist`'s enable/disable/accounts contract onto the registry —
    ``gecko keys`` toggles the hosted store while FileAllowlist stays for local/dev. Default-deny:
    an account with no enabled key is not enabled. Holds only non-secret account ids.
    """

    registry: KeyRegistry

    def is_enabled(self, account: str) -> bool:
        return bool(account) and account in set(self.registry.enabled_accounts())

    def enable(self, account: str) -> bool:
        """Re-enable every key for ``account``; ``True`` if anything changed."""
        return self.registry.set_account_enabled(_require_account(account), True) > 0

    def disable(self, account: str) -> bool:
        """Revoke every key for ``account``; ``True`` if anything changed (idempotent)."""
        return self.registry.set_account_enabled(_require_account(account), False) > 0

    def accounts(self) -> list[str]:
        return self.registry.enabled_accounts()


def _require_account(account: str) -> str:
    account = (account or "").strip()
    if not account:
        raise KeyRegistryError("account id must be a non-empty identifier")
    return account


def registry_from_env(env: dict[str, str] | None = None) -> KeyRegistry | None:
    """Build the Mongo-backed registry from ``MONGODB_URI``, or ``None`` when unconfigured.

    Fails SOFT (like ``registry.wiring``): a missing/sentinel URI or an import/connect error
    yields ``None`` so the gate stays fail-closed (``deny_all``) rather than crash the server.
    Never logs the URI.
    """
    source = os.environ if env is None else env
    uri = (source.get("MONGODB_URI") or "").strip()
    if not uri or uri == _UNSET_SENTINEL:
        return None
    try:
        from pymongo import MongoClient

        db: Any = MongoClient(uri, serverSelectionTimeoutMS=2000)[_MONGO_DB]
        return MongoKeyRegistry(collection=db[_MONGO_COLLECTION])
    except Exception:  # noqa: BLE001 - the registry must never take the server down
        logger.warning("gecko key registry init failed (redacted)")
        return None
