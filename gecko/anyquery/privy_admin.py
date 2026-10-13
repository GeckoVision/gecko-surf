"""Founder-run Privy changes: make the settlement wallet, and rewrite a policy's rules.

Each is a real change to Gecko's Privy app, so each ships disabled and runs only from
an operator command with an explicit live flag. Every rule sent is
authored here from config; nothing in a request can name a wallet, a policy or a rule.

* :func:`create_settlement_wallet` - three calls (``gecko.anyquery.settlement_policy``
  explains why three): store the policy WITHOUT the wrap rule, create the wallet under
  it, then replace the rules with the full set naming the new wallet's wSOL account.
  A failure on the third call still returns the new wallet, so the founder can set its
  id and address and finish with :func:`update_settlement_policy`.
* :func:`update_user_policy` - the user wallet's policy (the first one is
  ``testpolicy00000000000001``) to "USDC only to the treasury, SOL only to the settlement
  wallet". It READS the wallet first and stops unless Privy lists that policy on it,
  which is also the first live check of ``UNCONFIRMED_WALLET_POLICY_IDS_FIELD``.

The app id and secret are resolved from the keychain per call and never held on an
object; ``env`` exists for the offline suite only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..credentials import ChainResolver
from ..privy_backend import (
    UNCONFIRMED_WALLET_POLICY_IDS_FIELD,
    PrivyBackendError,
    Transport,
    create_policy,
    create_solana_wallet,
    get_wallet,
    resolve_config,
    update_policy,
)
from .config import AnyQueryConfig, ConfigError
from .payment_tx import usdc_ata
from .privy_policy import policy_body
from .settlement_policy import SETTLEMENT_POLICY_NAME, privy_settlement_rules
from .wallet_policy import privy_rules, wallet_policy_from_config

__all__ = [
    "AdminError",
    "CreatedSettlement",
    "create_settlement_wallet",
    "settlement_rules",
    "update_settlement_policy",
    "update_user_policy",
    "user_rules",
    "x402_pay_tos_for",
]

#: Stable across reruns, so a retried create gets the first policy and wallet back
#: (Privy honours an idempotency key for a limited time; a rerun days later is refused
#: by the "already configured" check instead).
_SETTLEMENT_POLICY_KEY = hashlib.sha256(b"gecko-settlement-policy:v1").hexdigest()
_SETTLEMENT_WALLET_KEY = hashlib.sha256(b"gecko-settlement-wallet:v1").hexdigest()


class AdminError(Exception):
    """A refused or failed founder step. Never carries a secret or a response body."""


@dataclass(frozen=True)
class CreatedSettlement:
    """What the founder's create made. Public identifiers only."""

    wallet_id: str
    address: str
    policy_id: str
    #: False when the third call (the full rules) failed: finish with update-settlement.
    full_rules: bool
    note: str


def _app(
    env: Mapping[str, str] | None, resolver: ChainResolver | None
) -> dict[str, Any]:
    source = resolve_config(env, resolver=resolver)
    missing = [n for n in ("PRIVY_APP_ID", "PRIVY_APP_SECRET") if not source.get(n)]
    if missing:
        raise AdminError(
            f"not configured: {', '.join(missing)} - set each with "
            f"`gecko auth set <NAME>` (OS keychain)"
        )
    return {
        "app_id": source["PRIVY_APP_ID"],
        "app_secret": source["PRIVY_APP_SECRET"],
        "authorization_key": source.get("PRIVY_AUTHORIZATION_KEY") or None,
    }


def x402_pay_tos_for(
    config: AnyQueryConfig, pinned: Mapping[str, Sequence[str]] | None
) -> dict[str, tuple[str, ...]]:
    """The payTos the settlement policy allows: the env override alone when it is set
    (it pays every call), else the registry's pinned set (payTo -> services)."""
    if config.orthogonal_x402_pay_to is not None:
        return {config.orthogonal_x402_pay_to: ("orthogonal",)}
    return {pay_to: tuple(s) for pay_to, s in (pinned or {}).items()}


def settlement_rules(
    config: AnyQueryConfig,
    *,
    wallet: str | None,
    x402_pay_tos: Mapping[str, Sequence[str]] | None = None,
) -> list[dict]:
    """The settlement wallet's rules from config (``wallet=None``: the pre-create set).
    ``x402_pay_tos`` is the registry's pinned set (:func:`gecko.servicemap.pins.
    pinned_pay_tos`); one "USDC to <provider>, capped" rule per payTo."""
    if not config.fee_wallet:
        raise ConfigError("GECKO_FEE_WALLET is required to restrict the settlement")
    # The x402 rules ride along whatever GECKO_ORTHOGONAL_PAY says: the policy is the
    # founder's ceiling, the switch is the runner's; flipping the switch must not need a
    # policy update (founder ruling, 9 Oct).
    return privy_settlement_rules(
        treasury_usdc_account=usdc_ata(config.fee_wallet),
        max_usdc_micro=config.max_settlement_usdc_micro,
        wallet=wallet,
        max_lamports=config.max_run_sol_lamports,
        x402_pay_tos=x402_pay_tos_for(config, x402_pay_tos),
        max_x402_call_usdc_micro=config.max_x402_call_usdc_micro,
    )


