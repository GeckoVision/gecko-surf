"""Which wallet belongs to which authenticated account — the identity half of custody.

This module holds NO key and performs NO signature. It answers exactly one question:
*for this account id, which wallet is the one it is allowed to spend from?* That is the
record the hosted-signer mode (roadmap 2026-08-13, item (B)) needs and did not have —
:mod:`gecko.keyauth` already resolves a caller to a stable, non-secret ``account`` id, and
nothing mapped that id to a wallet.

THE CHAIN IS ``account_id -> wallet_id -> pubkey``, and the hops belong to different
owners. ``account_id -> wallet_id`` is OUR record and lives here. ``wallet_id -> pubkey``
is the CUSTODIAN's answer and is asked for, never asserted
(``scripts/privy_backend.py``'s ``pubkey`` property fetches it). Only the pubkey ever
enters a transaction.

NO EMAIL LIVES HERE. An email is a communication address, not a spending identity; the
signing path must never be able to resolve a wallet from one. If a caller of this module
finds it needs an email to reach a wallet, that is a design error to report, not a field
to add.

Control plane only: opaque ids and a public key. No secret, no token, no payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "InMemoryWalletDirectory",
    "WalletBinding",
    "WalletBindingError",
    "WalletDirectory",
    "binding_from_document",
]

# The Bitcoin/Solana base58 alphabet (no 0, O, I, l). A SHAPE check only — see
# `_require_pubkey`; it proves nothing about who holds the key.
_B58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_MIN_PUBKEY_CHARS = 32
_MAX_PUBKEY_CHARS = 44
_MAX_ID_CHARS = 256


class WalletBindingError(Exception):
    """A binding is malformed, or the directory could not answer.

    NEVER carries a token, a credential, or a custodian response body — only the field
    that was wrong and why. A directory that fails to answer raises this rather than
    returning ``None``, because "I could not look it up" and "there is no binding" are
    different facts and the second one silently downgrades the caller to mode A.
    """


@dataclass(frozen=True)
class WalletBinding:
    """One account's wallet: ``account_id`` -> (``wallet_id``, ``pubkey``).

    ``pubkey`` IS STORED, AND THE STORAGE IS NOT THE AUTHORITY. It is recorded at bind
    time so that planning a transaction needs no custodian credential and no network hop.
    That makes it **asserted at rest**: a stale record, a botched migration, or a write
    this process did not make would all read back as a confident answer.

    The authority stays where it already is — the signer's three-way check, where
    ``sol_delta_account == fee_payer == backend.pubkey`` and ``backend.pubkey`` is FETCHED
    from the custodian rather than read from here. A stored pubkey that has drifted from
    the custodian's answer must therefore produce a REFUSAL at signing time, never a
    signature. Treat this field as a fast path that the signer re-derives, not as truth.

    ``account_id`` is the login identity's subject (:class:`gecko.keyauth.AuthDecision`'s
    ``account``) and ``wallet_id`` is the custodian's opaque handle. Both are non-secret
    ids. There is deliberately no email, no name, and no contact field of any kind.
    """

    account_id: str
    wallet_id: str
    pubkey: str

    def __post_init__(self) -> None:
        _require_id("account_id", self.account_id)
        _require_id("wallet_id", self.wallet_id)
        _require_pubkey(self.pubkey)


@runtime_checkable
class WalletDirectory(Protocol):
    """READ seam: the one question the call path is allowed to ask.

    Read-only on purpose. Binding a wallet is an enrolment act with its own
    authentication; a call path that could also WRITE here could rebind an account to a
    wallet mid-request, which is the same defect as trusting a caller-supplied address.
    """

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        """The binding for ``account_id``, or ``None`` if that account has none.

        Raises :class:`WalletBindingError` if the directory could not answer at all — a
        lookup failure must not be indistinguishable from "no binding".
        """
        ...


@dataclass
class InMemoryWalletDirectory:
    """A directory backed by a dict. For tests and for a single-tenant local run.

    Not durable and not shared between processes, so it is not the hosted store; the
    hosted one is :class:`gecko.registry.wallets.MongoWalletDirectory`, which answers the
    same one method.
    """

    bindings: dict[str, WalletBinding] = field(default_factory=dict)

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        if not account_id or not account_id.strip():
            return None
        return self.bindings.get(account_id.strip())

    def bind(self, binding: WalletBinding) -> None:
        """Enrolment, kept OFF the read protocol on purpose (see :class:`WalletDirectory`)."""
        self.bindings[binding.account_id] = binding


def binding_from_document(document: Any) -> WalletBinding:
    """A stored record -> a :class:`WalletBinding`, or raise.

    Shared by every persistent directory so one place decides what a stored row must
    contain. An email field, or any extra key, is simply not read: the record that
    reaches the call path carries opaque ids and a public key or it does not exist.
    """
    if not isinstance(document, dict):
        raise WalletBindingError(
            f"wallet binding record must be a mapping, got {type(document).__name__}"
        )
    try:
        return WalletBinding(
            account_id=str(document["account_id"]),
            wallet_id=str(document["wallet_id"]),
            pubkey=str(document["pubkey"]),
        )
    except KeyError as exc:
        raise WalletBindingError(
            f"wallet binding record is missing {exc.args[0]!r}"
        ) from exc


def _require_id(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WalletBindingError(f"{field_name} must be a non-empty identifier")
    if len(value) > _MAX_ID_CHARS:
        # Untrusted at rest as much as in flight: cap before anything logs or indexes it.
        raise WalletBindingError(
            f"{field_name} is longer than {_MAX_ID_CHARS} characters"
        )
    if "@" in value and field_name == "wallet_id":
        # A wallet handle that looks like an email means somebody wired the contact
        # address into the spending identity. Refuse it here rather than at the signer.
        raise WalletBindingError("wallet_id must be a custodian handle, not an address")


def _require_pubkey(value: str) -> None:
    """A base58 SHAPE check — not a curve check, and not proof of custody.

    Deliberately stdlib: this module must stay importable without the ``[solana]`` extra,
    and the real validation is the custodian's own answer at signing time. The check
    exists so a typo fails at bind time instead of deriving a plan for an address nobody
    controls.
    """
    if not isinstance(value, str) or not value.strip():
        raise WalletBindingError("pubkey must be a non-empty base58 Solana address")
    if not _MIN_PUBKEY_CHARS <= len(value) <= _MAX_PUBKEY_CHARS:
        raise WalletBindingError(
            "pubkey must be a base58 Solana address of "
            f"{_MIN_PUBKEY_CHARS}-{_MAX_PUBKEY_CHARS} characters"
        )
    if set(value) - _B58_ALPHABET:
        raise WalletBindingError(
            "pubkey contains characters outside the base58 alphabet"
        )
