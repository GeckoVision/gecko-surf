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
from .paysh_catalog import CatalogRegistry, ProviderSurface
from .surfaces import SurfaceError, safe_surface_id

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

    # -- resolve (§13 Phase 3.2 — the context7 resolve-library-id analogue) -------
    def enumerate_providers(self) -> list[str]:
        """The fqns of the providers registered in THIS workspace's registry (sorted).

        Scoped to what the operator provisioned — NEVER a hosted/global public catalog.
        The `surfaces.py` non-goal holds: exposing a global ``ids()`` would make us a
        marketplace; this only enumerates the workspace's own connected providers, the
        enumerate half of the resolve step."""
        return sorted(ps.entry.fqn for ps in self.registry.providers())

    def resolve_provider(self, name: str) -> ProviderSurface | None:
        """Resolve a provider by human name to its comprehended surface, or None.

        Deterministic, no fuzzy guessing: an exact fqn, then an exact tool slug, then a
        unique slug match against the tool name / fqn / title. An unknown or AMBIGUOUS
        name returns None (never a marketplace dump). Resolution is scoped to the
        workspace registry — the context7 ``resolve-library-id`` analogue, but over the
        providers this workspace connected, not a global index."""
        if not name or not name.strip():
            return None
        exact = self.registry.get(name) or self.registry.by_tool(name)
        if exact is not None:
            return exact
        try:
            slug = safe_surface_id(name)
        except SurfaceError:
            return None
        by_slug = self.registry.by_tool(slug)
        if by_slug is not None:
            return by_slug
        matches = [
            ps
            for ps in self.registry.providers()
            if slug in (safe_surface_id(ps.entry.fqn), safe_surface_id(ps.entry.title))
        ]
        return matches[0] if len(matches) == 1 else None

    def _surfaces(self) -> list[Any]:
        """Wrap every connected provider client that carries a call graph as a Surface.

        Getattr-guarded (guardrail for duck-typed clients): a registry client with no
        ``surface_graph`` — a test fake, a not-yet-comprehended stub — is skipped rather
        than raised on, so the aggregator degrades gracefully instead of crashing the
        whole cross-provider query for one odd provider."""
        from .surface import Surface

        return [
            Surface.of(ps.client)
            for ps in self.registry.providers()
            if getattr(ps.client, "surface_graph", None) is not None
        ]

    # -- the value-domain index over every connected provider (§13 Phase 3.3) -----
    def value_domain_index(self) -> Any:
        """The ``entity -> {producers, consumers}`` index across every connected
        provider — the cross-provider "what correlates with X" lookup. Deterministic,
        control-plane only. Returns a :class:`~gecko.vindex.ValueDomainIndex`."""
        from .vindex import value_domain_index

        return value_domain_index(self._surfaces())

    # -- the aggregator correlation tools (§13 Phase 2b / Phase 4) -----------------
    def correlate_surfaces(self, a: str, b: str) -> dict[str, Any]:
        """Correlate two named/resolved providers in THIS workspace — which of A's
        outputs feed B's inputs, each with a provenance-carrying basis and whether it is
        plan-eligible now or a quarantined candidate.

        Resolves both names through :meth:`resolve_provider` (deterministic, no fuzzy
        guessing) and delegates to the shared Phase-2 ``correlate.correlate_surfaces`` —
        the SAME confirmed-gate scorer as everywhere else, so a provider-only declaration
        can never reach a plan-eligible cross-API join through this door. Returns the
        control-plane-clean ``CorrelationResult.to_dict()`` (structure only, never a
        payload). Raises :class:`~gecko.caller.CallError` on an unknown/ambiguous name."""
        from .correlate import correlate_surfaces as _correlate
        from .surface import Surface

        ra = self.resolve_provider(a)
        rb = self.resolve_provider(b)
        unresolved = [n for n, r in ((a, ra), (b, rb)) if r is None]
        if unresolved:
            raise CallError(f"unknown or ambiguous provider(s): {unresolved!r}")
        assert ra is not None and rb is not None  # narrowed by the guard above
        return _correlate(Surface.of(ra.client), Surface.of(rb.client)).to_dict()

    def find_correlations(self, entity: str) -> dict[str, Any]:
        """The cross-provider query — "what correlates with ``entity`` across every
        connected provider." Groups the producers/consumers of the DECLARED value-domain
        entity and reports each cross-provider join tagged plan-eligible vs candidate via
        the SHARED Phase-2 scorer. Control-plane clean: names + entity + provenance only,
        never a response value."""
        idx = self.value_domain_index()
        group = idx.group(entity)
        return {
            "entity": entity,
            "group": group.to_dict() if group is not None else None,
            "cross_joins": [j.to_dict() for j in idx.find_correlations(entity)],
        }

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
        # The cross-provider correlation doors (hidden: callable by name, NOT enumerated
        # in list_tools — the projection stays byte-identical). Siblings of get_capability:
        # dispatched by name, resolved in the package (correlate/vindex), control-plane
        # only, and never reaching an upstream call.
        if name == "correlate_surfaces":
            return self.correlate_surfaces(
                str(args.get("a", "")), str(args.get("b", ""))
            )
        if name == "find_correlations":
            return self.find_correlations(str(args.get("entity", "")))
        if name == "value_domain_index":
            return self.value_domain_index().to_dict()
        ps = self.registry.by_tool(name)
        if ps is None:
            raise CallError(f"unknown tool: {name!r}")
        return ps.client.call(name, args, mode=self.mode)


__all__ = ["CatalogMcpSurface"]
