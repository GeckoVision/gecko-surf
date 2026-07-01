"""x402 402-challenge validation — the enforce point for the payment-swap scenario.

Pure, no network, never signs and NEVER auto-pays. Given a 402 challenge body and the
``PaymentPolicy`` pinned OUT-OF-BAND at provisioning, it returns a validated ``Challenge``
or raises ``ChallengeError`` naming ONLY the failed field
(``pay_to``/``asset``/``max_amount``/``scheme``) — never the attacker's address/amount
value (redact before raising). The trust anchor is the provisioning policy, never the
(attacker-influenced) 402 body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ChallengeError(Exception):
    """Raised when an x402 402 challenge is malformed or fails the provisioning policy.

    The message names the FAILED FIELD only — never the attacker address/amount value."""


@dataclass(frozen=True)
class PaymentPolicy:
    """Payment constraints pinned OUT-OF-BAND at provisioning — the settlement trust
    anchor, never derived from the served 402 body."""

    allowed_pay_to: frozenset[str]
    allowed_assets: frozenset[str]
    max_amount: int  # ceiling (``<=``) — or the EXACT required price when exact_amount
    scheme: str = "exact"  # the settlement scheme we provisioned
    exact_amount: bool = (
        False  # subscriptions want an exact price (``==``), not a ceiling
    )


@dataclass(frozen=True)
class Challenge:
    """A parsed, normalized x402 402 challenge (no policy judgement applied yet)."""

    pay_to: str
    max_amount: int
    asset: str
    scheme: str


def _first(body: Mapping[str, Any], *names: str) -> Any:
    """Return the first present, non-null value among ``names`` (for camelCase/snake_case
    tolerance across the flat harness form and the real x402 envelope)."""
    for name in names:
        value = body.get(name)
        if value is not None:
            return value
    return None


def parse_challenge(body: Mapping[str, Any]) -> Challenge:
    """Parse an x402 402 JSON body into a ``Challenge``. Pure; no network.

    Accepts both the flat form the harness scenarios use and the real x402
    ``{"accepts": [...]}`` envelope (first accepted method). Raises ``ChallengeError`` on a
    malformed body (missing/unparseable pay_to / asset / amount)."""
    accepts = body.get("accepts")
    source: Mapping[str, Any] = (
        accepts[0]
        if isinstance(accepts, list) and accepts and isinstance(accepts[0], Mapping)
        else body
    )
    pay_to = _first(source, "pay_to", "payTo")
    asset = _first(source, "asset")
    raw_amount = _first(source, "max_amount", "maxAmountRequired", "maxAmount")
    scheme = _first(source, "scheme")

    if not isinstance(pay_to, str) or not pay_to:
        raise ChallengeError("challenge is missing a pay_to recipient")
    if not isinstance(asset, str) or not asset:
        raise ChallengeError("challenge is missing an asset")
    try:
        max_amount = int(raw_amount)  # x402 encodes amounts as base-unit strings
    except (TypeError, ValueError):
        raise ChallengeError("challenge is missing a parseable max_amount") from None
    if not isinstance(scheme, str) or not scheme:
        raise ChallengeError("challenge is missing a scheme")
    return Challenge(pay_to=pay_to, max_amount=max_amount, asset=asset, scheme=scheme)


def validate_challenge(body: Mapping[str, Any], policy: PaymentPolicy) -> Challenge:
    """Return the validated ``Challenge`` or raise ``ChallengeError``. NEVER auto-pays,
    NEVER signs.

    Checks in order — the FIRST failing field decides the (redacted) message:
    ``pay_to ∈ allowed_pay_to``, ``asset ∈ allowed_assets``, amount (``<=`` ceiling, or
    ``==`` the exact price when ``policy.exact_amount``), ``scheme == policy.scheme``."""
    challenge = parse_challenge(body)
    if challenge.pay_to not in policy.allowed_pay_to:
        raise ChallengeError("challenge pay_to is not a provisioned recipient")
    if challenge.asset not in policy.allowed_assets:
        raise ChallengeError("challenge asset is not a provisioned asset")
    if policy.exact_amount:
        if challenge.max_amount != policy.max_amount:
            raise ChallengeError(
                "challenge amount does not match the provisioned price"
            )
    elif challenge.max_amount > policy.max_amount:
        raise ChallengeError("challenge max_amount exceeds the provisioned ceiling")
    if challenge.scheme != policy.scheme:
        raise ChallengeError("challenge scheme does not match the provisioned scheme")
    return challenge
