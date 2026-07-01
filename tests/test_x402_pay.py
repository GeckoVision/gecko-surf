"""Flow-A x402 billing (D1, offline) — Gecko collects its OWN flat per-surface
subscription in USDC over x402. NOT a rail-for-others, NOT a take-rate, NOT per-call
metering. Pure/offline: no signer, no broadcast, no network — settlement is delegated to
an injected, FAKE facilitator.

These tests falsify the slice before any wire (Pattern B): valid payment -> cloud grant;
wrong pay_to/asset/amount -> ChallengeError + NO grant; expired -> re-challenge; replayed
payment_ref -> idempotent; stub mode -> auto-grant via FakeFacilitator; and the stored
record is control-plane only ({entitlement, expires_at, opaque payment_ref}).
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from gecko.entitlements import Entitlements
from gecko.x402 import ChallengeError, PaymentPolicy
from gecko.x402_pay import (
    FacilitatorClient,
    FakeFacilitator,
    Plan,
    Settlement,
    build_payment_requirements,
    facilitator_for_mode,
    is_entitled,
    settle_subscription,
)

# --- config (all INJECTED — treasury/mint/network are never hardcoded in the module) ---
_TREASURY = "GECKOtreasury1111111111111111111111111111111"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # illustrative USDC mint
_ATTACKER = "ATTACKER1111111111111111111111111111111111"
_PRICE = 99_000_000  # 99 USDC @ 6dp — Gecko's own flat fee
_PERIOD = 30 * 24 * 3600
_NOW = 1_700_000_000

_PLAN = Plan(
    surface_id="pegana",
    price=_PRICE,
    period_seconds=_PERIOD,
    pay_to=_TREASURY,
    asset=_USDC,
    network="solana-mainnet",
)

# The trust anchor pinned out-of-band. exact_amount=True: a subscription wants an EXACT
# price, not a <= ceiling (the one additive change to gecko/x402.py).
_POLICY = PaymentPolicy(
    allowed_pay_to=frozenset({_TREASURY}),
    allowed_assets=frozenset({_USDC}),
    max_amount=_PRICE,
    scheme="exact",
    exact_amount=True,
)


def _payment_for(reqs: dict, **overrides) -> dict:
    """A customer's X-PAYMENT-shaped dict echoing the served terms (fake, no signature)."""
    m = reqs["accepts"][0]
    payment = {
        "payTo": m["payTo"],
        "asset": m["asset"],
        "amount": int(m["maxAmountRequired"]),
        "nonce": "nonce-abc123",
    }
    payment.update(overrides)
    return payment


def _tamper(reqs: dict, **fields) -> dict:
    body = json.loads(json.dumps(reqs))  # deep copy
    body["accepts"][0].update(fields)
    return body


def _settle(ents: Entitlements, terms: dict, payment: dict, now: int = _NOW):
    return settle_subscription(
        customer_id="cust_1",
        surface_id="pegana",
        returned_terms=terms,
        payment=payment,
        policy=_POLICY,
        facilitator=FakeFacilitator(),
        entitlements=ents,
        period_seconds=_PERIOD,
        now=now,
    )


# --- build_payment_requirements -------------------------------------------------------
def test_build_mints_exact_accepts_envelope():
    reqs = build_payment_requirements(_PLAN, _POLICY)
    m = reqs["accepts"][0]
    assert m["scheme"] == "exact"
    assert m["asset"] == _USDC
    assert m["payTo"] == _TREASURY
    assert m["maxAmountRequired"] == str(_PRICE)
    assert m["resource"] == "pegana"


def test_build_refuses_terms_outside_own_policy():
    # Defense-in-depth: never mint a 402 our own return leg would reject.
    off_plan = dataclasses.replace(_PLAN, price=_PRICE - 1)
    with pytest.raises(ChallengeError):
        build_payment_requirements(off_plan, _POLICY)


# --- exact-amount option (the additive x402.py change) --------------------------------
def test_exact_amount_rejects_underpay_below_ceiling():
    reqs = build_payment_requirements(_PLAN, _POLICY)
    underpay = _tamper(
        reqs, maxAmountRequired=str(_PRICE - 1)
    )  # below ceiling, != exact
    ents = Entitlements()
    with pytest.raises(ChallengeError):
        _settle(ents, underpay, _payment_for(underpay))
    assert ents.get("cust_1", "pegana") is None


# --- happy path -----------------------------------------------------------------------
def test_valid_payment_grants_cloud_entitlement():
    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    ent = _settle(ents, reqs, _payment_for(reqs))
    assert ent.kind == "cloud"
    assert ent.expires_at == _NOW + _PERIOD
    assert ent.payment_ref and ent.payment_ref.startswith("fac_")
    assert ent.cred_ref is None
    assert ents.get("cust_1", "pegana") is ent  # persisted control-plane record


