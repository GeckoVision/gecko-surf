"""Flow-A x402 billing — Gecko collects its OWN flat per-surface subscription in USDC.

This is Gecko charging its own fee for keeping a painful API first-call-correct and
drift-watched. It is emphatically **NOT** a rail-for-others, **NOT** a take-rate, and
**NOT** per-call metering. The 402 fires only on the cloud subscribe/renew path
(spec: private/2026-07-01-monetization-and-cloud-spec.md §2/§3). This module mints the
402 payment requirements and, on the return leg, validates + settles a subscription into
a control-plane ``cloud`` entitlement.

Hard invariants (structural, not just policy):

1. **No signer, no broadcast.** This module signs nothing and sends no transaction.
   Settlement is delegated to an INJECTED ``FacilitatorClient`` — the deterministic FAKE
   in stub mode, the ``HttpFacilitatorClient`` RELAY (x402_facilitator.py) in live mode.
   Any real mainnet broadcast is founder-run only; the customer wallet / facilitator
   broadcasts, never the Gecko server, never Claude.
2. **Control-plane only.** Persists ONLY ``{entitlement, expires_at, opaque payment_ref}``
   (in ``Entitlements``). NEVER the USDC, a wallet key, the ``X-PAYMENT`` payload, or any
   response body. ``payment_ref`` is an opaque dedupe key (like ``cred_ref``), not custody.
3. **``X402_MODE=stub`` is the DEFAULT** — ``FakeFacilitator`` + auto-grant; cloud
   user-testing needs no real USDC. ``live`` builds the HTTP relay from DEPLOY env only
   (``facilitator_from_env``; incomplete config fails closed with ``X402ConfigError``)
   and is never flipped without founder go-ahead (docs/x402-go-live.md).
4. **Neutrality.** The facilitator, treasury (``pay_to``), USDC mint (``asset``), and
   network are all injected/config — never hardcoded to one provider/wallet/chain.

``now`` is always injected (no argless clock — deterministic tests + the repo rule).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .entitlements import Entitlement, Entitlements
from .x402 import ChallengeError, PaymentPolicy, validate_challenge

# The live adapter (one seam, two adapters) — re-exported so callers keep a single
# import surface. Safe direction: x402_facilitator never imports x402_pay at module
# scope (Settlement is late-imported inside settle()), so no cycle.
from .x402_facilitator import (
    FacilitatorError as FacilitatorError,
    HttpFacilitatorClient as HttpFacilitatorClient,
    X402ConfigError as X402ConfigError,
    facilitator_from_env as facilitator_from_env,
)

X402_MODE_ENV = "X402_MODE"


def x402_mode() -> str:
    """Resolve the settlement mode. Defaults to ``stub`` (fake facilitator, no real USDC).

    ``__unset__`` is the SSM boot sentinel (infra/push-ssm-params.sh): ECS ``Secrets:``
    params must exist for the task to boot, so an unconfigured deploy carries the
    sentinel — treated as unset here, same contract as ``events._mongo_uri``.
    """
    raw = (os.environ.get(X402_MODE_ENV) or "").strip().lower()
    if not raw or raw == "__unset__":
        return "stub"
    return raw


@dataclass(frozen=True)
class Plan:
    """A flat per-surface subscription plan — Gecko's own fee.

    ``price``/``period_seconds`` are the fee and TTL; ``pay_to`` (treasury), ``asset``
    (USDC mint) and ``network`` are INJECTED config, never hardcoded (neutrality)."""

    surface_id: str
    price: int  # exact fee in base units (e.g. USDC, 6 decimals)
    period_seconds: int
    pay_to: str  # Gecko treasury address (config)
    asset: str  # USDC mint / asset id (config)
    network: str = ""  # e.g. "solana-mainnet" — injected, never hardcoded
    scheme: str = "exact"


@dataclass(frozen=True)
class Settlement:
    """The result of a facilitator settling a payment.

    Control-plane: carries ONLY an opaque reference — never funds, keys, or the payload."""

    reference: str


@runtime_checkable
class FacilitatorClient(Protocol):
    """Provider-agnostic settlement seam (template: ``access.py``'s injected Transport/Signer).

    Any adapter — an x402 wallet, PayAI (recurring x402), Privy/abacatepay/Stripe (card/fiat)
    — implements this; Gecko never hardcodes one. ``verify`` confirms the payment matches the
    served requirements (no funds move); ``settle`` moves funds provider-side and returns an
    opaque reference. Gecko itself NEVER signs or broadcasts."""

    def verify(
        self, payment: Mapping[str, Any], requirements: Mapping[str, Any]
    ) -> bool: ...

    def settle(self, payment: Mapping[str, Any]) -> Settlement: ...


def _method(terms: Mapping[str, Any]) -> Mapping[str, Any]:
    """The first ``accepts`` method, or the mapping itself (flat form tolerance)."""
    accepts = terms.get("accepts")
    if isinstance(accepts, list) and accepts and isinstance(accepts[0], Mapping):
        return accepts[0]
    return terms


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _canonical(payment: Mapping[str, Any]) -> bytes:
    return json.dumps(payment, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class FakeFacilitator:
    """Deterministic, OFFLINE facilitator for D1 — no keys, no network, no real settlement.

    ``verify`` checks the payment echoes the served pay_to/asset/amount. ``settle`` returns a
    deterministic OPAQUE reference derived from the payment (a replay yields the same
    reference and moves no funds — settle is idempotent). This is what ``X402_MODE=stub``
    auto-grants through; it NEVER settles real USDC and holds no key/network state."""

    def verify(
        self, payment: Mapping[str, Any], requirements: Mapping[str, Any]
    ) -> bool:
        method = _method(requirements)
        return (
            str(payment.get("payTo")) == str(method.get("payTo"))
            and str(payment.get("asset")) == str(method.get("asset"))
            and _as_int(payment.get("amount"))
            == _as_int(method.get("maxAmountRequired"))
        )

    def settle(self, payment: Mapping[str, Any]) -> Settlement:
        digest = hashlib.sha256(_canonical(payment)).hexdigest()[:24]
        return Settlement(reference=f"fac_{digest}")


def facilitator_for_mode(mode: str | None = None) -> FacilitatorClient:
    """Resolve a facilitator for the mode.

    ``stub`` (default) -> deterministic ``FakeFacilitator`` (no real USDC; never reads
    the facilitator env). ``live`` -> ``HttpFacilitatorClient`` built from DEPLOY env
    (``facilitator_from_env``; raises ``X402ConfigError`` naming any missing var — the
    flip itself needs founder go-ahead, see docs/x402-go-live.md). Any other value fails
    closed. Neutrality holds: the facilitator/treasury/asset/network are env-injected,
    never hardcoded."""
    resolved = mode or x402_mode()
    if resolved == "stub":
        return FakeFacilitator()
    if resolved == "live":
        return facilitator_from_env()
    raise X402ConfigError(
        f"unknown X402_MODE {resolved!r}: expected 'stub' (default) or 'live'"
    )


def build_payment_requirements(plan: Plan, policy: PaymentPolicy) -> dict[str, Any]:
    """Mint the x402 402 ``accepts`` envelope for a subscription. Pure, deterministic.

    Defense-in-depth: the minted terms are re-validated against our OWN pinned trust anchor
    (``policy``) so we never serve a 402 the return leg would reject."""
    method: dict[str, Any] = {
        "scheme": plan.scheme,
        "asset": plan.asset,
        "payTo": plan.pay_to,
        "maxAmountRequired": str(plan.price),
        "resource": plan.surface_id,
        "network": plan.network,
    }
    envelope: dict[str, Any] = {"x402Version": 1, "accepts": [method]}
    validate_challenge(envelope, policy)  # never mint terms our own anchor rejects
    return envelope


def is_entitled(
    entitlements: Entitlements,
    customer_id: str,
    surface_id: str,
    *,
    now: int,
) -> bool:
    """True iff the customer holds an ACTIVE (non-expired) cloud subscription for the surface.

    An expired or missing entitlement -> ``False`` -> the caller re-mints the 402 envelope.
    Recorded mode (free trial) and public/byok surfaces are gated elsewhere; this decides
    only the paid cloud aggregate."""
    ent = entitlements.get(customer_id, surface_id)
    return (
        ent is not None
        and ent.kind == "cloud"
        and ent.expires_at is not None
        and ent.expires_at > now
    )


def settle_subscription(
    *,
    customer_id: str,
    surface_id: str,
    returned_terms: Mapping[str, Any],
    payment: Mapping[str, Any],
    policy: PaymentPolicy,
    facilitator: FacilitatorClient,
    entitlements: Entitlements,
    period_seconds: int,
    now: int,
) -> Entitlement:
    """The return leg: validate the echoed terms, verify + settle via the injected
    facilitator, then grant a control-plane cloud entitlement.

    Order matters. (1) ``validate_challenge`` against the pinned ``policy`` FIRST — the
    payment-swap defense; a wrong pay_to/asset/amount raises ``ChallengeError`` with NO
    settlement and NO grant. (2) ``facilitator.verify`` the payment matches the served terms.
    (3) Pre-settle idempotency: an already-ACTIVE subscription short-circuits BEFORE settle,
    so a replayed payment never re-invokes ``settle`` on a live facilitator (no
    double-SETTLE, not just no double-grant). (4) ``facilitator.settle`` (Gecko never
    signs/broadcasts). (5) Post-settle idempotency: a replayed settlement (same opaque ref,
    same tenant) returns the existing grant — no double-grant, no extension. (6) Grant a
    control-plane record only: ``{entitlement, expires_at, opaque payment_ref}``.
    """
    # 1. Trust anchor first — never settle terms our policy rejects.
    validate_challenge(returned_terms, policy)
    # 2. Confirm the payment authorizes exactly the served terms (no funds move on verify).
    if not facilitator.verify(payment, returned_terms):
        raise ChallengeError("payment does not authorize the served requirements")
    # 3. Pre-settle short-circuit: an already-active sub must not re-settle. This keeps the
    #    common replay off the facilitator entirely. NOTE: for the post-expiry replay edge, an
    #    injected LIVE facilitator MUST still guarantee idempotent settle on a replayed
    #    payment/nonce — the FakeFacilitator's settle is deterministic and moves no funds.
    active = entitlements.get(customer_id, surface_id)
    if active is not None and is_entitled(
        entitlements, customer_id, surface_id, now=now
    ):
        return active
    # 4. Settle via the injected facilitator; we receive only an opaque reference.
    settlement = facilitator.settle(payment)
    ref = settlement.reference
    # 5. Idempotency: dedupe on the opaque settlement ref, SCOPED to this (customer, surface)
    #    — a replay must not double-grant, and a colliding ref from another tenant can never
    #    return the wrong entitlement.
    existing = entitlements.find_by_payment_ref(customer_id, surface_id, ref)
    if existing is not None:
        return existing
    # 6. Control-plane grant only — no payload, no key, no amount-as-funds is stored.
    return entitlements.grant(
        customer_id,
        surface_id,
        kind="cloud",
        expires_at=now + period_seconds,
        payment_ref=ref,
    )
