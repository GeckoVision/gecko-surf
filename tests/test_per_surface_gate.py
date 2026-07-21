"""Per-surface Gecko-key gating — the PAID surface is gated, the funnel stays open.

The regression this locks down: ``GECKO_REQUIRE_KEY=on`` used to gate EVERY mount, so
turning it on for the paid ``birdeye`` surface would also have closed the humanitarian
surfaces (``reportavnzla``/``sosvenezuela``) and the public keyless demos
(``txline``/``jito``/``jupiter``) — the funnel and the real public-good users.

Fully offline: Starlette's TestClient over the in-process ASGI app, the in-memory key
registry fake, no Mongo, no network.
"""

from __future__ import annotations

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
    registry.store_key(key_hash=hash_key(key), account_id=ACCOUNT, label="co-founder")
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
