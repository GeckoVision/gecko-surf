"""Reader keys: a key that reads ONE dataset of its owner, and nothing else.

Why a record of its own. A grant on the key registry is per ACCOUNT:
``RegistryAllowlist.may_access`` joins the surfaces of every key of an account, and
``set_account_surfaces`` rewrites all of them. A read key stored there under the owner's
account would gain the paying ``anyquery`` tools on the owner's next grant. So a reader
key lives in its own collection, carries its own prefix (``gecko_rk_``, which
``GeckoKeyResolver`` rejects before any lookup), and resolves to a
:class:`DatasetAccess`, never to an account id. No gate that admits an account can admit
it.

What the record holds: ``sha256(key) -> owner, dataset_id, key_id, label, created,
enabled``. No plaintext key and no reveal switch: the founder ruled (12 Oct 2026) that
only the owner's own account key may see contact details whole or download the unmasked
Parquet. ``key_id`` is the first 16 hex of the hash, a non-secret handle to list and
revoke by.

No MCP tool mints a key. A key in a tool result is a secret in a transcript. Minting is
:func:`mint_reader_key`, called from the owner-run ``scripts/dataset_reader_keys.py``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..keyauth import AccountResolver
from ..keyregistry import hash_key, mint_key
from .bundles import BUNDLE_NAME_RE
from .pii import masked
from .store import Store

logger = logging.getLogger(__name__)

__all__ = [
    "READER_COLLECTION",
    "READER_KEY_PREFIX",
    "DatasetAccess",
    "DatasetRole",
    "InMemoryReaderKeyRegistry",
    "MongoReaderKeyRegistry",
    "ReaderGrant",
    "ReaderKeyError",
    "ReaderKeyRegistry",
    "mint_reader_key",
    "resolve_dataset_access",
    "revoke_reader_key",
    "shown_value",
]

READER_KEY_PREFIX = "gecko_rk_"
#: Sibling of ``gecko_keys`` in the registry database; never the customer's own database,
#: because a key is resolved before we know whose database to open.
READER_COLLECTION = "gecko_reader_keys"
_KEY_ID_LEN = 16

DatasetRole = Literal["owner", "reader"]


class ReaderKeyError(Exception):
    """Minting or revoking refused. Never contains a key."""


@dataclass(frozen=True)
class ReaderGrant:
    """One reader key's record, as read back. Holds no key and no hash beyond ``key_id``."""

    key_id: str
    owner: str
    dataset_id: str
    label: str
    created: float
    enabled: bool


@dataclass(frozen=True)
class DatasetAccess:
    """Who a presented key reads as. ``dataset_id`` is None for the owner (all of theirs)."""

    customer: str
    role: DatasetRole
    dataset_id: str | None

    def may_read(self, customer: str, dataset_id: str) -> bool:
        if not customer or customer != self.customer:
            return False
        return self.dataset_id is None or self.dataset_id == dataset_id

    @property
    def may_reveal_pii(self) -> bool:
        """Only the owner's own key: contact details whole, the unmasked Parquet."""
        return self.role == "owner"


@runtime_checkable
class ReaderKeyRegistry(Protocol):
    def store_reader(
        self, *, key_hash: str, owner: str, dataset_id: str, label: str
    ) -> ReaderGrant: ...

    def reader_for(self, key_hash: str) -> ReaderGrant | None:
        """The ENABLED record for ``key_hash``, else None."""
        ...

    def readers_for(self, owner: str) -> list[ReaderGrant]:
        """Every record of ``owner``, enabled or not, oldest first."""
        ...

    def disable_reader(self, owner: str, key_id: str) -> bool:
        """Disable ``owner``'s key ``key_id``; True if it was enabled."""
        ...


def _grant(doc: dict[str, Any]) -> ReaderGrant:
    return ReaderGrant(
        key_id=str(doc["key_id"]),
        owner=str(doc["owner"]),
        dataset_id=str(doc["dataset_id"]),
        label=str(doc.get("label", "")),
        created=float(doc["created"]),
        enabled=bool(doc.get("enabled")),
    )


def _record(
    key_hash: str, owner: str, dataset_id: str, label: str, created: float
) -> dict[str, Any]:
    return {
        "_id": key_hash,
        "key_id": key_hash[:_KEY_ID_LEN],
        "owner": owner,
        "dataset_id": dataset_id,
        "label": label,
        "created": created,
        "enabled": True,
    }


