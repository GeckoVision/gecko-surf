"""Class-wallet registration: a student proves they control an address before we fund it.

Non-custodial by construction. The student makes the keypair on their own machine and
the key never leaves it; we only ever SEND to the address. The single risk is funding an
address the student does not control (a typo, a pasted exchange deposit address, someone
else's wallet), so registration requires an ed25519 signature, made by that address,
over a challenge WE issued to THIS account a few minutes ago.

This is deliberately not :mod:`gecko.registry.wallets` / :mod:`gecko.wallet_binding`.
That directory binds a HOSTED wallet we can sign for, and its rule is "bind on create,
never on assert", because a wallet id is not a secret and binding one would let us sign
from someone else's wallet. Here we sign for nothing: a registered address is only ever
a destination, and asserting it is safe once the signature proves the asserter holds
its key. Nothing here is ever read by a signing path.

The order of checks is the contract: key -> grant -> challenge (consumed) -> address ->
signature. A challenge is consumed before the signature is checked, so a failed attempt
costs the caller a fresh challenge and a signature cannot be ground against one.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gecko.keyregistry import GeckoKeyResolver, KeyRegistry, RegistryAllowlist

from .class_wallet_store import ClassWallet, ClassWalletStore, IssuedChallenge

__all__ = [
    "BOOTCAMP_SURFACE",
    "CHALLENGE_TTL",
    "ClassWalletError",
    "ClassWalletRegistrar",
    "FundingPlan",
    "FundingRow",
    "Registration",
    "funding_plan",
    "verify_address_signature",
]

#: The grant a founder gives with ``gecko keys grant <account> --surface bootcamp``.
BOOTCAMP_SURFACE = "bootcamp"
CHALLENGE_TTL = timedelta(minutes=10)
_COHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_MAX_FIELD = 512


class ClassWalletError(Exception):
    """A refusal with its HTTP status and stable code. Never carries the key."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Registration:
    account: str
    cohort: str
    address: str
    replaced: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "cohort": self.cohort,
            "address": self.address,
            "registered": True,
            "replaced": self.replaced,
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(UTC)


def verify_address_signature(address: str, message: bytes, signature: str) -> bool:
    """True iff ``signature`` (base58) is ``address``'s ed25519 signature over ``message``.

    Raises :class:`ClassWalletError` ``address-invalid`` when ``address`` is not a
    32-byte base58 key. solders is lazy-imported: it ships in the ``solana`` extra, which
    the hosted image installs.
    """
    from solders.pubkey import Pubkey
    from solders.signature import Signature

    try:
        pubkey = Pubkey.from_string(address)
    except Exception:  # noqa: BLE001 - any parse failure is the same client error
        raise ClassWalletError(
            400, "address-invalid", "address is not a base58 32-byte public key"
        ) from None
    try:
        sig = Signature.from_string(signature)
    except Exception:  # noqa: BLE001 - a malformed signature is an invalid one
        return False
    return bool(sig.verify(pubkey, message))


@dataclass
class ClassWalletRegistrar:
    """The two operations behind the routes. Transport-free, so tests call it directly."""

    resolve_account: Callable[[str], str | None]
    may_access: Callable[[str, str], bool]
    store: ClassWalletStore
    clock: Callable[[], datetime] = _now

    @classmethod
    def from_key_registry(
        cls,
        registry: KeyRegistry,
        store: ClassWalletStore,
        clock: Callable[[], datetime] = _now,
    ) -> ClassWalletRegistrar:
        allowlist = RegistryAllowlist(registry)
        return cls(
            resolve_account=GeckoKeyResolver(registry),
            may_access=allowlist.may_access,
            store=store,
            clock=clock,
        )

    def _authorise(self, key: str) -> str:
        account = self.resolve_account(key) if key else None
        if not account:
            raise ClassWalletError(401, "key-invalid", "missing or invalid Gecko key")
        if not self.may_access(account, BOOTCAMP_SURFACE):
            raise ClassWalletError(
                403, "not-granted", "this account is not granted the bootcamp surface"
            )
        return account

    @staticmethod
    def _cohort(value: object) -> str:
        cohort = str(value or "").strip()
        if not _COHORT_RE.match(cohort):
            raise ClassWalletError(
                400, "cohort-invalid", "cohort is missing or malformed"
            )
        return cohort

    def issue_challenge(self, key: str, cohort: object) -> dict[str, str]:
        account = self._authorise(key)
        cohort_id = self._cohort(cohort)
        expires_at = self.clock() + CHALLENGE_TTL
        nonce = secrets.token_hex(16)
        text = (
            f"dev3pack {cohort_id} class wallet for {account} "
            f"nonce {nonce} expires {_iso(expires_at)}"
        )
        self.store.issue(
            IssuedChallenge(
                account=account,
                cohort=cohort_id,
                nonce=nonce,
                challenge=text,
                expires_at=expires_at,
            )
        )
        return {"challenge": text, "expires_at": _iso(expires_at)}

    def register(self, key: str, body: dict[str, Any]) -> Registration:
        account = self._authorise(key)
        challenge = body.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise ClassWalletError(400, "challenge-invalid", "challenge is required")
        cohort = str(body.get("cohort") or "").strip()
        held = (
            self.store.consume(account=account, cohort=cohort, challenge=challenge)
            if _COHORT_RE.match(cohort) and len(challenge) <= _MAX_FIELD
            else None
        )
        now = self.clock()
        if held is None or held.expires_at <= now:
            raise ClassWalletError(
                400,
                "challenge-invalid",
                "challenge unknown, used, expired, or issued to another account",
            )
        address = body.get("address")
        signature = body.get("signature")
        if not isinstance(address, str) or len(address) > _MAX_FIELD:
            raise ClassWalletError(
                400, "address-invalid", "address is not a base58 32-byte public key"
            )
        address = address.strip()
        if not isinstance(signature, str) or len(signature) > _MAX_FIELD:
            signature = ""
        if not verify_address_signature(
            address, challenge.encode("utf-8"), signature.strip()
        ):
            raise ClassWalletError(
                400,
                "signature-invalid",
                "signature does not verify against the address",
            )
        replaced = self.store.upsert(
            ClassWallet(
                account=account,
                cohort=cohort,
                address=address,
                challenge_nonce_used=held.nonce,
                verified_at=now,
            )
        )
        return Registration(
            account=account, cohort=cohort, address=address, replaced=replaced
        )


# --- the founder's funding arithmetic (used by scripts/class_wallets.py) -------------


@dataclass(frozen=True)
class FundingRow:
    account: str
    address: str
    usdc_raw: int = 0
    sol_lamports: int = 0


@dataclass(frozen=True)
class FundingPlan:
    rows: list[FundingRow]
    total_usdc_raw: int
    total_sol_lamports: int


def funding_plan(
    wallets: Iterable[FundingRow], *, price_raw: int, count: int, sol_lamports: int
) -> FundingPlan:
    """What to send each registered wallet: ``price_raw * count`` USDC raw plus a flat
    ``sol_lamports`` for fees and the first purchase's rent. Arithmetic only; nothing
    here signs or sends."""
    if price_raw < 0 or count < 0 or sol_lamports < 0:
        raise ValueError("amounts must be non-negative")
    usdc = price_raw * count
    rows = [
        FundingRow(
            account=w.account,
            address=w.address,
            usdc_raw=usdc,
            sol_lamports=sol_lamports,
        )
        for w in wallets
    ]
    return FundingPlan(
        rows=rows,
        total_usdc_raw=sum(r.usdc_raw for r in rows),
        total_sol_lamports=sum(r.sol_lamports for r in rows),
    )
