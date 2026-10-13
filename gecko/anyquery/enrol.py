"""Onboarding: Gecko MAKES the user's Privy wallet and binds it to their account.

Bind on CREATE, never on ASSERT (enrolment ruling, 2026-08-14). :meth:`WalletEnrolment.enrol`
takes an account id and nothing else: there is no parameter through which a wallet id or
an address could arrive. A ``wallet_id`` is not a secret, so "bind this wallet" would let
anybody who learned a victim's id spend from it through us; a wallet that did not exist
until this authenticated request made it has nothing to forge.

The account id is the key gate's answer (``gecko.keyauth``), never a tool argument. One
wallet per account: a second enrol is :class:`AlreadyEnrolledError`, never a rebind.
Rebinding is an operator act with a human in it, not an API.

**Born restricted** (review finding 2): the live creator stores a Privy policy first
(:mod:`gecko.anyquery.wallet_policy`: USDC to the treasury and SOL to the settlement
wallet, up to the configured ceilings, nothing else; no program-wide rule) and creates the
wallet under it.

**Ships disabled.** Every create makes a real, fundable Solana account under Gecko's Privy
app, so it runs only when ``GECKO_ANYQUERY_WALLET_CREATE`` is on, and the first live
create is run by the operator, by hand.
"""

from __future__ import annotations

import hashlib

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..privy_backend import (
    CreatedWallet,
    PrivyBackendError,
    Transport,
    create_policy,
    create_solana_wallet,
    idempotency_key_for,
    resolve_config,
)
from ..credentials import ChainResolver
from ..wallet_binding import (
    WalletBinding,
    WalletBindingError,
    binding_from_document,
)
from .config import AnyQueryConfig
from .wallet_policy import USER_POLICY_NAME, WalletPolicy, privy_policy_body

__all__ = [
    "AlreadyEnrolledError",
    "CreateWallet",
    "EnrolmentDisabledError",
    "EnrolmentError",
    "InMemoryWalletBinder",
    "MongoWalletBinder",
    "WalletBinder",
    "WalletEnrolment",
    "privy_wallet_creator",
]


class EnrolmentError(Exception):
    """Redacted: never carries a secret, a wallet id from a caller, or a response body."""


class EnrolmentDisabledError(EnrolmentError):
    """Wallet creation is off (``GECKO_ANYQUERY_WALLET_CREATE``)."""


class AlreadyEnrolledError(EnrolmentError):
    """This account already has a wallet. One per account; never a silent rebind."""


#: ``idempotency_key -> CreatedWallet``. The ONLY input is the key derived from the
#: account id, so the creator cannot be handed a wallet to adopt.
CreateWallet = Callable[[str], CreatedWallet]


class WalletBinder(Protocol):
    """The read seam plus the one write enrolment needs: insert, never update."""

    def wallet_for(self, account_id: str) -> WalletBinding | None: ...

    def bind_new(self, binding: WalletBinding) -> None:
        """Insert ``binding``; raise :class:`AlreadyEnrolledError` if the account has one."""
        ...


@dataclass
class InMemoryWalletBinder:
    """The offline binder. Also a :class:`~gecko.wallet_binding.WalletDirectory`."""

    bindings: dict[str, WalletBinding] = field(default_factory=dict)

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        return self.bindings.get((account_id or "").strip())

    def bind_new(self, binding: WalletBinding) -> None:
        if binding.account_id in self.bindings:
            raise AlreadyEnrolledError("this account already has a wallet")
        self.bindings[binding.account_id] = binding


@dataclass
class MongoWalletBinder:
    """Insert-only over the same collection :class:`gecko.registry.wallets.MongoWalletDirectory`
    reads. Needs a unique index on ``account_id``; a duplicate-key error is the
    "already enrolled" answer, so two racing enrols cannot both bind."""

    collection: Any

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        from ..registry.wallets import MongoWalletDirectory

        return MongoWalletDirectory(self.collection).wallet_for(account_id)

    def bind_new(self, binding: WalletBinding) -> None:
        document = {
            "account_id": binding.account_id,
            "wallet_id": binding.wallet_id,
            "pubkey": binding.pubkey,
        }
        try:
            self.collection.insert_one(document)
        except Exception as exc:  # noqa: BLE001 - driver errors may carry the URI
            if type(exc).__name__ == "DuplicateKeyError":
                raise AlreadyEnrolledError(
                    "this account already has a wallet"
                ) from None
            raise EnrolmentError("the wallet binding could not be written") from None
        # Read it back through the one parser every directory uses.
        binding_from_document(document)


def privy_wallet_creator(
    *,
    policy: WalletPolicy,
    resolver: ChainResolver | None = None,
    env: dict[str, str] | None = None,
    transport: Transport | None = None,
) -> CreateWallet:
    """The live creator. ``policy`` is required: every wallet is born under it (review
    finding 2). Two Privy calls, in order: store the policy, then create the wallet
    naming it. A policy answer without an id stops before any wallet exists.

    Resolves the app id and secret from the keychain PER CALL, so the secret is never
    held on an object that could reach a repr or a log."""
    body_name = USER_POLICY_NAME

    def create(idempotency_key: str) -> CreatedWallet:
        source = resolve_config(env, resolver=resolver)
        missing = [n for n in ("PRIVY_APP_ID", "PRIVY_APP_SECRET") if not source.get(n)]
        if missing:
            raise EnrolmentError(
                f"not configured: {', '.join(missing)} — set each with "
                f"`gecko auth set <NAME>` (OS keychain)"
            )
        authorization_key = source.get("PRIVY_AUTHORIZATION_KEY") or None
        # A retried enrol re-sends the same policy key, so it gets the first policy back
        # rather than a second one.
        policy_key = hashlib.sha256(
            f"gecko-wallet-policy:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        policy_id = create_policy(
            app_id=source["PRIVY_APP_ID"],
            app_secret=source["PRIVY_APP_SECRET"],
            body=privy_policy_body(policy, name=body_name),
            idempotency_key=policy_key,
            authorization_key=authorization_key,
            transport=transport,
        )
        return create_solana_wallet(
            app_id=source["PRIVY_APP_ID"],
            app_secret=source["PRIVY_APP_SECRET"],
            idempotency_key=idempotency_key,
            policy_ids=[policy_id],
            authorization_key=authorization_key,
            transport=transport,
        )

    return create


@dataclass
class WalletEnrolment:
    config: AnyQueryConfig
    binder: WalletBinder
    create: CreateWallet

    def enrol(self, account_id: str) -> WalletBinding:
        """Make and bind this account's wallet. The account id is the gate's answer."""
        if not self.config.wallet_create_enabled:
            raise EnrolmentDisabledError(
                "wallet creation is off; the founder runs the first live create"
            )
        account = (account_id or "").strip()
        if not account:
            raise EnrolmentError("enrolment needs the authenticated account id")
        try:
            if self.binder.wallet_for(account) is not None:
                raise AlreadyEnrolledError("this account already has a wallet")
        except WalletBindingError:
            # "Could not look it up" is not "has none": never create on a failed read.
            raise EnrolmentError("the wallet directory could not be read") from None
        try:
            created = self.create(idempotency_key_for(account))
        except PrivyBackendError as exc:
            raise EnrolmentError(f"the wallet could not be created: {exc}") from None
        try:
            binding = WalletBinding(
                account_id=account, wallet_id=created.wallet_id, pubkey=created.address
            )
        except WalletBindingError as exc:
            raise EnrolmentError(f"the custodian's answer was refused: {exc}") from None
        self.binder.bind_new(binding)
        return binding
