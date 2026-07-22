"""A gated (PAID) surface must not leak its comprehension artifact to anonymous callers.

Adversarial-review residual R1. The gate wrapped ONLY ``Route(/mcp)``, so every sibling
discovery route of the paid mount was served in the clear, and the registry store added
EVERY surface at ``tier="free"`` entirely outside the mount. Measured anonymously:

* ``200 /birdeye/tools.md`` (31KB — all 89 tool defs), ``llms.txt``, ``SKILL.md``,
  ``gecko.json``, ``.well-known/gecko.json``
* ``200 /registry/surfaces/birdeye`` — 71KB, the FULL OpenAPI spec
* ``/`` and ``/.well-known/gecko.json`` advertised the paid surface BY NAME.

Comprehension IS the product: handing it out for free is the same drift as serving the
paid API openly. Now the gate wraps the whole ``Mount`` (403, the same decision the
``/mcp`` edge already made), the gated name is excluded from the anonymous registry
store (404 — no oracle), and the root index/manifests name it only to a valid key.

The critical regression, asserted in the same file: every PUBLIC surface — mcp AND its
discovery routes AND its registry entry — stays reachable with NO key.

Fully offline: in-process ASGI (Starlette TestClient) + the in-memory key registry fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

import gecko.serve_mcp as serve_mcp  # noqa: E402
from gecko.http_server import build_multi_surface_app  # noqa: E402
from gecko.keyregistry import InMemoryKeyRegistry, hash_key, mint_key  # noqa: E402
from gecko.registry.api import registry_routes  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json"
SPEC = str(SPEC_PATH)
PAID = "birdeye"
PUBLIC = "jupiter"
ACCOUNT = "did:privy:cofounder"
GATED = frozenset({PAID})

# Every per-surface discovery artifact build_http_app emits (the whole leaked set).
ARTIFACTS = ["tools.md", "llms.txt", "SKILL.md", "gecko.json", ".well-known/gecko.json"]

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
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "GECKO_GATED_SURFACES",
        "GECKO_REQUIRE_KEY",
        "MONGODB_URI",
        "PRIVY_APP_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _registry_with_key() -> tuple[InMemoryKeyRegistry, str]:
    registry = InMemoryKeyRegistry()
    key = mint_key()
    registry.store_key(
        key_hash=hash_key(key),
        account_id=ACCOUNT,
        label="co-founder",
        surfaces=[
            PAID
        ],  # enabled is not enough: the account must be granted THIS surface
    )
    return registry, key


def _app(*, require_key: bool = True, key_registry: Any = None) -> Any:
    spec = json.loads(SPEC_PATH.read_text("utf-8"))
    # The registry store the hosted server builds — gated names excluded (see main()).
    store = serve_mcp._registry_store(
        [(PAID, spec), (PUBLIC, spec)], exclude=GATED if require_key else frozenset()
    )
    return build_multi_surface_app(
        [(PAID, SPEC), (PUBLIC, SPEC)],
        allowed_hosts=["testserver"],
        require_gecko_key=require_key,
        gated_surfaces=GATED,
        key_registry=key_registry,
        registry_routes=registry_routes(store, None),
    )


def _auth(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


# --- the gated surface's discovery siblings are behind the gate ---------------


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_gated_discovery_artifacts_deny_without_a_key(artifact):
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = client.get(f"/{PAID}/{artifact}")
    assert resp.status_code == 403
    assert resp.json()["reason"] == "missing_token"


def test_gated_tools_md_does_not_leak_a_single_tool_name():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        body = client.get(f"/{PAID}/tools.md").text
    # The fixture's operation ids must not appear anywhere in a denial body.
    assert "state" not in body
    assert len(body) < 512  # a 403 envelope, not a 31KB artifact


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_gated_discovery_artifacts_are_reachable_with_a_minted_key(artifact):
    registry, key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = client.get(f"/{PAID}/{artifact}", headers=_auth(key))
    assert resp.status_code == 200


def test_gated_mcp_still_denies_and_still_allows_the_minted_key():
    registry, key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        denied = client.post(f"/{PAID}/mcp", json=_INIT, headers=_MCP_HEADERS)
        allowed = client.post(
            f"/{PAID}/mcp", json=_INIT, headers={**_MCP_HEADERS, **_auth(key)}
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_the_alb_health_check_is_never_gated():
    # The ALB target is the ROOT /healthz — it must stay 200 with no key.
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        assert client.get("/healthz").status_code == 200


# --- the registry never distributes the gated surface's spec anonymously ------


def test_registry_fetch_of_a_gated_surface_is_404():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = client.get(f"/registry/surfaces/{PAID}")
    assert resp.status_code == 404
    assert resp.json() == {"error": "unknown_surface"}


def test_registry_listing_does_not_name_the_gated_surface():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        names = [s["name"] for s in client.get("/registry/surfaces").json()["surfaces"]]
    assert PAID not in names
    assert PUBLIC in names


def test_registry_search_cannot_reach_the_gated_surface():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        results = client.get("/registry/search?intent=state").json()["results"]
    assert PAID not in {r["surface"] for r in results}


def test_registry_store_excludes_the_gated_name():
    spec = json.loads(SPEC_PATH.read_text("utf-8"))
    store = serve_mcp._registry_store([(PAID, spec), (PUBLIC, spec)], exclude=GATED)
    assert PAID not in store.names()
    assert PUBLIC in store.names()


def test_registry_store_excludes_nothing_by_default():
    # Existing callers (and the free/public distribution path) are unchanged.
    spec = json.loads(SPEC_PATH.read_text("utf-8"))
    store = serve_mcp._registry_store([(PAID, spec), (PUBLIC, spec)])
    assert PAID in store.names()


# --- the gated surface is not advertised by name to anonymous callers ---------


def test_root_index_does_not_name_the_gated_surface():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        body = client.get("/").json()
    names = [s["name"] for s in body["surfaces"]]
    assert PAID not in names
    assert PUBLIC in names
    assert PAID not in json.dumps(body)


def test_well_known_gecko_does_not_name_the_gated_surface():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        body = client.get("/.well-known/gecko.json").json()
    assert PAID not in [s["name"] for s in body["surfaces"]]


def test_well_known_x402_does_not_name_the_gated_surface():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        body = client.get("/.well-known/x402.json").json()
    assert PAID not in [s["name"] for s in body["surfaces"]]
    assert PUBLIC in [s["name"] for s in body["surfaces"]]


def test_a_valid_key_still_discovers_the_gated_surface_in_the_index():
    registry, key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        body = client.get("/", headers=_auth(key)).json()
    assert PAID in [s["name"] for s in body["surfaces"]]


# --- the critical regression: PUBLIC surfaces are untouched -------------------


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_public_discovery_artifacts_need_no_key(artifact):
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        resp = client.get(f"/{PUBLIC}/{artifact}")
    assert resp.status_code == 200


def test_public_mcp_and_registry_entry_need_no_key():
    registry, _key = _registry_with_key()
    with TestClient(_app(key_registry=registry)) as client:
        mcp = client.post(f"/{PUBLIC}/mcp", json=_INIT, headers=_MCP_HEADERS)
        entry = client.get(f"/registry/surfaces/{PUBLIC}")
    assert mcp.status_code == 200
    assert entry.status_code == 200
    assert entry.json()["name"] == PUBLIC
    assert "paths" in entry.json()["spec"]


def test_an_ungated_mount_is_never_wrapped_by_a_gate_object():
    # Byte-identical: the public mount's app is the raw sub-app, not a gate wrapper.
    registry, _key = _registry_with_key()
    app = _app(key_registry=registry)
    mounts = {r.path: r.app for r in app.routes if getattr(r, "path", None)}
    assert type(mounts[f"/{PUBLIC}"]).__name__ == "Starlette"
    assert type(mounts[f"/{PAID}"]).__name__ == "_GeckoKeyGateASGI"


def test_gate_off_leaves_every_mount_and_the_index_exactly_as_before():
    # No gate wired => nothing is hidden and nothing is wrapped (the paid surface is
    # open anyway; hiding it would be theatre). The boot guard forbids this combination
    # on the hosted deploy — see test_gated_boot_guard.
    with TestClient(_app(require_key=False)) as client:
        assert client.get(f"/{PAID}/tools.md").status_code == 200
        assert PAID in [s["name"] for s in client.get("/").json()["surfaces"]]
