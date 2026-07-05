"""MCP surface — what an agent actually installs.

`McpSurface` is a framework-agnostic, fully testable view (list_tools / call_tool)
over an AgentApiClient. It adds one synthetic tool — `search_capabilities` — so an
agent can go from natural-language intent to the right endpoint, then call it.

The optional `serve_stdio()` wraps it with the `mcp` SDK for a real server; it's
import-guarded so the surface (and its tests) work without the SDK installed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .client import AgentApiClient
from .comprehend_service import (
    ComprehendError,
    comprehend_submission,
    ensure_submittable,
)
from .events import emit_surf_event

_SEARCH_TOOL = {
    "name": "search_capabilities",
    "description": "Find which endpoint/tool fits a natural-language intent. Returns ranked tool names you can then call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in plain language.",
            }
        },
        "required": ["query"],
    },
}


class McpSurface:
    def __init__(self, client: AgentApiClient, mode: str = "recorded"):
        self.client = client
        self.mode = mode

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [_SEARCH_TOOL]
        for t in self.client.list_tools():
            tools.append({k: t[k] for k in ("name", "description", "inputSchema")})
        return tools

    def call_tool(
        self, name: str, arguments: dict[str, Any], session_id: str | None = None
    ) -> Any:
        """Invoke a tool. ``session_id`` (the MCP transport session, when the caller
        is the HTTP surface) is threaded onto the usage event ONLY as an opaque
        correlation token — it joins connect->call for the retention funnel and is
        sanitized by ``emit_surf_event``; it never touches the upstream call."""
        if name == "search_capabilities":
            hits = self.client.search(arguments.get("query", ""))
            # Observe, never mutate: usage metadata only (result breadth k), never the query.
            emit_surf_event(
                "surf.search",
                surface_id=self.client.surface_id,
                k=len(hits),
                session_id=session_id,
            )
            return hits
        result = self.client.call(name, arguments, mode=self.mode)
        emit_surf_event(
            "surf.call",
            surface_id=self.client.surface_id,
            tool_name=name,
            mode=self.mode,
            session_id=session_id,
        )
        return result


_COMPREHEND_TOOL = {
    "name": "comprehend_api",
    "description": (
        "Submit an API's OpenAPI URL (or a human docs page URL with from_docs=true) and "
        "get it comprehended into first-call-correct agent tools — no integration code. "
        "Returns the API name, its usable tools, agent-native artifacts (llms.txt / "
        "gecko.json / tools.md), and self-host next steps. Comprehends and returns to YOU "
        "only: it does not host, publicly list, or register your API."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The API's OpenAPI spec URL (or a docs page URL if from_docs).",
            },
            "from_docs": {
                "type": "boolean",
                "description": (
                    "Recover the surface from a human docs page instead of an OpenAPI "
                    "spec. Results are quarantined pending review."
                ),
                "default": False,
            },
        },
        "required": ["url"],
    },
}


class MetaComprehendSurface:
    """A minimal synthetic MCP surface with ONE tool: ``comprehend_api``.

    The agent-facing door to the same core the HTTP ``POST /comprehend`` route calls
    (one engine, two front doors). An agent submits an API URL and gets first-call-correct
    tools back — comprehended FOR THE CALLER ONLY.

    MVP scope — comprehend-and-return only. It deliberately does NOT host, publicly list,
    or register the submitted API: ephemeral hosting is an explicit later tier and public
    listing is a hard non-goal (no public catalog). It carries no ``AgentApiClient``, so
    it is not wrapped in :class:`McpSurface`; the HTTP layer duck-types it as a surface.
    """

    surface_id = "gecko-meta"

    def list_tools(self) -> list[dict[str, Any]]:
        return [_COMPREHEND_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "comprehend_api":
            raise ComprehendError(f"unknown tool: {name}")
        url = arguments.get("url", "")
        if not isinstance(url, str) or not url:
            raise ComprehendError("comprehend_api requires a 'url' argument")
        ensure_submittable(url)  # remote door: http(s) only, no local file read
        result = comprehend_submission(
            url, from_docs=bool(arguments.get("from_docs", False))
        )
        return asdict(result)


def serve_stdio(
    spec: str, base_url: str | None = None, mode: str = "recorded"
) -> None:  # pragma: no cover
    """Run a real MCP stdio server (requires the `mcp` package)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Install the `mcp` package to run the stdio server: uv add mcp"
        ) from exc

    surface = McpSurface(AgentApiClient(spec, base_url=base_url), mode=mode)
    server = FastMCP("gecko")
    for tool in surface.list_tools():

        def _make(tool_name):
            def _handler(**kwargs):
                return surface.call_tool(tool_name, kwargs)

            return _handler

        server.add_tool(
            _make(tool["name"]), name=tool["name"], description=tool["description"]
        )
    server.run()
