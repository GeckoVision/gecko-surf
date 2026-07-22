"""Per-surface Gecko-key gating — the PAID surface is gated, the funnel stays open.

The regression this locks down: ``GECKO_REQUIRE_KEY=on`` used to gate EVERY mount, so
turning it on for the paid ``birdeye`` surface would also have closed the humanitarian
surfaces (``reportavnzla``/``sosvenezuela``) and the public keyless demos
(``txline``/``jito``/``jupiter``) — the funnel and the real public-good users.

Fully offline: Starlette's TestClient over the in-process ASGI app, the in-memory key
registry fake, no Mongo, no network.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")  # skip cleanly if the serve extra isn't installed

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import (  # noqa: E402
    GATED_SURFACES_ENV,
    build_multi_surface_app,
    resolve_gated_surfaces,
)
from gecko.keyregistry import InMemoryKeyRegistry, hash_key, mint_key  # noqa: E402

SPEC = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")

# The hosted shape in miniature: one PAID surface + the public/humanitarian ones.
GATED = "birdeye"
UNGATED = ["jupiter", "jito", "txline", "reportavnzla", "sosvenezuela"]
ACCOUNT = "did:privy:cofounder"

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


@pytest.fixture(autouse=True)
def _no_gated_env(monkeypatch):
    """Env must never leak between tests — each test states its own stance."""
    monkeypatch.delenv(GATED_SURFACES_ENV, raising=False)
    monkeypatch.delenv("GECKO_REQUIRE_KEY", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("PRIVY_APP_ID", raising=False)


def _app(**kwargs: Any):
    surfaces = [(name, SPEC) for name in [GATED, *UNGATED]]
    return build_multi_surface_app(
        surfaces,
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=frozenset({GATED}),
        **kwargs,
    )


def _init(client: TestClient, surface: str, key: str | None = None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    return client.post(f"/{surface}/mcp", json=_INIT, headers=headers)


def _registry_with_key() -> tuple[InMemoryKeyRegistry, str]:
    registry = InMemoryKeyRegistry()
    key = mint_key()
    registry.store_key(
        key_hash=hash_key(key),
        account_id=ACCOUNT,
        label="co-founder",
        surfaces=[
            GATED
        ],  # enabled is not enough: the account must be granted THIS surface
    )
    return registry, key


# --- the gated (paid) surface ------------------------------------------------


def test_gated_surface_denies_without_a_key():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "missing_token"


def test_gated_surface_denies_a_random_key():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED, key=mint_key())  # well-formed, never minted here
    assert resp.status_code == 403
    assert resp.json()["reason"] == "invalid_token"


def test_gated_surface_denies_a_disabled_key():
    registry, key = _registry_with_key()
    registry.set_account_enabled(ACCOUNT, False)
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED, key=key)
    assert resp.status_code == 403
    assert key not in resp.text  # the key is never echoed


def test_gated_surface_allows_the_minted_key():
    registry, key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED, key=key)
    assert resp.status_code == 200


# --- the funnel stays open (the regression) ----------------------------------


@pytest.mark.parametrize("surface", UNGATED)
def test_ungated_surfaces_reachable_with_no_key_while_gate_is_on(surface):
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, surface)
    assert resp.status_code == 200


def test_public_submit_door_stays_open_while_gate_is_on():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, "gecko")  # the meta comprehend front door
    assert resp.status_code == 200


# --- fail closed -------------------------------------------------------------


def test_gate_on_with_no_registry_denies_the_gated_surface_and_keeps_others_open():
    # No registry, no Privy config: the gated surface must DENY (never fail open).
    with TestClient(_app()) as client:
        denied = _init(client, GATED, key=mint_key())
        open_one = _init(client, "jupiter")
    assert denied.status_code == 403
    assert denied.json()["reason"] == "invalid_token"
    assert open_one.status_code == 200


def test_a_raising_registry_denies_with_403_not_500():
    """R3: ``RegistryAllowlist.is_enabled`` used to let a store error propagate, so a
    Mongo blip surfaced as an HTTP 500 (fail-closed, but a different shape — and a
    stack-trace 500 tells a prober the store is reachable-but-broken). It must deny
    exactly like an unresolvable key: a clean 403."""
    registry, key = _registry_with_key()

    class _AllowlistDown:
        """The key still resolves; only the enablement read is down."""

        def __getattr__(self, item):
            return getattr(registry, item)

        def enabled_accounts(self):
            raise RuntimeError("store down")

    with TestClient(_app(key_registry=_AllowlistDown())) as client:
        resp = _init(client, GATED, key=key)
        still_open = _init(client, "jupiter")
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_enabled"
    assert key not in resp.text
    assert still_open.status_code == 200


# --- the set resolution ------------------------------------------------------


def test_default_is_gate_all_so_existing_callers_are_unchanged():
    assert resolve_gated_surfaces() is None


def test_env_overrides_the_hosted_default(monkeypatch):
    monkeypatch.setenv(GATED_SURFACES_ENV, " birdeye , newpaid ")
    assert resolve_gated_surfaces(default=frozenset({"birdeye"})) == frozenset(
        {"birdeye", "newpaid"}
    )


def test_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv(GATED_SURFACES_ENV, "birdeye")
    assert resolve_gated_surfaces(frozenset({"other"})) == frozenset({"other"})


def test_unset_env_falls_back_to_the_hosted_default():
    assert resolve_gated_surfaces(default=frozenset({"birdeye"})) == frozenset(
        {"birdeye"}
    )


def test_serve_mcp_gates_only_the_paid_surface():
    from gecko.serve_mcp import GATED_SURFACES

    assert "birdeye" in GATED_SURFACES
    # The funnel + the humanitarian surfaces must never appear in the gated set.
    assert GATED_SURFACES.isdisjoint(UNGATED)


def test_enabled_but_ungranted_is_denied():
    """Being enabled is not the same as being allowed HERE. Without the per-surface
    scope one key opened every gated surface at once, so enabling a developer for a
    future paid API would silently hand them every other paid API too."""
    registry, key = _registry_with_key()
    registry.set_account_surfaces(ACCOUNT, [])  # enabled, granted nothing
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED, key=key)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_enabled"


def test_a_grant_for_one_surface_does_not_open_another():
    registry, key = _registry_with_key()
    registry.set_account_surfaces(ACCOUNT, ["some-other-paid-api"])
    with TestClient(_app(key_registry=registry)) as client:
        resp = _init(client, GATED, key=key)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_enabled"


# --- birdeye live/recorded switch ------------------------------------------------


def _birdeye_mode(monkeypatch, value: str | None) -> str:
    from gecko import serve_mcp

    if value is None:
        monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("BIRDEYE_API_KEY", value)
    monkeypatch.setenv("REFUGIOS_APIKEY", "")
    from gecko.enforce import resolve_hosted_enforce

    for name, surface in serve_mcp._build_surfaces(resolve_hosted_enforce()):
        if name == "birdeye":
            return str(surface.mode)
    raise AssertionError("birdeye surface not built")


def test_birdeye_stays_recorded_without_a_key(monkeypatch):
    """Fail-SAFE, not fail-closed: no key must degrade to $0 recorded, never error and
    never spend."""
    assert _birdeye_mode(monkeypatch, None) == "recorded"


def test_birdeye_stays_recorded_on_the_unset_sentinel(monkeypatch):
    """push-ssm provisions `__unset__`; treating it as a real key would send the literal
    string as X-API-KEY and bill nothing but fail every call."""
    assert _birdeye_mode(monkeypatch, "__unset__") == "recorded"


def test_birdeye_stays_recorded_on_a_blank_key(monkeypatch):
    assert _birdeye_mode(monkeypatch, "   ") == "recorded"


def test_birdeye_goes_live_with_a_real_key(monkeypatch):
    assert _birdeye_mode(monkeypatch, "bd-real-key-value") == "live"


def test_the_birdeye_key_never_reaches_a_tool_def(monkeypatch):
    """Invariant #4: auth is invisible to the agent. A live upstream key must not be
    discoverable in any tool definition the agent receives."""
    from gecko import serve_mcp

    secret = "bd-super-secret-key"
    monkeypatch.setenv("BIRDEYE_API_KEY", secret)
    monkeypatch.setenv("REFUGIOS_APIKEY", "")
    from gecko.enforce import resolve_hosted_enforce

    for name, surface in serve_mcp._build_surfaces(resolve_hosted_enforce()):
        if name == "birdeye":
            blob = json.dumps(surface.client.list_tools())
            assert secret not in blob
            return
    raise AssertionError("birdeye surface not built")
