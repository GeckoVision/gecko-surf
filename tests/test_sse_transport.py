"""The legacy HTTP+SSE transport, mounted alongside Streamable HTTP.

Not nostalgia — distribution. The hosted chat products (Grok, ChatGPT, Claude on the web)
accept a remote MCP server through the two-endpoint SSE shape, so without it Gecko cannot
be added inside the window where people already are.

These assert the SHAPE — both doors exist, both halves of the two-endpoint transport are
mounted, and losing the extra door never closes the main one. They deliberately do not
open a live stream: an SSE connection stays open by design, so a test that reads one
blocks forever. Proving the stream carries real MCP traffic is an integration concern,
and the honest place for it is a client actually connecting.
"""

from __future__ import annotations

from typing import Any

from gecko import http_server


def _app() -> Any:
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec

    spec = load_spec("gecko/examples/jupiter_swap_openapi.json")
    return http_server.build_http_app(AgentApiClient(spec))


def _paths(app: Any) -> set[str]:
    return {(getattr(r, "path", "") or "").rstrip("/") for r in app.routes}


def test_streamable_http_is_still_the_default_door() -> None:
    """SSE is ADDITIONAL. Replacing the modern transport to please an old client would
    trade one compatibility problem for another."""
    assert http_server.MCP_PATH.rstrip("/") in _paths(_app())


def test_the_sse_stream_endpoint_is_mounted() -> None:
    assert http_server.SSE_PATH.rstrip("/") in _paths(_app())


def test_the_client_half_is_mounted_too() -> None:
    """SSE is two-endpoint: the stream comes down `/sse`, the client's own messages go up
    to the message path. Mounting only the stream looks right and never works."""
    assert http_server.SSE_MESSAGE_PATH.rstrip("/") in _paths(_app())


def test_the_two_sse_routes_come_as_a_pair() -> None:
    from unittest.mock import Mock

    routes = http_server._sse_routes(Mock(), None)

    assert len(routes) == 2
    assert {(getattr(r, "path", "") or "").rstrip("/") for r in routes} == {
        http_server.SSE_PATH.rstrip("/"),
        http_server.SSE_MESSAGE_PATH.rstrip("/"),
    }


def test_losing_the_extra_door_never_closes_the_main_one(monkeypatch: Any) -> None:
    """An `mcp` without SseServerTransport must still boot the server on Streamable
    HTTP — an optional transport that can take the process down is not optional."""
    monkeypatch.setattr(http_server, "_sse_routes", lambda *_a, **_kw: [])

    paths = _paths(_app())

    assert http_server.MCP_PATH.rstrip("/") in paths
    assert http_server.SSE_PATH.rstrip("/") not in paths
