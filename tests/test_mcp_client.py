"""Calling someone else's MCP — the decoder, and one opt-in lane against a live partner.

PATTERN B. Everything about the protocol is proved offline with an injected transport:
Streamable HTTP is JSON-RPC over POST, and none of the shapes below need a network to
falsify. The `partner_mcp` lane is the final check, never the debugger, and it stays off
unless GECKO_PARTNER_MCP_URL names an endpoint.

The live lane calls READ-ONLY tools (`tools/list`, a catalogue search, a PDA listing, a
derivation). Nothing it touches writes, signs or broadcasts.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from gecko.mcp_client import McpClient, McpError, McpToolError

PARTNER_MCP_URL = os.getenv("GECKO_PARTNER_MCP_URL")

needs_partner = pytest.mark.skipif(
    not PARTNER_MCP_URL,
    reason=(
        "THE PARTNER LANE DID NOT RUN — set GECKO_PARTNER_MCP_URL to a partner's MCP "
        "endpoint to measure against it. Nothing in that lane was proved."
    ),
)


def replying(body: str) -> Any:
    """A transport that answers every request with one canned body."""

    def post(_url: str, _payload: dict[str, Any]) -> str:
        return body

    return post


def rpc(result: Any, request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


# --------------------------------------------------------------- the two wire framings


def test_a_plain_json_body_decodes() -> None:
    client = McpClient("https://example.test/mcp", post=replying(rpc({"tools": []})))
    assert client.list_tools() == []


def test_an_sse_framed_body_decodes() -> None:
    """The same server answers one endpoint with JSON and another with SSE, so the
    client must read both rather than assume the one it was written against."""
    body = "event: message\ndata: " + rpc({"tools": [{"name": "search"}]}) + "\n\n"
    client = McpClient("https://example.test/mcp", post=replying(body))
    assert [t["name"] for t in client.list_tools()] == ["search"]


def test_the_last_data_frame_wins() -> None:
    """A stream may carry progress notifications ahead of the result. Taking the FIRST
    frame would return a notification as though it were the answer."""
    body = (
        "event: message\ndata: "
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
        + "\n\nevent: message\ndata: "
        + rpc({"tools": [{"name": "real"}]})
        + "\n\n"
    )
    client = McpClient("https://example.test/mcp", post=replying(body))
    assert [t["name"] for t in client.list_tools()] == ["real"]


# ------------------------------------------------------------------ refusing to guess


@pytest.mark.parametrize(
    "body",
    ["", "   ", "<html>a proxy error page</html>", "event: ping\n\n"],
    ids=["empty", "whitespace", "html", "sse-with-no-data"],
)
def test_a_body_that_is_not_a_response_raises_rather_than_returning_nothing(
    body: str,
) -> None:
    """An unreadable answer must not reduce to an empty result. A measurement that
    counts "the proxy returned HTML" as "the surface offers no tools" is worse than one
    that stops."""
    client = McpClient("https://example.test/mcp", post=replying(body))
    with pytest.raises(McpError):
        client.list_tools()


def test_a_jsonrpc_error_carries_the_servers_own_message() -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}
    )
    client = McpClient("https://example.test/mcp", post=replying(body))
    with pytest.raises(McpError, match="bad params"):
        client.list_tools()


def test_a_result_without_a_tool_list_is_not_an_empty_tool_list() -> None:
    client = McpClient("https://example.test/mcp", post=replying(rpc({})))
    with pytest.raises(McpError):
        client.list_tools()


# ------------------------------------------------------------------------- call_tool


def test_call_tool_returns_the_text_content() -> None:
    body = rpc(
        {
            "content": [
                {"type": "text", "text": "two lines"},
                {"type": "text", "text": "of it"},
            ]
        }
    )
    client = McpClient("https://example.test/mcp", post=replying(body))
    assert client.call_tool("anything", {}) == "two lines\nof it"


def test_a_tool_error_is_a_DIFFERENT_exception_from_a_transport_error() -> None:
    """For a measurement these mean opposite things: a transport error says we could not
    ask, a tool error says the surface answered "no" — which is a RESULT. Collapsing them
    would let an outage read as a finding about the surface."""
    body = rpc(
        {"isError": True, "content": [{"type": "text", "text": "program not found"}]}
    )
    client = McpClient("https://example.test/mcp", post=replying(body))
    with pytest.raises(McpToolError, match="program not found"):
        client.call_tool("search_programs", {"programId": "nope"})
    assert issubclass(McpToolError, McpError)


def test_each_request_carries_a_fresh_id() -> None:
    seen: list[int] = []

    def post(_url: str, payload: dict[str, Any]) -> str:
        seen.append(payload["id"])
        return rpc({"tools": []})

    client = McpClient("https://example.test/mcp", post=post)
    client.list_tools()
    client.list_tools()
    assert seen == [1, 2], "a reused id makes two calls indistinguishable in a stream"


def test_the_request_is_a_wellformed_jsonrpc_envelope() -> None:
    captured: dict[str, Any] = {}

    def post(_url: str, payload: dict[str, Any]) -> str:
        captured.update(payload)
        return rpc({"content": []})

    McpClient("https://example.test/mcp", post=post).call_tool("t", {"a": 1})
    assert captured["jsonrpc"] == "2.0"
    assert captured["method"] == "tools/call"
    assert captured["params"] == {"name": "t", "arguments": {"a": 1}}


# ------------------------------------------------- the live lane (opt in, read-only)


@pytest.mark.partner_mcp
@needs_partner
def test_the_partner_surface_answers_and_names_its_tools() -> None:
    tools = McpClient(str(PARTNER_MCP_URL)).list_tools()

    assert tools, "a partner surface with no tools is not a surface"
    for tool in tools:
        assert tool.get("name"), f"a tool with no name cannot be called: {tool!r}"
        assert tool.get("inputSchema"), (
            f"{tool.get('name')!r} publishes no input schema — an agent would have to "
            "guess its arguments, which is the failure this project exists to remove"
        )
