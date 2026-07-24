"""The Agent Surface — one named artifact for the thing Gecko projects.

Gecko does not hand an agent a Swagger, an OpenAPI, or an MCP tool list and hope. It
derives — and hands over — a **Surface**: the deterministic, provenance-carrying,
safety-checked *call graph* an agent traverses to make the right call the first time.

That artifact already exists; before this module it was assembled ad-hoc from four places:

    the call graph + per-edge provenance   gecko.graph      (SurfaceGraph, Plan)
    the question-shaped tools              gecko.tools       (via AgentApiClient)
    the safety verdict                     gecko.sanitize    (per-tool quarantine)
    the agent-native projections           gecko.agentnative (llms.txt / gecko.json / …)

``Surface`` is the **façade that names them as one object**. It is behavior-preserving: it
holds an :class:`~gecko.client.AgentApiClient` and delegates — it invents no new inference,
no new wire format, and changes nothing about the engine. Its whole job is legibility: a
reader (or a caller, or a pitch) meets *the Surface*, not four modules.

Three layers, only one of which is ours:

    shape       what endpoints/fields exist          OpenAPI, GraphQL   (input)
    transport   how to invoke one tool               MCP                (wire)
    SURFACE     given intent: which chain, in what    ← this            (derived)
                order, at what confidence, from what
                basis — and is it safe

Shape and transport are probabilistic *at the moment of use* (the agent still guesses which
op, which order, whether to trust the spec). The Surface is the deterministic answer, so the
model does not have to guess. It **composes on** OpenAPI (as input) and MCP (as transport);
it never replaces them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .agentnative import build_artifacts
from .client import AgentApiClient
from .graph import SurfaceGraph

#: The agent-native projections a Surface can emit. One derived artifact, N shapes —
#: the same Surface rendered for whoever is reading (an llms.txt crawler, a gecko.json
#: consumer, a human skimming tools.md, an MCP client).
Projection = Literal[
    "llms.txt", "gecko.json", ".well-known/gecko.json", "tools.md", "SKILL.md"
]


@dataclass(frozen=True)
class SafetyVerdict:
    """The Surface's anti-poisoning stance, read from the client's per-tool quarantine.

    An ingested spec is untrusted input; a tool whose spec text trips the sanitizer is
    quarantined — it loses auth injection and cannot be called with credentials, while its
    clean siblings keep working (per-tool blast radius, not per-surface). This value object
    is the *reportable* form of that stance: what a reviewer or a compose partner (a policy
    engine, a human-approval gate) reads to decide.
    """

    total_tools: int
    quarantined: tuple[str, ...]  # tool names withheld from credentialed calls

    @property
    def clean(self) -> bool:
        """True when no tool is quarantined — the whole surface is safe to inject auth for."""
        return not self.quarantined

    @property
    def all_quarantined(self) -> bool:
        """The degenerate case: every tool tripped the sanitizer (the whole spec is hostile)."""
        return self.total_tools > 0 and len(self.quarantined) == self.total_tools


@dataclass(frozen=True)
class Surface:
    """The Agent Surface for one API — a single handle over the derived artifact.

    Construct with :meth:`from_spec` (the common path) or wrap an existing client with
    :meth:`of`. Every method delegates to the engine; the value of this class is that the
    call graph, the tools, the safety verdict, and the projections are reachable through
    ONE noun instead of four modules.
    """

    client: AgentApiClient

    # -- construction ------------------------------------------------------------
    @classmethod
    def from_spec(
        cls,
        spec: str | dict[str, Any],
        *,
        session: Any = None,
        base_url: str | None = None,
        surface_id: str | None = None,
        **kwargs: Any,
    ) -> Surface:
        """Derive a Surface from an OpenAPI spec (a URL, a path, or a parsed dict).

        This is the moment the artifact is *made*: ingest the shape, infer the call graph,
        shape the tools, run the sanitizer. Everything below is a view onto the result.
        """
        return cls(
            AgentApiClient(
                spec,
                session=session,
                base_url=base_url,
                surface_id=surface_id,
                **kwargs,
            )
        )

    @classmethod
    def of(cls, client: AgentApiClient) -> Surface:
        """Wrap an already-built client (e.g. one the MCP layer already holds)."""
        return cls(client)

    # -- identity ----------------------------------------------------------------
    @property
    def surface_id(self) -> str:
        """The namespace that scopes every node id — "" for a single, un-namespaced API."""
        return self.client.surface_id

    # -- the deterministic call graph (the heart of the Surface) -----------------
    @property
    def graph(self) -> SurfaceGraph:
        """The call graph: nodes (ops/params/fields) + edges (consumes/produces/feeds) with
        per-edge provenance (EXTRACTED / DECLARED / INFERRED), confidence, and basis."""
        return self.client.surface_graph

    def plan(self, intent: str, tool: str | None = None) -> dict[str, Any] | None:
        """The deterministic supplier chain for an intent — or ``None`` when the top op's
        inputs are already satisfiable (no chain needed).

        This is the Surface's answer to "which calls, in what order": an ordered plan with
        provenance-carrying ``explain`` on every sourced input. If ``tool`` is omitted, the
        top capability-search hit for ``intent`` is used.
        """
        target = tool or self._top_tool(intent)
        if target is None:
            return None
        return self.client.plan_for(intent, target)

    def _top_tool(self, intent: str) -> str | None:
        hits = self.client.search(intent, limit=1)
        return hits[0]["name"] if hits else None

    # -- the question-shaped tools -----------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        """Question-shaped tool defs (intent → the right op; auth headers hidden)."""
        return self.client.list_tools()

    def search(self, intent: str, limit: int = 5) -> list[dict[str, Any]]:
        """Capability search: intent → the right endpoint(s), ranked."""
        return self.client.search(intent, limit=limit)

    # -- the safety verdict ------------------------------------------------------
    @property
    def safety(self) -> SafetyVerdict:
        """The anti-poisoning stance: which tools are quarantined (untrusted-spec defense)."""
        quarantined = tuple(sorted(self.client._poisoned_tool_names))
        return SafetyVerdict(total_tools=len(self.tools()), quarantined=quarantined)

    # -- the projections (one artifact, N shapes) --------------------------------
    def projections(
        self, *, mcp_url: str | None = None, site_url: str | None = None
    ) -> dict[str, str]:
        """Every agent-native projection: ``{relative_path -> text}``.

        The same derived Surface rendered for each reader — ``llms.txt`` (breadcrumb),
        ``gecko.json`` + ``/.well-known/gecko.json`` (machine manifest), ``tools.md`` (human
        skim), ``SKILL.md`` (a Claude-Code/Cursor skill). One inference, N shapes.
        """
        return build_artifacts(self.client, mcp_url=mcp_url, site_url=site_url)

    def project(
        self,
        kind: Projection,
        *,
        mcp_url: str | None = None,
        site_url: str | None = None,
    ) -> str:
        """A single projection by name (see :data:`Projection`)."""
        arts = self.projections(mcp_url=mcp_url, site_url=site_url)
        if kind not in arts:
            raise KeyError(
                f"unknown projection {kind!r}; available: {', '.join(sorted(arts))}"
            )
        return arts[kind]

    # -- the visual (graphviz for APIs) ------------------------------------------
    def render_svg(self, *, title: str | None = None) -> str:
        """The Surface as an SVG call graph — operations as nodes, feeds as arrows colored
        by provenance. Deterministic, self-contained, control-plane clean (structure only,
        no payloads). The shareable picture of the derived surface."""
        from .surfaceviz import render_svg

        label = title or f"Agent Surface — {self.surface_id or 'api'}"
        return render_svg(self.graph, title=label)


__all__ = ["Projection", "SafetyVerdict", "Surface"]
