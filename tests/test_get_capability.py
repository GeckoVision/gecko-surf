"""`get_capability` / `AgentApiClient.get_tool` — the explicit fetch-one-in-full step
that completes the ref→resolve→call loop (progressive disclosure).

Offline/$0. Pure lookup: a known usable tool returns its full callable def (schema +
_invoke, auth still hidden); an unknown OR auth-gated-unavailable name raises a typed
error, never a bare KeyError and never a leaked half-authed def.
"""

from __future__ import annotations

import pytest

from gecko import AgentApiClient, public_session
from gecko.client import ToolNotFound
from gecko.mcp_server import McpSurface

# One public op + one auth-gated op. Under a public (no-auth) session the gated op is
# hidden from the usable set — so get_tool on it must raise, not leak an uncallable def.
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Cap API", "version": "1.0.0"},
    "servers": [{"url": "https://cap.example.com"}],
    "components": {"securitySchemes": {"bear": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/public/{id}": {
            "get": {
                "operationId": "getPublic",
                "summary": "Get a public thing",
                "tags": ["P"],
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/private": {
            "get": {
                "operationId": "getPrivate",
                "summary": "Gated op",
                "tags": ["P"],
                "security": [{"bear": []}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _client() -> AgentApiClient:
    return AgentApiClient(SPEC, session=public_session())


def test_get_tool_returns_full_def_for_known_usable_tool() -> None:
    client = _client()
    name = client.list_tools()[0]["name"]
    tool = client.get_tool(name)
    assert tool["name"] == name
    assert "inputSchema" in tool and tool["inputSchema"]["type"] == "object"
    # Carries invocation metadata so the loop reaches a real call — control-plane safe.
    assert tool["_invoke"]["method"] == "GET"
    assert tool["_invoke"]["path"] == "/public/{id}"


def test_get_tool_unknown_name_raises_typed_error() -> None:
    with pytest.raises(ToolNotFound):
        _client().get_tool("no_such_tool")


def test_get_tool_auth_gated_op_is_hidden_not_leaked() -> None:
    # getPrivate requires auth the public session can't provide → hidden → raises.
    with pytest.raises(ToolNotFound):
        _client().get_tool("getPrivate")


def test_mcp_get_capability_dispatches_to_client() -> None:
    surface = McpSurface(_client())
    name = surface.client.list_tools()[0]["name"]
    # The thin surface method and the call_tool verb both route to client.get_tool.
    assert surface.get_capability(name)["name"] == name
    assert surface.call_tool("get_capability", {"name": name})["name"] == name


def test_mcp_get_capability_unknown_raises() -> None:
    with pytest.raises(ToolNotFound):
        McpSurface(_client()).call_tool("get_capability", {"name": "nope"})
