"""Entitlements — which customer may access which surface, and how.

Day-one build slice 2 (docs/decisions/2026-06-28-day-one-model.md). Maps
(customer_id, surface_id) -> {public | byok + cred_ref}. The cred_ref is an OPAQUE
REFERENCE to a BYOK credential, NEVER the secret value (invariant #1) — so this layer
is custody-agnostic w.r.t. the open store-vs-never-store decision. surfaces_for() is
the per-customer connected set that powers intra-set discovery (NOT a global catalog).
"""

from __future__ import annotations

import inspect

import pytest

from surfcall.entitlements import Entitlement, EntitlementError, Entitlements


def test_grant_public_and_lookup():
    e = Entitlements()
    ent = e.grant("cust_1", "sosvenezuela")
    assert isinstance(ent, Entitlement)
    assert ent.kind == "public" and ent.cred_ref is None
    got = e.get("cust_1", "sosvenezuela")
    assert got is not None and got.surface_id == "sosvenezuela"


def test_byok_requires_a_cred_ref():
    e = Entitlements()
    ent = e.grant("cust_1", "pegana", kind="byok", cred_ref="vault://cust_1/pegana")
    assert ent.kind == "byok" and ent.cred_ref == "vault://cust_1/pegana"
    with pytest.raises(EntitlementError):
        e.grant("cust_1", "txodds", kind="byok")  # byok with no reference


def test_public_must_not_carry_a_cred_ref():
    e = Entitlements()
    with pytest.raises(EntitlementError):
        e.grant("cust_1", "sos", kind="public", cred_ref="oops")


def test_surfaces_for_is_per_customer_intra_set():
    e = Entitlements()
    e.grant("A", "pegana", kind="byok", cred_ref="r1")
    e.grant("A", "sos")
    e.grant("B", "txodds", kind="byok", cred_ref="r2")
    assert e.surfaces_for("A") == ["pegana", "sos"]
    assert e.surfaces_for("B") == ["txodds"]  # no leakage across customers
    assert e.surfaces_for("unknown") == []


def test_surface_id_is_normalized_on_grant_and_lookup():
    e = Entitlements()
    e.grant("cust_1", "Pegana API", kind="byok", cred_ref="r")
    assert e.get("cust_1", "pegana-api") is not None  # both normalize to the same id


def test_get_unknown_returns_none_and_revoke_removes():
    e = Entitlements()
    e.grant("cust_1", "sos")
    assert e.get("cust_1", "nope") is None
    assert e.revoke("cust_1", "sos") is True
    assert e.get("cust_1", "sos") is None
    assert e.revoke("cust_1", "sos") is False  # idempotent


def test_invalid_customer_id_rejected():
    e = Entitlements()
    with pytest.raises(EntitlementError):
        e.grant("   ", "sos")


def test_grant_takes_a_reference_never_a_raw_secret():
    # Control-plane: the only credential-shaped parameter is cred_ref (a reference).
    params = set(inspect.signature(Entitlements.grant).parameters)
    assert "cred_ref" in params
    assert not (
        params & {"secret", "token", "password", "api_key", "credential", "key"}
    )
