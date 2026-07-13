"""Aggregated MCP surface over the whole pay.sh catalog — one MCP, 70 providers.

:class:`CatalogMcpSurface` is a framework-agnostic view (``list_tools`` / ``call_tool``)
over a :class:`~gecko.paysh_catalog.CatalogRegistry`. It duck-types as a Gecko surface, so
it drops straight into the EXISTING transports — ``serve_stdio`` and ``build_http_app``
(via ``_surface_from``) — with no changes to either.

Because ``caller.build_request`` is single-host, the registry holds one pinned client per
provider; this surface aggregates them:
  * ``list_tools`` — the synthetic ``search_capabilities`` + one lightweight ref per
    provider (scale-friendly for ~70 tools; the full schema is served on demand).
  * ``search_capabilities`` — ranks intent -> provider ACROSS the whole catalog and
    returns the full callable def (+ price / free-tier / verification status) so the agent
    calls first try.
  * ``call_tool`` — routes a tool name to its owning provider's client (the ONLY object
    that knows that provider's host + correct shape).

Aggregate, never replace: this consumes pay.sh's catalog as an input. pay.sh keeps 100%,
x402 settles direct, and pay.sh's own MCP (if any) is untouched and runs side by side.
Default mode is ``recorded`` ($0, stub) — this surface NEVER triggers a live payment.
"""

from __future__ import annotations

from typing import Any

from .caller import CallError
from .mcp_server import _SEARCH_TOOL, to_lightweight_ref
from .modes import CallMode
from .paysh_catalog import CatalogRegistry

# How many providers a cross-catalog search returns. Small: the agent wants the few most
# relevant providers, not a marketplace dump.
_TOP_K = 8


class CatalogMcpSurface:
    """One MCP surface aggregating every comprehended provider in a ``CatalogRegistry``."""

    surface_id = "paysh-catalog"

    def __init__(self, registry: CatalogRegistry, mode: CallMode = "recorded") -> None:
        self.registry = registry
        self.mode = mode

    def _provider_tools(self) -> list[tuple[Any, dict[str, Any]]]:
        """(provider_surface, full_tool_def) for every usable provider tool."""
        out: list[tuple[Any, dict[str, Any]]] = []
        for ps in self.registry.providers():
            for tool in ps.client.list_tools():
                out.append((ps, tool))
        return out

    def list_tools(self) -> list[dict[str, Any]]:
        """The MCP ``tools/list`` view: the search tool + one lightweight ref per provider.

        At ~70 tools, dumping full defs would blow the context budget, so each provider is a
        ref (name + one-line summary + minimal schema); the agent resolves the full schema
        via ``search_capabilities`` / ``get_capability`` before calling by name."""
        tools = [_SEARCH_TOOL]
        for _ps, tool in self._provider_tools():
            tools.append(to_lightweight_ref(tool))
        return tools

    def search_capabilities(self, query: str) -> list[dict[str, Any]]:
        """Rank intent -> provider across the WHOLE catalog and return full callable defs.

        Each provider is a single-host client with its own lexical catalog (enriched with
        the pay.sh description/use_case); we score the query against each and merge. Only
        providers with genuine lexical overlap surface (an out-of-scope intent returns
        nothing), so the agent isn't handed a marketplace dump."""
        ranked: list[tuple[float, Any, Any]] = []
        for ps in self.registry.providers():
            hits = ps.client.search_scored(query, 1)
            if hits:
                ranked.append((hits[0].score, ps, hits[0]))
        ranked.sort(key=lambda r: (-r[0], r[1].entry.title.lower()))
        results: list[dict[str, Any]] = []
        for _score, ps, hit in ranked[:_TOP_K]:
            full = ps.client.get_tool(ps.tool_name)
            results.append(
                {
                    "name": ps.tool_name,
                    "provider": ps.entry.fqn,
                    "summary": hit.summary,
                    "method": hit.method,
                    "path": hit.path,
                    "host": ps.host,
                    "comprehension": ps.status,
                    "price_usd": [ps.entry.min_price_usd, ps.entry.max_price_usd],
                    "has_free_tier": ps.entry.has_free_tier,
                    "inputSchema": full["inputSchema"],
                }
            )
        return results

    def get_capability(self, name: str) -> dict[str, Any]:
        """Fetch one provider tool's full callable def by name (progressive disclosure)."""
        ps = self.registry.by_tool(name)
        if ps is None:
            raise CallError(f"unknown tool: {name!r}")
        return ps.client.get_tool(name)

    def call_tool(
        self, name: str, arguments: dict[str, Any], session_id: str | None = None
    ) -> Any:
        """Invoke a tool, routing to the owning provider's client. ``session_id`` is
        accepted (HTTP surface passes it) but unused — this surface has no per-session
        state and never touches the upstream call with it."""
        args = arguments or {}
        if name == "search_capabilities":
            return self.search_capabilities(str(args.get("query", "")))
        if name == "get_capability":
            return self.get_capability(str(args.get("name", "")))
        ps = self.registry.by_tool(name)
        if ps is None:
            raise CallError(f"unknown tool: {name!r}")
        return ps.client.call(name, args, mode=self.mode)


__all__ = ["CatalogMcpSurface"]
