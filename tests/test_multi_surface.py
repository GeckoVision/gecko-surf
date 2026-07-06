"""Multi-surface serving: two comprehended APIs on one host, each under /{name}.

Uses Starlette's TestClient (which runs the composed lifespan — unlike a bare
ASGITransport), so this genuinely proves each surface's MCP session manager starts
under the mount. If the lifespan composition were wrong, /{name}/mcp would 500."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import build_multi_surface_app  # noqa: E402

PEGANA = "tests/fixtures/pegana_openapi.json"
JITO = "examples/jito_demo/spec/jito_openapi.json"


def _app():
    return build_multi_surface_app(
        [("pegana", PEGANA), ("jito", JITO)],
        public_url="https://mcp.example.com",
        # TestClient sends `Host: testserver`; allow it past the DNS-rebinding guard.
        allowed_hosts=["testserver"],
    )


def test_root_index_lists_every_surface() -> None:
    with TestClient(_app()) as c:
        assert c.get("/healthz").text == "ok"
        idx = c.get("/").json()
        names = {s["name"] for s in idx["surfaces"]}
        assert names == {"pegana", "jito"}
        assert idx["surfaces"][0]["mcp"].startswith("https://mcp.example.com/")


def test_each_surface_has_its_own_discovery_routes() -> None:
    with TestClient(_app()) as c:
        for name in ("pegana", "jito"):
            gj = c.get(f"/{name}/gecko.json")
            assert gj.status_code == 200
            assert (
                json.loads(gj.text)["mcp"]["url"]
                == f"https://mcp.example.com/{name}/mcp"
            )
            assert c.get(f"/{name}/llms.txt").status_code == 200
        # surfaces don't bleed: jito's bundle op is not on the pegana surface
        assert "sendBundle" in c.get("/jito/tools.md").text
        assert "sendBundle" not in c.get("/pegana/tools.md").text


def test_each_mcp_endpoint_initializes_under_the_mount() -> None:
    """The load-bearing check: the composed lifespan started each session manager, so
    a real MCP `initialize` returns 200 at /{name}/mcp (a dead lifespan -> 500)."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(_app()) as c:
        for name in ("pegana", "jito"):
            r = c.post(f"/{name}/mcp", json=init, headers=headers)
            assert r.status_code == 200, f"{name}/mcp init failed: {r.status_code}"


# --- Change 1: root /mcp resolves (307 -> the meta front door) instead of 404 ---


def test_root_mcp_redirects_to_meta_front_door() -> None:
    """A real MCP client POSTs the conventional /mcp; it must 307 to /gecko/mcp
    (method+body preserving), not 404. Both GET and POST alias."""
    with TestClient(_app()) as c:
        for method in ("get", "post"):
            r = getattr(c, method)("/mcp", follow_redirects=False)
            assert r.status_code == 307, f"{method} /mcp -> {r.status_code}"
            assert r.headers["location"] == "/gecko/mcp"


def test_root_mcp_redirect_lands_on_live_meta_surface() -> None:
    """Follow-through: the redirected POST must actually resolve on the live meta
    surface (proves the alias resolves, not just bounces)."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(_app()) as c:
        r = c.post("/mcp", json=init, headers=headers, follow_redirects=True)
        assert r.status_code == 200


# --- Change 2: root .well-known/{gecko,x402}.json on the public app ---


def test_root_wellknown_gecko_lists_surfaces() -> None:
    with TestClient(_app()) as c:
        r = c.get("/.well-known/gecko.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert {s["name"] for s in body["surfaces"]} == {"pegana", "jito"}


def test_root_wellknown_x402_is_honest() -> None:
    with TestClient(_app()) as c:
        for path in ("/.well-known/x402.json", "/.well-known/x402"):
            r = c.get(path)
            assert r.status_code == 200, path
            body = r.json()
            assert body["custody"] == "none"
            assert body["composes"] == "x402"
            assert {s["name"] for s in body["surfaces"]} == {"pegana", "jito"}
            assert all(s["payment"] == "none" for s in body["surfaces"])
            # honesty / control-plane: no fabricated recipient, price, or secret.
            blob = json.dumps(body).lower()
            assert "pay_to" not in blob
            assert "0x" not in blob
            assert "amount" not in blob


def test_build_x402_manifest_two_surface_fixture() -> None:
    from gecko.wellknown import build_x402_manifest

    m = build_x402_manifest([("alpha", {}), ("beta", {})], "https://h.example.com")
    assert m["provider"] == "gecko"
    assert m["composes"] == "x402"
    assert m["custody"] == "none"
    assert [s["name"] for s in m["surfaces"]] == ["alpha", "beta"]
    assert all(s["payment"] == "none" for s in m["surfaces"])
    assert m["surfaces"][0]["mcp"] == "https://h.example.com/alpha/mcp"
    blob = json.dumps(m).lower()
    assert "pay_to" not in blob
    assert "0x" not in blob


# --- Change 1: getting_started serves onboarding for both audiences, single-source ---


def test_root_index_has_getting_started_for_both_audiences() -> None:
    with TestClient(_app()) as c:
        idx = c.get("/").json()
        gs = idx["getting_started"]
        use = gs["use_an_api"]
        onboard = gs["onboard_your_api"]
        assert use["docs"] == "https://docs.geckovision.tech/quickstart"
        assert onboard["docs"] == "https://docs.geckovision.tech/for-providers"
        # the add-command carries the template placeholder + absolute mcp path
        assert "claude mcp add --transport http <name>" in use["add"]
        assert "https://mcp.example.com/<name>/mcp" in use["add"]
        # self-serve door points at the comprehend endpoint + the meta MCP tool
        assert "https://mcp.example.com/comprehend" in onboard["self_serve"]
        assert "comprehend_api" in onboard["self_serve"]


def test_wellknown_gecko_also_carries_getting_started() -> None:
    """Single-source: getting_started flows through the SAME index dict to both doors."""
    with TestClient(_app()) as c:
        body = c.get("/.well-known/gecko.json").json()
        gs = body["getting_started"]
        assert gs["use_an_api"]["docs"] == "https://docs.geckovision.tech/quickstart"
        assert (
            gs["onboard_your_api"]["docs"]
            == "https://docs.geckovision.tech/for-providers"
        )


# --- Change 2: served /.well-known/onboard.md breadcrumb ---


def test_root_wellknown_onboard_md_serves_both_paths() -> None:
    with TestClient(_app()) as c:
        r = c.get("/.well-known/onboard.md")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        body = r.text
        assert "docs.geckovision.tech/for-providers" in body
        assert "docs.geckovision.tech/quickstart" in body
        assert "claude mcp add" in body
        assert "comprehend" in body