# --- payment-swap defense: wrong terms -> ChallengeError, NO grant --------------------
@pytest.mark.parametrize(
    "tamper",
    [
        {"payTo": _ATTACKER},
        {"asset": "SCAMCOIN"},
        {"maxAmountRequired": str(_PRICE + 1)},
    ],
    ids=["wrong_pay_to", "wrong_asset", "wrong_amount"],
)
def test_wrong_terms_raise_and_do_not_grant(tamper):
    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    bad = _tamper(reqs, **tamper)
    with pytest.raises(ChallengeError):
        _settle(ents, bad, _payment_for(bad))
    assert ents.get("cust_1", "pegana") is None  # no entitlement on rejection


def test_payment_not_matching_terms_is_rejected():
    # Terms valid vs policy, but the payment authorizes a different amount -> no grant.
    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    mismatched = _payment_for(reqs, amount=_PRICE + 5)
    with pytest.raises(ChallengeError):
        _settle(ents, reqs, mismatched)
    assert ents.get("cust_1", "pegana") is None


# --- expiry -> re-challenge -----------------------------------------------------------
def test_expired_entitlement_requires_rechallenge():
    ents = Entitlements()
    ents.grant(
        "cust_1", "pegana", kind="cloud", expires_at=_NOW + _PERIOD, payment_ref="fac_x"
    )
    assert is_entitled(ents, "cust_1", "pegana", now=_NOW + 1) is True
    assert is_entitled(ents, "cust_1", "pegana", now=_NOW + _PERIOD + 1) is False
    # re-challenge: the gate mints the 402 envelope again for the same surface.
    reqs = build_payment_requirements(_PLAN, _POLICY)
    assert reqs["accepts"][0]["resource"] == "pegana"


def test_unknown_customer_is_not_entitled():
    ents = Entitlements()
    assert is_entitled(ents, "nobody", "pegana", now=_NOW) is False


# --- replay -> idempotent (dedupe on the opaque payment_ref) --------------------------
def test_replayed_payment_ref_is_idempotent():
    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    payment = _payment_for(reqs)
    first = _settle(ents, reqs, payment, now=_NOW)
    # Replay the SAME payment at a later clock — must NOT double-grant or extend expiry.
    second = _settle(ents, reqs, payment, now=_NOW + 10_000)
    assert second is first
    assert second.expires_at == _NOW + _PERIOD  # not extended
    assert ents.get("cust_1", "pegana") is first


# --- stub mode -> auto-grant via FakeFacilitator; live needs injection ----------------
def test_stub_is_default_and_auto_grants(monkeypatch):
    monkeypatch.delenv("X402_MODE", raising=False)
    fac = facilitator_for_mode()  # default resolves to stub
    assert isinstance(fac, FakeFacilitator)
    assert isinstance(fac, FacilitatorClient)  # satisfies the neutral Protocol

    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    ent = settle_subscription(
        customer_id="cust_1",
        surface_id="pegana",
        returned_terms=reqs,
        payment=_payment_for(reqs),
        policy=_POLICY,
        facilitator=fac,
        entitlements=ents,
        period_seconds=_PERIOD,
        now=_NOW,
    )
    assert ent.kind == "cloud"


def test_live_mode_requires_injected_facilitator():
    # live is never hardcoded to a wallet/facilitator — it must be injected (neutrality +
    # founder go-ahead). The module refuses to conjure one.
    with pytest.raises(NotImplementedError):
        facilitator_for_mode("live")


# --- control-plane discipline ---------------------------------------------------------
def test_stored_record_is_control_plane_only():
    ents = Entitlements()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    payment = _payment_for(reqs)
    ent = _settle(ents, reqs, payment)

    names = {f.name for f in dataclasses.fields(ent)}
    assert names == {
        "customer_id",
        "surface_id",
        "kind",
        "cred_ref",
        "expires_at",
        "payment_ref",
    }
    values = [getattr(ent, n) for n in names]
    # Never the X-PAYMENT payload, the wallet nonce, or the amount-as-funds.
    assert payment not in values
    assert payment["nonce"] not in values
    assert _PRICE not in values
    assert str(_PRICE) not in values
    # payment_ref is an OPAQUE dedupe key, not the payload/nonce (like cred_ref).
    assert ent.payment_ref != payment["nonce"]


def test_settlement_carries_only_an_opaque_reference():
    names = {f.name for f in dataclasses.fields(Settlement)}
    assert names == {"reference"}


def test_fake_facilitator_settlement_is_deterministic_and_opaque():
    fac = FakeFacilitator()
    reqs = build_payment_requirements(_PLAN, _POLICY)
    payment = _payment_for(reqs)
    s1 = fac.settle(payment)
    s2 = fac.settle(payment)
    assert s1.reference == s2.reference  # deterministic (idempotent settle)
    assert s1.reference.startswith("fac_")
    assert str(_PRICE) not in s1.reference  # opaque — no funds/amount leaked
