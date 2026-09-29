"""Storage for class-wallet registrations: ``(account, cohort) -> public address``.

Two collections, both control plane:

* ``class_wallets`` — ``{account, cohort, address, challenge_nonce_used, verified_at}``,
  one document per ``(account, cohort)``. The account id is our own opaque identifier and
  the address is a public Solana key: who the founder funds. No email, no key, no
  signature — the signature is checked and dropped, it proves control once and has no
  further use.
* ``class_wallet_challenges`` — outstanding challenges, keyed by the SHA-256 of the
  exact challenge text. Consuming one DELETES it, which is what makes it single-use.
  A TTL index on ``expires_at`` only sweeps the ones nobody redeemed; expiry itself is
  enforced in logic by :class:`gecko.registry.class_wallets.ClassWalletRegistrar`.

Collections are duck-typed like :class:`gecko.registry.keys.KeyStore`, so the Mongo
implementation is exercised offline against a dict-backed fake.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger("gecko.registry")

__all__ = [
    "ClassWallet",
    "ClassWalletStore",
    "IssuedChallenge",
    "InMemoryClassWalletStore",
    "MongoClassWalletStore",
    "challenge_id",
]


@dataclass(frozen=True)
class IssuedChallenge:
    """An outstanding challenge, as the store holds it until it is redeemed."""

    account: str
    cohort: str
    nonce: str
    challenge: str
    expires_at: datetime


@dataclass(frozen=True)
class ClassWallet:
    """One registration. Public by construction: nothing here is a secret."""

    account: str
    cohort: str
    address: str
    challenge_nonce_used: str
    verified_at: datetime


def challenge_id(challenge: str) -> str:
    """The lookup id for a challenge: exact-text match, no parsing of what came back."""
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


class ClassWalletStore(Protocol):
    def issue(self, challenge: IssuedChallenge) -> None: ...

    def consume(
        self, *, account: str, cohort: str, challenge: str
    ) -> IssuedChallenge | None:
        """Atomically remove and return the challenge iff it was issued to ``account``
        for ``cohort`` with exactly this text. A challenge for another account is left
        untouched, so a stranger cannot burn someone else's."""
        ...

    def upsert(self, wallet: ClassWallet) -> bool:
        """Store the one address for ``(account, cohort)``; True if one was replaced."""
        ...

    def list_wallets(self, cohort: str) -> list[ClassWallet]: ...

    def delete_wallet(self, account: str, cohort: str) -> bool: ...


@dataclass
class InMemoryClassWalletStore:
    """The offline fake. Same semantics as the Mongo store."""

    _challenges: dict[str, IssuedChallenge] = field(default_factory=dict)
    _wallets: dict[tuple[str, str], ClassWallet] = field(default_factory=dict)

    def issue(self, challenge: IssuedChallenge) -> None:
        self._challenges[challenge_id(challenge.challenge)] = challenge

    def consume(
        self, *, account: str, cohort: str, challenge: str
    ) -> IssuedChallenge | None:
        key = challenge_id(challenge)
        held = self._challenges.get(key)
        if held is None or held.account != account or held.cohort != cohort:
            return None
        return self._challenges.pop(key)

    def upsert(self, wallet: ClassWallet) -> bool:
        key = (wallet.account, wallet.cohort)
        replaced = key in self._wallets
        self._wallets[key] = wallet
        return replaced

    def list_wallets(self, cohort: str) -> list[ClassWallet]:
        return sorted(
            (w for w in self._wallets.values() if w.cohort == cohort),
            key=lambda w: w.account,
        )

    def delete_wallet(self, account: str, cohort: str) -> bool:
        return self._wallets.pop((account, cohort), None) is not None


def _aware(value: Any) -> datetime:
    # pymongo returns naive UTC datetimes unless the client is tz_aware.
    if not isinstance(value, datetime):
        raise TypeError("expected a datetime")
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class MongoClassWalletStore:
    """Production store over two duck-typed Mongo collections."""

    wallets: Any
    challenges: Any
    _indexed: bool = field(default=False, repr=False)

    def _ensure_indexes(self) -> None:
        # Lazy and best-effort: an eager create_index would block server start on a slow
        # Mongo (see registry.wiring). The unique index backs one-address-per-cohort;
        # the TTL index only garbage-collects challenges nobody redeemed.
        if self._indexed:
            return
        try:
            self.challenges.create_index("expires_at", expireAfterSeconds=0)
            self.wallets.create_index(
                [("account", 1), ("cohort", 1)], unique=True, name="account_cohort"
            )
            self._indexed = True
        except Exception:  # noqa: BLE001 - an index is housekeeping, never a refusal
            logger.warning("class wallet index creation failed (redacted)")

    def issue(self, challenge: IssuedChallenge) -> None:
        self._ensure_indexes()
        self.challenges.insert_one(
            {
                "_id": challenge_id(challenge.challenge),
                "account": challenge.account,
                "cohort": challenge.cohort,
                "nonce": challenge.nonce,
                "challenge": challenge.challenge,
                "expires_at": challenge.expires_at,
            }
        )

    def consume(
        self, *, account: str, cohort: str, challenge: str
    ) -> IssuedChallenge | None:
        doc = self.challenges.find_one_and_delete(
            {"_id": challenge_id(challenge), "account": account, "cohort": cohort}
        )
        if not isinstance(doc, dict):
            return None
        return IssuedChallenge(
            account=str(doc["account"]),
            cohort=str(doc["cohort"]),
            nonce=str(doc["nonce"]),
            challenge=str(doc["challenge"]),
            expires_at=_aware(doc["expires_at"]),
        )

    def upsert(self, wallet: ClassWallet) -> bool:
        self._ensure_indexes()
        before = self.wallets.find_one_and_update(
            {"account": wallet.account, "cohort": wallet.cohort},
            {
                "$set": {
                    "address": wallet.address,
                    "challenge_nonce_used": wallet.challenge_nonce_used,
                    "verified_at": wallet.verified_at,
                }
            },
            upsert=True,
        )
        return before is not None

    def list_wallets(self, cohort: str) -> list[ClassWallet]:
        out = [
            ClassWallet(
                account=str(d["account"]),
                cohort=str(d["cohort"]),
                address=str(d["address"]),
                challenge_nonce_used=str(d.get("challenge_nonce_used", "")),
                verified_at=_aware(d["verified_at"]),
            )
            for d in self.wallets.find({"cohort": cohort}, {"_id": 0})
        ]
        return sorted(out, key=lambda w: w.account)

    def delete_wallet(self, account: str, cohort: str) -> bool:
        result = self.wallets.delete_one({"account": account, "cohort": cohort})
        return int(getattr(result, "deleted_count", 0)) > 0