@dataclass
class InMemoryReaderKeyRegistry:
    _by_hash: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _clock: Callable[[], float] = time.time

    def store_reader(
        self, *, key_hash: str, owner: str, dataset_id: str, label: str
    ) -> ReaderGrant:
        if key_hash in self._by_hash:
            raise ReaderKeyError("a reader key with this hash already exists")
        doc = _record(key_hash, owner, dataset_id, label, self._clock())
        self._by_hash[key_hash] = doc
        return _grant(doc)

    def reader_for(self, key_hash: str) -> ReaderGrant | None:
        doc = self._by_hash.get(key_hash)
        return _grant(doc) if doc is not None and doc.get("enabled") else None

    def readers_for(self, owner: str) -> list[ReaderGrant]:
        docs = [d for d in self._by_hash.values() if d.get("owner") == owner]
        return [
            _grant(d) for d in sorted(docs, key=lambda d: (d["created"], d["key_id"]))
        ]

    def disable_reader(self, owner: str, key_id: str) -> bool:
        for doc in self._by_hash.values():
            if doc["owner"] == owner and doc["key_id"] == key_id and doc["enabled"]:
                doc["enabled"] = False
                return True
        return False


@dataclass
class MongoReaderKeyRegistry:
    """Over a pymongo-shaped collection, ``_id`` = the key hash."""

    collection: Any = field(repr=False)
    _clock: Callable[[], float] = time.time

    def store_reader(
        self, *, key_hash: str, owner: str, dataset_id: str, label: str
    ) -> ReaderGrant:
        doc = _record(key_hash, owner, dataset_id, label, self._clock())
        self.collection.insert_one(doc)
        return _grant(doc)

    def reader_for(self, key_hash: str) -> ReaderGrant | None:
        doc = self.collection.find_one({"_id": key_hash, "enabled": True})
        return _grant(doc) if isinstance(doc, dict) else None

    def readers_for(self, owner: str) -> list[ReaderGrant]:
        docs = [
            d for d in self.collection.find({"owner": owner}) if isinstance(d, dict)
        ]
        return [
            _grant(d) for d in sorted(docs, key=lambda d: (d["created"], d["key_id"]))
        ]

    def disable_reader(self, owner: str, key_id: str) -> bool:
        result = self.collection.update_one(
            {"owner": owner, "key_id": key_id, "enabled": True},
            {"$set": {"enabled": False}},
        )
        return int(getattr(result, "matched_count", 0)) > 0


def mint_reader_key(
    registry: ReaderKeyRegistry,
    store: Store,
    *,
    owner: str,
    dataset_id: str,
    label: str,
) -> tuple[str, ReaderGrant]:
    """Mint a reader key on ``owner``'s dataset ``dataset_id``. The key is returned once.

    The caller must already have proved it IS ``owner`` (the script resolves the owner
    from the owner's own account key). Refuses a dataset the owner does not hold.
    """
    owner = (owner or "").strip()
    if not owner:
        raise ReaderKeyError("the owner account is empty")
    if not BUNDLE_NAME_RE.match(dataset_id or ""):
        raise ReaderKeyError(
            "a dataset name is 1-40 lowercase letters, digits or dashes"
        )
    if store.dataset(owner, dataset_id) is None:
        raise ReaderKeyError(f"this account has no dataset {dataset_id!r}")
    key = mint_key(READER_KEY_PREFIX)
    grant = registry.store_reader(
        key_hash=hash_key(key), owner=owner, dataset_id=dataset_id, label=label
    )
    return key, grant


def revoke_reader_key(registry: ReaderKeyRegistry, *, owner: str, key_id: str) -> bool:
    """Disable one of ``owner``'s reader keys. Another owner's key_id changes nothing."""
    if not owner or not key_id:
        return False
    return registry.disable_reader(owner, key_id)


def resolve_dataset_access(
    token: str,
    *,
    readers: ReaderKeyRegistry,
    resolve_owner: AccountResolver,
    owner_enabled: Callable[[str], bool],
) -> DatasetAccess | None:
    """A presented key -> what it may read, or None. Fail-closed on any store error.

    A reader key whose owner account is disabled is refused: disabling an account must
    close every door it opened. An owner key needs no surface grant to read its own data.
    """
    if not token:
        return None
    try:
        if token.startswith(READER_KEY_PREFIX):
            grant = readers.reader_for(hash_key(token))
            if grant is None or not owner_enabled(grant.owner):
                return None
            return DatasetAccess(grant.owner, "reader", grant.dataset_id)
        account = resolve_owner(token)
    except Exception:  # noqa: BLE001 - a store error must deny, never leak the key
        logger.warning("dataset key lookup failed (redacted)")
        return None
    return DatasetAccess(account, "owner", None) if account else None


def shown_value(
    access: DatasetAccess, field_name: str, value: str | None, *, reveal: bool
) -> tuple[str | None, bool]:
    """The one masking rule for a dataset view: the ask AND the key. Masked by default."""
    return masked(field_name, value, reveal=reveal and access.may_reveal_pii)
