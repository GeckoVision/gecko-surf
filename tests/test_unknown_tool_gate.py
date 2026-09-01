"""An unknown tool name is a JSON-RPC -32602 protocol error, not a tool result.

The SDK's ``@server.call_tool()`` decorator flattens every raised exception into a
``CallToolResult`` with ``isError`` — including ``McpError`` — so before the gate,
a ghost name fell through to the Skill Guard and came back as a BLOCKED tool result:
structured, but on the wrong layer, and an auditor probing error handling saw no
``error.code``/``error.message``. ``install_unknown_tool_gate`` sits above the SDK's
handler where a raised ``McpError`` still becomes the error envelope.
"""

from __future__ import annotations

import json
import warnings

import pytest

from gecko.toolerror import ensure_known_tool

warnings.filterwarnings("ignore", category=DeprecationWarning)

INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _data_line(text: str) -> dict:
    line = next(row for row in text.splitlines() if row.startswith("data:"))
    return json.loads(line[len("data:") :])


@pytest.mark.parametrize("path", ["/jupiter/mcp", "/mcp"])
def test_unknown_tool_is_a_json_rpc_error_with_code_and_names(path: str) -> None:
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    app = build_multi_surface_app(
        [("jupiter", "gecko/examples/jupiter_swap_openapi.json")],
        allowed_hosts=["testserver"],
    )
    with TestClient(app) as client:
        r = client.post(path, json=INIT, headers=HEADERS)
        h = {**HEADERS, "mcp-session-id": r.headers["mcp-session-id"]}
        client.post(
            path,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=h,
        )
        r = client.post(
            path,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
            headers=h,
        )
        body = _data_line(r.text)
        assert "result" not in body  # NOT an isError tool result
        assert body["error"]["code"] == -32602
        # the message names the valid tools so the agent's next call can be right
        assert "no_such_tool" in body["error"]["message"]
        expected = "list_surfaces" if path == "/mcp" else "search_capabilities"
        assert expected in body["error"]["message"]


def test_known_tools_pass_the_gate_untouched() -> None:
    class Surface:
        def list_tools(self):
            return [{"name": "real_tool"}]

    ensure_known_tool(Surface(), "real_tool")  # no raise


def test_enumeration_failure_never_becomes_unknown_tool() -> None:
    class Broken:
        def list_tools(self):
            raise RuntimeError("index down")

    ensure_known_tool(Broken(), "anything")  # conservative: no raise


def test_unknown_name_raises_the_mcp_error() -> None:
    from mcp.shared.exceptions import McpError

    class Surface:
        def list_tools(self):
            return [{"name": "a"}, {"name": "b"}]

    with pytest.raises(McpError) as exc:
        ensure_known_tool(Surface(), "ghost")
    assert exc.value.error.code == -32602
    assert "a, b" in exc.value.error.message
