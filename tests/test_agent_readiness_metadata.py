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


def test_initialize_reports_the_engine_version_not_the_sdk_version() -> None:
    # serverInfo.version said 1.28.1 (the MCP SDK) while the server card said 0.10.3;
    # a blind agent read that as a contradiction (2026-09-01). One version, ours.
    from starlette.testclient import TestClient

    from gecko import __version__
    from gecko.http_server import build_multi_surface_app

    app = build_multi_surface_app(
        [("jupiter", "gecko/examples/jupiter_swap_openapi.json")],
        allowed_hosts=["testserver"],
    )
    with TestClient(app) as client:
        res = client.post(
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
        assert res.status_code == 200
        text = res.text
        import json as _json

        line = next(row for row in text.splitlines() if row.startswith("data:"))
        payload = _json.loads(line[5:])
        assert payload["result"]["serverInfo"]["version"] == __version__


def test_ard_catalog_is_served_at_both_probed_paths() -> None:
    """ARD defines `ard.json`; readiness scanners probe `ai-catalog.json`.

    They cite the same spec and disagree on the filename, so one payload answers
    both names. A discovery file nobody looks for is not discovery.
    """
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
        spec_path = client.get("/.well-known/ard.json")
        probed = client.get("/.well-known/ai-catalog.json")
        assert spec_path.status_code == 200
        assert probed.status_code == 200
        assert spec_path.json() == probed.json()
        # Crawlers fetch this cross-origin; the spec requires it be allowed.
        assert probed.headers["access-control-allow-origin"] == "*"

        entries = probed.json()["entries"]
        names = {e["identifier"].rsplit(":", 1)[-1] for e in entries}
        assert names == {"jupiter", "orquestra"}
        for entry in entries:
            # The domain-anchored URN the spec requires, not a bare slug.
            assert entry["identifier"].startswith("urn:air:"), entry
            assert entry["type"] == "application/mcp-server+json"
            assert entry["url"].endswith("/mcp")
            # 2-5 natural-language queries so a discovery service can match on them.
            assert 2 <= len(entry["representativeQueries"]) <= 5, entry
            # `capabilities` is omitted rather than emptied: an empty array would
            # claim the surface has none, and the honest answer is "ask the endpoint".
            assert "capabilities" not in entry, entry


def test_ard_catalog_withholds_a_gated_surface() -> None:
    """One withholding rule, now four doors: index, card, list_surfaces, ARD."""
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.keyregistry import InMemoryKeyRegistry

    app = build_multi_surface_app(
        [("jupiter", "gecko/examples/jupiter_swap_openapi.json")],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=frozenset({"jupiter"}),
        key_registry=InMemoryKeyRegistry(),
    )
    with TestClient(app) as client:
        assert client.get("/.well-known/ai-catalog.json").json()["entries"] == []


def test_host_llms_txt_leads_with_when_to_use_and_routes_to_every_surface() -> None:
    """The breadcrumb an agent reads first.

    A readiness scan called out that generic marketing copy does not read as
    guidance, so this leads with when to use the host — and, just as usefully,
    when not to.
    """
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    app = build_multi_surface_app(
        [
            ("jupiter", "gecko/examples/jupiter_swap_openapi.json"),
            ("orquestra", OrquestraCatalogSurface()),
        ],
        allowed_hosts=["testserver"],
        public_url="https://mcp.example.com",
    )
    with TestClient(app) as client:
        response = client.get("/llms.txt")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text

        # llms.txt is a navigation index: heading-led, with markdown links.
        assert body.startswith("# Gecko")
        assert "## When to use this" in body
        # The boundary is stated as plainly as the capability — an agent looking
        # for custody should leave rather than try.
        assert "## When not to" in body
        assert "holds no funds" in body
        # Every public surface is reachable from here.
        for name in ("jupiter", "orquestra"):
            assert f"https://mcp.example.com/{name}/mcp" in body
        # ...and so is the rest of the discovery surface.
        assert "/.well-known/mcp/server-card.json" in body
        assert "/.well-known/ard.json" in body
        # Comfortably inside the 30k the convention asks for.
        assert len(body) < 30_000


def test_host_llms_txt_withholds_a_gated_surface() -> None:
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.keyregistry import InMemoryKeyRegistry

    app = build_multi_surface_app(
        [("jupiter", "gecko/examples/jupiter_swap_openapi.json")],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=frozenset({"jupiter"}),
        key_registry=InMemoryKeyRegistry(),
    )
    with TestClient(app) as client:
        body = client.get("/llms.txt").text
        assert "jupiter" not in body
        assert "No public surfaces" in body


def test_an_unlisted_surface_is_served_but_advertised_nowhere() -> None:
    """Served, never advertised — distinct from gated, which demands a key.

    A surface we stop marketing is not a surface we retire: somebody may still be
    calling it, and withdrawing the advertisement is ours to do while breaking
    their integration is not. So the mount answers normally and disappears from
    every discovery door at once.
    """
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    app = build_multi_surface_app(
        [
            ("jupiter", "gecko/examples/jupiter_swap_openapi.json"),
            ("orquestra", OrquestraCatalogSurface()),
        ],
        allowed_hosts=["testserver"],
        unlisted_surfaces={"jupiter"},
        public_url="https://mcp.example.com",
    )
    with TestClient(app) as client:
        # Every door, one rule.
        doors = {
            "index": [s["name"] for s in client.get("/").json()["surfaces"]],
            "gecko.json": [
                s["name"]
                for s in client.get("/.well-known/gecko.json").json()["surfaces"]
            ],
            "server card": [
                r["name"]
                for r in client.get("/.well-known/mcp/server-card.json").json()[
                    "remotes"
                ]
            ],
            "ard": [
                e["identifier"].rsplit(":", 1)[-1]
                for e in client.get("/.well-known/ai-catalog.json").json()["entries"]
            ],
            "x402": [
                s["name"]
                for s in client.get("/.well-known/x402.json").json()["surfaces"]
            ],
        }
        for door, names in doors.items():
            assert "jupiter" not in names, (
                f"{door} still advertises the unlisted surface"
            )
            assert "orquestra" in names, f"{door} lost the listed surface"
        assert "jupiter" not in client.get("/llms.txt").text

        # ...and it still answers, so nobody's integration breaks.
        initialize = client.post(
            "/jupiter/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "c", "version": "1"},
                },
            },
            headers={"accept": "application/json, text/event-stream"},
        )
        assert initialize.status_code == 200, initialize.text


def test_unlisted_env_override_falls_back_rather_than_advertising_everything() -> None:
    """Garbage in the env must never be what puts an unmarketed mount back on show."""
    import os

    from gecko.serve_mcp import resolve_unlisted_surfaces

    default = frozenset({"txline"})
    previous = os.environ.get("GECKO_UNLISTED_SURFACES")
    try:
        for value in ("", "   ", ",", ",,,"):
            os.environ["GECKO_UNLISTED_SURFACES"] = value
            assert resolve_unlisted_surfaces(default) == default, value
        os.environ["GECKO_UNLISTED_SURFACES"] = "Foo, BAR"
        assert resolve_unlisted_surfaces(default) == {"foo", "bar"}
    finally:
        if previous is None:
            os.environ.pop("GECKO_UNLISTED_SURFACES", None)
        else:
            os.environ["GECKO_UNLISTED_SURFACES"] = previous
