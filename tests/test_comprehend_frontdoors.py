"""The two 'submit your API' front doors over one engine:

  * POST /comprehend — the human page's backend, also directly agent-POST-able.
  * the comprehend_api MCP tool on the /gecko meta surface — the agent door.

Offline: the happy path stubs the core (no network); the rejection paths run the real
core (SSRF validates before any fetch, so no network is touched).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko.comprehend_service import ComprehendError, ComprehendResult  # noqa: E402
from gecko.http_server import (  # noqa: E402
    MAX_COMPREHEND_REQUEST_BYTES,
    build_multi_surface_app,
)
from gecko.mcp_server import MetaComprehendSurface  # noqa: E402

PEGANA = "tests/fixtures/pegana_openapi.json"


def _canned() -> ComprehendResult:
    return ComprehendResult(
        name="Canned API",
        description="stub",
        op_count=1,
        usable_tool_count=1,
        tools=[{"name": "ping", "summary": "ping"}],
        artifacts={"llms.txt": "x", "gecko.json": "{}"},
        quarantined=False,
        warnings=[],
        next_steps={"self_host": "uvx ..."},
    )


def _app() -> Any:
    return build_multi_surface_app([("pegana", PEGANA)], allowed_hosts=["testserver"])


# --- HTTP: POST /comprehend ---


def test_comprehend_route_returns_result(monkeypatch) -> None:
    # Stub the core + the front-door guard so the route is exercised without a network fetch.
    monkeypatch.setattr(
        "gecko.comprehend_service.ensure_submittable", lambda _url: None
    )
    monkeypatch.setattr(
        "gecko.comprehend_service.comprehend_submission",
        lambda url, from_docs=False: _canned(),
    )
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": "https://api.example.com/openapi.json"})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Canned API"
        assert body["tools"] == [{"name": "ping", "summary": "ping"}]
        assert body["quarantined"] is False


def test_comprehend_route_rejects_file_scheme() -> None:
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": "file:///etc/passwd"})
        assert r.status_code == 400
        assert "error" in r.json()


def test_comprehend_route_rejects_private_ip() -> None:
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": "http://127.0.0.1:8000/openapi.json"})
        assert r.status_code == 400


def test_comprehend_route_rejects_local_path() -> None:
    # A schemeless local path must not become a server-side file read (LFI).
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": PEGANA})
        assert r.status_code == 400


def test_comprehend_route_missing_url() -> None:
    with TestClient(_app()) as c:
        assert c.post("/comprehend", json={}).status_code == 400


def test_comprehend_route_rejects_oversize_body() -> None:
    big = "x" * (MAX_COMPREHEND_REQUEST_BYTES + 1)
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": "https://a.example", "pad": big})
        assert r.status_code == 413


def test_index_lists_both_submit_doors() -> None:
    with TestClient(_app()) as c:
        idx = c.get("/").json()
        # comprehended surfaces stay in `surfaces`; a submission is NOT added there.
        assert {s["name"] for s in idx["surfaces"]} == {"pegana"}
        submit = idx["submit"]
        assert submit["http"] == "/comprehend"
        assert submit["mcp"] == "/gecko/mcp"
        assert submit["tool"] == "comprehend_api"


def test_meta_mcp_endpoint_initializes_under_mount() -> None:
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
        r = c.post("/gecko/mcp", json=init, headers=headers)
        assert r.status_code == 200


# --- Agent door: the comprehend_api MCP tool ---


def test_meta_surface_lists_only_comprehend_api() -> None:
    tools = MetaComprehendSurface().list_tools()
    assert [t["name"] for t in tools] == ["comprehend_api"]
    props = tools[0]["inputSchema"]["properties"]
    assert set(props) == {"url", "from_docs"}


def test_comprehend_api_tool_calls_core(monkeypatch) -> None:
    monkeypatch.setattr("gecko.mcp_server.ensure_submittable", lambda _url: None)
    monkeypatch.setattr(
        "gecko.mcp_server.comprehend_submission",
        lambda url, from_docs=False: _canned(),
    )
    out = MetaComprehendSurface().call_tool(
        "comprehend_api", {"url": "https://api.example.com/openapi.json"}
    )
    assert out["name"] == "Canned API"
    assert out["usable_tool_count"] == 1


def test_comprehend_api_tool_rejects_local_path() -> None:
    with pytest.raises(ComprehendError):
        MetaComprehendSurface().call_tool("comprehend_api", {"url": PEGANA})


def test_comprehend_api_tool_requires_url() -> None:
    with pytest.raises(ComprehendError):
        MetaComprehendSurface().call_tool("comprehend_api", {})
