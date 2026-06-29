"""Surface Registry — control-plane store of comprehended API surfaces.

Day-one build slice 1 (see docs/decisions/2026-06-28-day-one-model.md). The registry
holds ONLY the API surface (spec) + metadata, keyed by a stable surface_id, so
comprehension is computed once per provider and reused per customer. It must NEVER be
able to hold a response payload (invariant #1), and it deliberately exposes no public
browsable catalog.
"""

from __future__ import annotations

import inspect

import pytest

from surfcall.surfaces import (
    Surface,
    SurfaceError,
    SurfaceRegistry,
    safe_surface_id,
    surface_rev,
)

PEGANA_SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.pegana.xyz"}],
    "paths": {"/v1/assets/{symbol}/state": {"get": {"operationId": "state"}}},
}


def test_safe_surface_id_slugs_and_rejects_empty():
    assert safe_surface_id("Pegana API") == "pegana-api"
    assert safe_surface_id("  TxODDS / World-Cup  ") == "txodds-world-cup"
    with pytest.raises(SurfaceError):
        safe_surface_id("   ")


def test_surface_rev_is_stable_for_same_spec_and_changes_on_edit():
    rev = surface_rev(PEGANA_SPEC)
    assert rev == surface_rev(dict(PEGANA_SPEC))  # same content -> same rev
    changed = {**PEGANA_SPEC, "info": {"version": "0.2.0"}}
    assert surface_rev(changed) != rev  # any spec change bumps the rev


def test_register_get_roundtrip_with_access_tier():
    reg = SurfaceRegistry()
    s = reg.register(
        "Pegana API", PEGANA_SPEC, base_url="https://api.pegana.xyz", access="paid"
    )
    assert isinstance(s, Surface)
    assert s.surface_id == "pegana-api"
    assert s.base_url == "https://api.pegana.xyz"
    assert s.access == "paid"
    assert s.surface_rev == surface_rev(PEGANA_SPEC)
    got = reg.get("Pegana API")  # lookups normalize the id
    assert got is not None and got.surface_id == "pegana-api"
    assert reg.get("nope") is None


def test_access_defaults_public():
    reg = SurfaceRegistry()
    assert reg.register("sos", PEGANA_SPEC, base_url="https://x").access == "public"


def test_reregister_same_spec_keeps_rev_changed_spec_bumps():
    reg = SurfaceRegistry()
    r1 = reg.register("p", PEGANA_SPEC, base_url="https://x").surface_rev
    r2 = reg.register("p", dict(PEGANA_SPEC), base_url="https://x").surface_rev
    assert r1 == r2  # idempotent comprehension
    r3 = reg.register("p", {**PEGANA_SPEC, "x": 1}, base_url="https://x").surface_rev
    assert r3 != r1  # new revision on change


def test_ids_is_internal_only_not_a_public_catalog():
    # ids() is for internal ops; there is no per-provider browsable surface for agents.
    reg = SurfaceRegistry()
    reg.register("a", PEGANA_SPEC, base_url="https://x")
    reg.register("b", PEGANA_SPEC, base_url="https://y")
    assert reg.ids() == ["a", "b"]


def test_register_cannot_accept_a_response_payload():
    # Control-plane guard: the signature takes a spec + metadata only — there is no
    # parameter through which a response body / payload could be stored.
    params = set(inspect.signature(SurfaceRegistry.register).parameters)
    assert not (params & {"data", "response", "payload", "body", "result"})
