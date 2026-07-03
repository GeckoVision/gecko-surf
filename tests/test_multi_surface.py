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
