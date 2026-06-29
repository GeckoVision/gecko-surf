"""Per-customer binding — the day-one payoff (build slice 3).

Joins the Surface Registry + Entitlements + the access seam: given a customer_id and a
surface they've connected, build an McpSurface bound to THEIR session — so the visible
tool set is per-customer (public session hides auth-gated tools; a BYOK session reveals
them). Tested offline in recorded mode; the BYOK credential resolver is injected so this
slice stays custody-agnostic (slice 4 provides the real resolver).
"""

from __future__ import annotations

import pytest

from surfcall.binding import BindingError, CustomerBinder
from surfcall.entitlements import Entitlements
from surfcall.surfaces import SurfaceRegistry

# A surface with one public op and one auth-gated op (per-op security).
SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.example.com"}],
    "components": {
        "securitySchemes": {
            "key": {"type": "apiKey", "name": "X-Api-Key", "in": "header"}
        }
    },
    "paths": {
        "/public": {
            "get": {
                "operationId": "publicRead",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/private": {
            "get": {
                "operationId": "privateRead",
                "security": [{"key": []}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    },
}


class _AuthedSession:
    """A BYOK-resolved session that injects the customer's credential."""

    def auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": "resolved-from-ref"}


def _setup(access="paid"):
    surfaces = SurfaceRegistry()
    surfaces.register("ex", SPEC, base_url="https://api.example.com", access=access)
    return surfaces, Entitlements()


def test_public_binding_exposes_only_public_tools():
    surfaces, ents = _setup(access="public")
    ents.grant("cust", "ex")  # connected, public (no creds)
    surface = CustomerBinder(surfaces, ents, mode="recorded").bind("cust", "ex")
    assert surface is not None
    names = {t["name"] for t in surface.list_tools()}
    assert "publicRead" in names
    assert "privateRead" not in names  # auth-gated hidden for a no-auth session


def test_byok_binding_reveals_auth_gated_tools_via_resolver():
    surfaces, ents = _setup(access="paid")
    ents.grant("cust", "ex", kind="byok", cred_ref="vault://cust/ex")
    seen: list[str | None] = []

    def resolver(ent):
        seen.append(ent.cred_ref)  # the resolver gets a REFERENCE, not a secret
        return _AuthedSession()

    surface = CustomerBinder(
        surfaces, ents, byok_resolver=resolver, mode="recorded"
    ).bind("cust", "ex")
    assert surface is not None
    names = {t["name"] for t in surface.list_tools()}
    assert {"publicRead", "privateRead"} <= names  # authed session sees all
    assert seen == ["vault://cust/ex"]


def test_bind_none_when_not_entitled_or_surface_missing():
    surfaces, ents = _setup()
    binder = CustomerBinder(surfaces, ents)
    assert binder.bind("cust", "ex") is None  # not connected
    ents.grant("cust", "ghost")  # connected to a surface that isn't registered
    assert binder.bind("cust", "ghost") is None


def test_byok_without_resolver_raises():
    surfaces, ents = _setup()
    ents.grant("cust", "ex", kind="byok", cred_ref="r")
    with pytest.raises(BindingError):
        CustomerBinder(surfaces, ents).bind("cust", "ex")


def test_connected_surfaces_is_the_customer_set():
    surfaces, ents = _setup()
    ents.grant("cust", "a")
    ents.grant("cust", "b")
    ents.grant("other", "c")
    binder = CustomerBinder(surfaces, ents)
    assert binder.connected_surfaces("cust") == ["a", "b"]
    assert binder.connected_surfaces("other") == ["c"]


def test_bound_surface_makes_a_recorded_call():
    surfaces, ents = _setup(access="public")
    ents.grant("cust", "ex")
    surface = CustomerBinder(surfaces, ents, mode="recorded").bind("cust", "ex")
    assert surface is not None
    res = surface.call_tool("publicRead", {})
    assert res["status"] == 200 and res["mode"] == "recorded"
