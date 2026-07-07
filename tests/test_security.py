"""op.security comprehension fix — an agent is never offered (nor able to fire) an
auth-gated op its session can't satisfy. Uses a tiny synthetic spec for control."""

import pytest

from gecko.access import NoAuthSession, Session, public_session
from gecko.caller import CallError, build_request
from gecko.client import AgentApiClient
from gecko.ingest import extract_operations
from gecko.tools import to_tool


def _resp() -> dict:
    return {"200": {"description": "ok"}}


SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://example.test"}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/public": {
            "get": {
                "operationId": "get_public",
                "summary": "public read",
                "responses": _resp(),
            }
        },
        "/private": {
            "get": {
                "operationId": "get_private",
                "summary": "private read",
                "security": [{"bearer": []}],
                "responses": _resp(),
            }
        },
        "/optional": {
            "get": {
                "operationId": "get_optional",
                "summary": "optional read",
                "security": [{}, {"bearer": []}],
                "responses": _resp(),
            }
        },
    },
}


def _ops() -> dict:
    return {o.operation_id: o for o in extract_operations(SPEC)}


def test_no_auth_session_returns_empty_headers():
    assert NoAuthSession().auth_headers() == {}
    assert public_session().auth_headers() == {}


def test_requires_auth_detection():
    ops = _ops()
    assert to_tool(ops["get_public"])["requires_auth"] is False
    assert to_tool(ops["get_private"])["requires_auth"] is True
    # an empty {} requirement means auth is OPTIONAL -> not required
    assert to_tool(ops["get_optional"])["requires_auth"] is False


def test_auth_schemes_surfaced_as_metadata():
    ops = _ops()
    assert to_tool(ops["get_private"])["auth_schemes"] == ["bearer"]
    assert to_tool(ops["get_public"])["auth_schemes"] == []


def test_no_auth_session_hides_gated_tools():
    client = AgentApiClient(SPEC, session=public_session())
    names = {t["name"] for t in client.list_tools()}
    assert "get_public" in names
    assert "get_optional" in names
    assert "get_private" not in names


def test_auth_session_surfaces_gated_tools():
    client = AgentApiClient(SPEC, session=Session(jwt="J", api_token="T"))
    names = {t["name"] for t in client.list_tools()}
    assert "get_private" in names


def test_search_excludes_gated_tools_without_auth():
    client = AgentApiClient(SPEC, session=public_session())
    hits = {h["name"] for h in client.search("private read", limit=5)}
    assert "get_private" not in hits


def test_prepare_gated_tool_without_auth_raises():
    client = AgentApiClient(SPEC, session=public_session())
    with pytest.raises(CallError):
        client.prepare("get_private", {})


def test_prepare_public_tool_without_auth_ok():
    client = AgentApiClient(SPEC, session=public_session())
    req = client.prepare("get_public", {})
    assert req.url.endswith("/public")
    # A public tool carries no auth — only the default User-Agent (so WAFs don't 403
    # the stdlib caller). The point of this test is the ABSENCE of injected auth.
    assert "Authorization" not in req.headers
    assert set(req.headers) == {"User-Agent"}


# FIX 3 — token-exfil guard. base_url derives from untrusted servers[].url; a poisoned
# URL must not carry the customer's injected auth to an attacker host. The error must
# name only the offending host, never the secret.
def _bare_tool() -> dict:
    return {
        "name": "t",
        "inputSchema": {"type": "object", "properties": {}},
        "_invoke": {"method": "GET", "path": "/data", "param_locations": {}},
    }


def test_build_request_refuses_auth_toward_unexpected_host():
    with pytest.raises(CallError) as ei:
        build_request(
            _bare_tool(),
            {},
            base_url="https://evil.attacker.test",
            auth={"Authorization": "SECRETTOKEN"},
            allowed_auth_hosts={"api.woovi.com"},
        )
    msg = str(ei.value)
    assert "evil.attacker.test" in msg
    assert "SECRETTOKEN" not in msg  # the secret must never leak into the error


def test_build_request_injects_auth_toward_allowed_host():
    req = build_request(
        _bare_tool(),
        {},
        base_url="https://api.woovi.com",
        auth={"Authorization": "SECRETTOKEN"},
        allowed_auth_hosts={"api.woovi.com"},
    )
    assert req.headers["Authorization"] == "SECRETTOKEN"


def test_client_pins_auth_host_when_base_url_explicit():
    # Explicit base_url pins the allowlist to that one host (the protected mode).
    client = AgentApiClient(
        SPEC, base_url="https://example.test", session=Session(jwt="J", api_token="T")
    )
    assert client._auth_allowed_hosts == {"example.test"}
    req = client.prepare("get_private", {})
    assert req.headers["Authorization"].startswith("Bearer ")
