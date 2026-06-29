"""op.security comprehension fix — an agent is never offered (nor able to fire) an
auth-gated op its session can't satisfy. Uses a tiny synthetic spec for control."""

import pytest

from gecko.access import NoAuthSession, Session, public_session
from gecko.caller import CallError
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
    assert req.headers == {}