def user_rules(config: AnyQueryConfig) -> list[dict]:
    """The user wallet's rules from config. Without a settlement wallet there is no SOL
    rule, and an update that would silently drop SOL is refused instead."""
    policy = wallet_policy_from_config(config)
    if policy.settlement is None:
        raise ConfigError(
            "GECKO_SETTLEMENT_WALLET is required: the new rules send SOL to it"
        )
    return privy_rules(policy)


def create_settlement_wallet(
    config: AnyQueryConfig,
    *,
    env: Mapping[str, str] | None = None,
    resolver: ChainResolver | None = None,
    transport: Transport | None = None,
) -> CreatedSettlement:
    """Make Gecko's ONE settlement wallet. Disabled unless
    ``GECKO_SETTLEMENT_WALLET_CREATE`` is on; refused when one is already configured."""
    if not config.settlement_create_enabled:
        raise AdminError(
            "settlement wallet creation is off; the founder runs it with "
            "GECKO_SETTLEMENT_WALLET_CREATE=1"
        )
    if config.settlement_wallet_id or config.settlement_wallet:
        raise AdminError(
            "a settlement wallet is already configured; Gecko keeps exactly one"
        )
    app = _app(env, resolver)
    try:
        policy_id = create_policy(
            body=policy_body(
                SETTLEMENT_POLICY_NAME, settlement_rules(config, wallet=None)
            ),
            idempotency_key=_SETTLEMENT_POLICY_KEY,
            transport=transport,
            **app,
        )
        created = create_solana_wallet(
            idempotency_key=_SETTLEMENT_WALLET_KEY,
            policy_ids=[policy_id],
            transport=transport,
            **app,
        )
    except PrivyBackendError as exc:
        raise AdminError(f"the settlement wallet was not created: {exc}") from None
    try:
        update_policy(
            policy_id=policy_id,
            rules=settlement_rules(config, wallet=created.address),
            transport=transport,
            **app,
        )
    except PrivyBackendError as exc:
        return CreatedSettlement(
            created.wallet_id,
            created.address,
            policy_id,
            full_rules=False,
            note=(
                f"wallet created under the pre-create rules (it cannot wrap, so it "
                f"settles nothing yet); the full rules were not stored: {exc}. Set "
                f"its id and address, then run update-settlement-policy."
            ),
        )
    return CreatedSettlement(
        created.wallet_id,
        created.address,
        policy_id,
        full_rules=True,
        note="wallet created; its policy names its own wSOL account",
    )


def update_settlement_policy(
    config: AnyQueryConfig,
    *,
    policy_id: str,
    x402_pay_tos: Mapping[str, Sequence[str]] | None = None,
    env: Mapping[str, str] | None = None,
    resolver: ChainResolver | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Replace the settlement policy's rules with the full set for the CONFIGURED
    wallet. Returns Privy's echo."""
    if not config.settlement_wallet:
        raise AdminError("GECKO_SETTLEMENT_WALLET is not set")
    app = _app(env, resolver)
    try:
        return update_policy(
            policy_id=policy_id,
            rules=settlement_rules(
                config, wallet=config.settlement_wallet, x402_pay_tos=x402_pay_tos
            ),
            transport=transport,
            **app,
        )
    except PrivyBackendError as exc:
        raise AdminError(f"the settlement policy was not updated: {exc}") from None


def update_user_policy(
    config: AnyQueryConfig,
    *,
    wallet_id: str,
    policy_id: str,
    env: Mapping[str, str] | None = None,
    resolver: ChainResolver | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Replace a user wallet's policy rules (SOL only to the settlement wallet, USDC only
    to the treasury). Reads the wallet first: the policy must be one Privy lists on it."""
    rules = user_rules(config)
    app = _app(env, resolver)
    try:
        wallet = get_wallet(
            app_id=app["app_id"],
            app_secret=app["app_secret"],
            wallet_id=wallet_id,
            transport=transport,
        )
    except PrivyBackendError as exc:
        raise AdminError(f"the wallet could not be read: {exc}") from None
    attached = wallet.get(UNCONFIRMED_WALLET_POLICY_IDS_FIELD)
    if not isinstance(attached, list) or policy_id not in attached:
        raise AdminError(
            f"Privy does not list policy {policy_id} on wallet {wallet_id} under "
            f"`{UNCONFIRMED_WALLET_POLICY_IDS_FIELD}`; nothing was changed"
        )
    try:
        return update_policy(
            policy_id=policy_id, rules=rules, transport=transport, **app
        )
    except PrivyBackendError as exc:
        raise AdminError(f"the user policy was not updated: {exc}") from None
