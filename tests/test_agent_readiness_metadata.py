"""The agent-readiness metadata pack: tool annotations, the host PRM, the server card.

Every claim here is behavioral, checked over the same wire an auditor probes."""

from __future__ import annotations

import warnings


warnings.filterwarnings("ignore", category=DeprecationWarning)


def test_every_tool_on_every_surface_carries_annotations() -> None:
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec
    from gecko.mcp_server import McpSurface, MetaComprehendSurface
    from gecko.providers.catalog_surface import OrquestraCatalogSurface
    from gecko.providers.cli import PROGRAMS

    surfaces = [
        McpSurface(
            AgentApiClient(load_spec("gecko/examples/jupiter_swap_openapi.json"))
        ),
        MetaComprehendSurface(),
        OrquestraCatalogSurface(),
        *(PROGRAMS[name]() for name in sorted(PROGRAMS)),
    ]
    for surface in surfaces:
        for tool in surface.list_tools():
            annotations = tool.get("annotations")
            assert annotations, f"{type(surface).__name__}:{tool['name']} unannotated"
            assert isinstance(annotations.get("readOnlyHint"), bool)
            assert isinstance(annotations.get("openWorldHint"), bool)


def test_write_operations_are_not_marked_read_only() -> None:
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec
    from gecko.mcp_server import McpSurface

    surface = McpSurface(
        AgentApiClient(load_spec("gecko/examples/jupiter_swap_openapi.json"))
    )
    by_name = {t["name"]: t for t in surface.list_tools()}
    assert by_name["QuoteGet"]["annotations"]["readOnlyHint"] is True
    assert by_name["SwapPost"]["annotations"]["readOnlyHint"] is False


def test_try_purchase_is_declared_non_read_only() -> None:
    # It signs with a throwaway key on a local fork: not destructive, but NOT read-only.
    from gecko.sandbox.try_purchase import TRY_PURCHASE_TOOL

    annotations = TRY_PURCHASE_TOOL["annotations"]
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False


def test_server_card_carries_the_required_registry_fields() -> None:
    from gecko.wellknown import build_server_card

    card = build_server_card(["jupiter"], "https://mcp.example.com")
    for field in ("name", "description", "version", "serverUrl", "tools"):
        assert card.get(field), f"missing {field}"
    assert card["serverUrl"] == "https://mcp.example.com/mcp"
    assert card["version"] not in ("", None)
    assert {t["name"] for t in card["tools"]} == {"comprehend_api", "list_surfaces"}


def test_server_card_lets_a_client_decide_compatibility_before_a_session() -> None:
    # The fields a client reads BEFORE initialize: which protocol, whether to bring a
    # credential, and where the PRM is. The version is the SDK's own constant.
    from mcp.types import LATEST_PROTOCOL_VERSION

    from gecko.wellknown import build_server_card

    card = build_server_card(["jupiter"], "https://mcp.example.com")
    # One registry name for the product, on this host and mirrored on the landing.
    assert card["name"] == "tech.geckovision/gecko"
    assert card["protocolVersion"] == LATEST_PROTOCOL_VERSION
    auth = card["authentication"]
    assert auth["required"] is False
    assert auth["schemes"] == ["bearer"]
    assert (
        auth["resource_metadata"]
        == "https://mcp.example.com/.well-known/oauth-protected-resource"
    )


def test_the_host_index_advertises_llms_txt_only_where_it_is_served() -> None:
    # Ten advertised `/<name>/llms.txt` paths, ten 404s on the live host (2026-09-01):
    # the index named the sibling for every mount, but only OpenAPI surfaces emit it.
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    app = build_multi_surface_app(
        [
            ("jupiter", "gecko/examples/jupiter_swap_openapi.json"),
            ("orquestra", OrquestraCatalogSurface()),
        ],
        allowed_hosts=["testserver"],
    )
    with TestClient(app) as client:
        surfaces = {
            e["name"]: e
            for e in client.get("/.well-known/gecko.json").json()["surfaces"]
        }
        assert "llms_txt" in surfaces["jupiter"]
        assert "llms_txt" not in surfaces["orquestra"]
        for entry in surfaces.values():
            if "llms_txt" in entry:
                assert client.get(entry["llms_txt"]).status_code == 200, entry


def test_host_prm_is_honest_and_carries_the_real_scopes() -> None:
    from gecko.wellknown import build_protected_resource_metadata

    prm = build_protected_resource_metadata("https://mcp.example.com", ["birdeye"])
    assert prm["resource"] == "https://mcp.example.com"
    assert prm["scopes_supported"] == ["surface:birdeye"]
    assert prm["bearer_methods_supported"] == ["header"]
    # No fabricated authorization server — RFC 9728 makes the member optional.
    assert "authorization_servers" not in prm
    assert prm["agent_auth"]["identity_endpoint"].endswith("/auth/login/start")
    assert prm["agent_auth"]["claim_endpoint"].endswith("/auth/login/verify")


def test_prm_served_on_the_host_and_named_by_the_gated_401() -> None:
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.keyregistry import InMemoryKeyRegistry

    app = build_multi_surface_app(
        [("jupiter", "gecko/examples/jupiter_swap_openapi.json")],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=frozenset({"jupiter"}),
        key_registry=InMemoryKeyRegistry(),
        public_url="https://mcp.example.com",
    )
    with TestClient(app) as client:
        prm = client.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        assert prm.json()["scopes_supported"] == ["surface:jupiter"]
        denied = client.post(
            "/jupiter/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "p", "version": "0"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert denied.status_code == 401
        challenge = denied.headers["www-authenticate"]
        assert (
            'resource_metadata="https://mcp.example.com'
            '/.well-known/oauth-protected-resource"' in challenge
        )
