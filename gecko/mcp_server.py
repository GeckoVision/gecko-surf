"""MCP surface — what an agent actually installs.

`McpSurface` is a framework-agnostic, fully testable view (list_tools / call_tool)
over an AgentApiClient. It adds one synthetic tool — `search_capabilities` — so an
agent can go from natural-language intent to the right endpoint, then call it.

The optional `serve_stdio()` wraps it with the `mcp` SDK for a real server; it's
import-guarded so the surface (and its tests) work without the SDK installed.
"""

from __future__ import annotations

from typing import Any

from .client import AgentApiClient
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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search_capabilities":
            hits = self.client.search(arguments.get("query", ""))
            # Observe, never mutate: usage metadata only (result breadth k), never the query.
            emit_surf_event(
                "surf.search", surface_id=self.client.surface_id, k=len(hits)
            )
            return hits
        result = self.client.call(name, arguments, mode=self.mode)
        emit_surf_event(
            "surf.call",
            surface_id=self.client.surface_id,
            tool_name=name,
            mode=self.mode,
        )
        return result


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
