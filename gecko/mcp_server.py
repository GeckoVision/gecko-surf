"""MCP surface — what an agent actually installs.

`McpSurface` is a framework-agnostic, fully testable view (list_tools / call_tool)
over an AgentApiClient. It adds synthetic navigation tools — `search_capabilities`
(intent -> the right endpoint), `get_capability` (I know the name, give me its schema)
and `query_docs` (why did my call fail) — so an agent can go from natural-language
intent to a correct first call.

The token split the two doors enforce (see `gecko.scope`):

* **`list_tools` owns BREADTH** — every usable capability stays visible, full defs below
  scale and lightweight refs above (`gecko.scale`). Recall lives here.
* **`search_capabilities` owns DEPTH + ORDER** — one intent gets the ordered `plan` and
  full schemas for exactly the ops that plan names, never the whole surface. It used to
  re-emit the entire below-scale surface on every call (5.1x the connect cost on the
  43-op Pegana P0 fixture, entirely duplicate).

The optional `serve_stdio()` wraps it with the `mcp` SDK for a real server; it's
import-guarded so the surface (and its tests) work without the SDK installed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from collections.abc import Callable, Mapping
from typing import Any

from .client import AgentApiClient
from .comprehend_service import (
    ComprehendError,
    comprehend_submission,
    ensure_submittable,
)
from .enforce import (
    FAIL_CLOSED_SIGNAL,
    EnforceMode,
    apply_gate,
    attach_warning,
    blocked_signals,
    enforce_mode_from_env,
    fail_closed_refusal,
    is_write_method,
    refusal_payload,
)
from .events import emit_surf_event
from .honeypot import (
    HONEYPOT_DECISION,
    HONEYPOT_REASON,
    decoy_tool_defs,
    honeypot_refusal,
    honeypots_from_env,
    is_decoy,
)
from .modes import CallMode
from .risk import RiskAssessment, RiskPolicy, assess_from_client, policy_from_client
from .scope import RETRIEVAL_MAX_TOOLS, build_scope
from .search import project_hits
from .toolerror import ensure_known_tool, tool_result_payload
from .tools import tool_annotations

logger = logging.getLogger("gecko.mcp_server")


def _question_of(arguments: Mapping[str, Any]) -> str:
    """The caller's question, under either name it may have used.

    `search_capabilities` advertises ``query`` and `query_docs`/`find_start` advertise
    ``intent`` — same concept, two spellings, on the same surface. An agent that learns
    one and reuses it hits a validation error on its next call. Rather than rename a
    published field (which breaks callers who got it right), both are accepted
    everywhere and neither is required, so whichever the agent reaches for works.
    """
    for key in _QUESTION_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


#: The names this surface accepts for "the caller's question". Both are advertised in
#: every schema that takes one, so an agent that learns either keeps working.
_QUESTION_KEYS = ("query", "intent")


def question_error(arguments: Mapping[str, Any]) -> str | None:
    """Why a question-taking call cannot proceed, or ``None`` if it can.

    Making `query` optional (so `intent` would also work) had a consequence nobody
    intended: EVERY name started "working". `search_capabilities(goal="...")` returned a
    successful, unranked dump of the whole catalog — top hits `root`, `live`, `ready` —
    with nothing telling the agent its argument had been ignored. A liveness probe
    presented as the best answer to a real question.

    That is the same shape as a check that could not run rendering as a check that
    passed, and it is worse here because it is the ENTRY tool: an agent whose first call
    silently misfires has no reason to make a second.

    So the schema stays permissive (either name is accepted) and the HANDLER is strict:
    an unrecognised argument name is named, not ignored, and an absent question says what
    it needs instead of dumping the surface.
    """
    if any(
        isinstance(arguments.get(key), str) and arguments[key].strip()
        for key in _QUESTION_KEYS
    ):
        return None
    supplied = sorted(k for k in arguments if k not in _QUESTION_KEYS)
    if supplied:
        return (
            f"unknown argument {supplied[0]!r} — this tool takes your question as "
            f"`query` (or `intent`). Nothing was searched."
        )
    return (
        "needs your question as `query` (or `intent`), e.g. "
        'query="is USDC pegged right now". Nothing was searched.'
    )


_SEARCH_TOOL = {
    "name": "search_capabilities",
    "annotations": tool_annotations(read_only=True, title="Search capabilities"),
    "description": (
        "Find which endpoint/tool fits a natural-language intent. Returns "
        "{plan, tools}: `tools` = full schemas for just the ops that answer it; "
        "`plan` (when present) = the order to call them in and the field feeding "
        "each next step."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in plain language.",
            },
            # `intent` is the same field under the name the sibling tools use. Two tools
            # on ONE surface asking for a question under two names is a first-call
            # failure we shipped in the product whose whole claim is first-call-correct:
            # an agent that succeeds here with `query` then calls query_docs/find_start
            # with `query` and gets a validation error. Accept both, keep `query`
            # canonical (it is what the published schema has always advertised).
            "intent": {
                "type": "string",
                "description": "Alias of `query` — same thing, either name works.",
            },
        },
        "required": [],
    },
}


_QUERY_DOCS_TOOL = {
    "name": "query_docs",
    "annotations": tool_annotations(read_only=True, title="Query the API docs"),
    "description": (
        "Search the comprehended API's virtualized docs (spec-derived summaries, "
        "params, and agent-native artifacts) to understand WHY a call failed and how "
        "to rewrite it. Returns doc snippets + the relevant tool's inputSchema. "
        "Control-plane only: no auth, no payloads."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "What you were trying to do (or the error you hit), in plain language.",
            },
            "query": {
                "type": "string",
                "description": "Alias of `intent` — same thing, either name works.",
            },
        },
        "required": [],
    },
}


_GET_CAPABILITY_TOOL = {
    "name": "get_capability",
    "annotations": tool_annotations(read_only=True, title="Get one capability"),
    "description": (
        "Fetch ONE tool's full callable schema by name. Use this whenever you already "
        "know which tool you want (e.g. from a tools/list entry) — one schema, no "
        "ranking. Cheaper than search_capabilities; that one is for finding a tool, "
        "this one is for reading its contract."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The exact tool name, as listed.",
            },
        },
        "required": ["name"],
    },
}


# The lightweight-ref hint: an above-scale list entry keeps only enough for the agent to
# know the tool EXISTS and how to get its real schema. This exact suffix is asserted by the
# projection tests — keep it stable.
#
# It points at ``get_capability``, not ``search_capabilities``: the agent reading a ref
# ALREADY KNOWS the name it wants, so re-ranking an intent to recover one schema is the
# expensive door (measured on the 159-op Privy surface: 9,086 B of get_capability calls vs
# 36,070 B of searches for the same three known schemas — 4.0x).
#
# Deliberately SHORTER than the hint it replaces: it is repeated once per ref, so on a
# 159-op surface every character is paid 159 times. The argument name is not spelled here
# because ``get_capability`` is now enumerated with its own inputSchema.
_REF_HINT = "call get_capability for the full schema"


def to_lightweight_ref(tool: dict[str, Any]) -> dict[str, Any]:
    """Project a full agent-facing tool def down to a lightweight MCP reference.

    Above scale, dumping every full tool def into ``tools/list`` blows the token budget and
    evaporates Gecko's O(1)-at-scale advantage. A ref keeps only ``{name, description,
    inputSchema}`` — a one-line summary plus a minimal VALID MCP inputSchema — and tells the
    agent to fetch the real schema via ``search_capabilities`` before calling by name. It is
    control-plane safe by construction: no auth fields, no ``_invoke``, no payload — only the
    name and a summary line.
    """
    summary = str(tool.get("description", "")).strip().splitlines()
    head = summary[0].strip() if summary else ""
    description = f"{head} — {_REF_HINT}" if head else _REF_HINT
    return {
        "name": tool["name"],
        "description": description,
        # Minimal valid MCP inputSchema — a permissive object. The real parameter schema is
        # deliberately withheld from the list and served on demand via search_capabilities.
        "inputSchema": {"type": "object"},
    }


class McpSurface:
    def __init__(
        self,
        client: AgentApiClient,
        mode: CallMode = "recorded",
        *,
        enforce: EnforceMode | None = None,
        policy: RiskPolicy | None = None,
        honeypots: bool | None = None,
        recorded_ops: frozenset[str] = frozenset(),
    ):
        """``enforce`` sets the call-time risk gate stance (block | warn | off); ``None``
        resolves ``GECKO_ENFORCE`` (default: warn — a bare surface only observes). The
        HOSTED builders inject ``block`` explicitly. ``policy`` is the auto-derived
        allowed-tools + trusted-hosts set; ``None`` derives it lazily from the client's
        comprehension on first assessment (the operator only tunes thresholds).

        ``honeypots`` opts IN to the decoy tripwire (``None`` resolves ``GECKO_HONEYPOTS``,
        default OFF). It is a DETECTION layer, not a moat — off by default so a real
        surface never shows fake tools unless the operator asks; when off, ``list_tools``
        is byte-identical to a surface with no honeypot layer.

        ``recorded_ops`` is the per-op mode override: tool names listed here stay
        RECORDED even when the surface ``mode`` is live. It is the catalog-not-the-relay
        boundary — a money-moving write (e.g. Jito ``sendBundle`` / ``sendTransaction``)
        is comprehended and served as a tool, but its response is SYNTHESIZED from the
        schema and NEVER relayed to the wire, so this public endpoint can't become an open
        broadcaster. Default empty set -> every call uses ``self.mode`` (byte-identical to
        before)."""
        self.client = client
        self.mode = mode
        self.enforce: EnforceMode = (
            enforce if enforce is not None else enforce_mode_from_env()
        )
        self._policy = policy
        self.honeypots: bool = (
            honeypots if honeypots is not None else honeypots_from_env()
        )
        self.recorded_ops = recorded_ops

    @property
    def instructions(self) -> str:
        """What the client shows the model BEFORE any tool is chosen.

        Twelve of thirteen hosted mounts sent ``instructions: null`` (measured
        2026-08-31) — the only text a cold agent gets before tool choice, absent
        on nearly every surface. Generated here from the comprehension so it can
        never drift from what the surface actually serves. The template teaches
        the ORDER (search → get → call, query_docs on failure), the auth rule
        (server-side, never ask the user for a key), the mode honestly (recorded
        responses are synthesized, $0, not live data), and that a refusal is an
        answer, not a malfunction.
        """
        try:
            tool_count = len(self.client.list_tools())
        except Exception:  # noqa: BLE001 - instructions must never break connect
            tool_count = 0
        name = self.client.surface_id
        if self.mode == "recorded" and not self.recorded_ops:
            mode_clause = (
                "Responses on this surface are RECORDED: synthesized from the "
                "API's own schema, $0, shape-correct but not live data — say so "
                "if the user asks for current values."
            )
        elif self.recorded_ops:
            mode_clause = (
                "Calls are LIVE against the real API, except the operations the "
                "catalog serves RECORDED (money-moving writes are synthesized "
                "from schema and never relayed to the wire)."
            )
        else:
            mode_clause = "Calls are LIVE against the real API."
        return (
            f"This server is {name}, comprehended by Gecko into {tool_count} "
            "first-call-correct tools. THE ORDER: (1) call `search_capabilities` "
            "with your goal in plain language (`query`); it returns full schemas "
            "for exactly the operations that answer it, and when a `plan` is "
            "present it is the order to call them in — follow it rather than "
            "guessing a tool from its name. (2) Already know the tool name? "
            "`get_capability` is cheaper than searching. (3) A call failed? Call "
            "`query_docs` with the error — it answers why and how to rewrite; do "
            "not retry unchanged. Auth is handled server-side: never ask the "
            "user for an API key and never put credentials in arguments. "
            f"{mode_clause} A result with `isError` or a blocked/refused verdict "
            "is an ANSWER, not a malfunction: read the reason, tell the user, do "
            "not route around it."
        )

    def list_tools(
        self,
        *,
        session_id: str | None = None,
        user_agent: str | None = None,
        client_kind: str | None = None,
        client: str | None = None,
    ) -> list[dict[str, Any]]:
        """The MCP ``tools/list`` view.

        Below scale (``client.surface_all``) this is BYTE-IDENTICAL to the pre-projection
        behaviour: the full search tool followed by a full callable def per usable tool. All
        current hosted surfaces are <50 ops, so they are unaffected.

        Above scale, dumping every full def would blow the context budget and evaporate the
        O(1)-at-scale token advantage, so it returns the full ``search_capabilities`` tool +
        one LIGHTWEIGHT REF per usable tool (name + one-line summary + minimal inputSchema).
        The agent enumerates refs -> ``search_capabilities`` for the one it needs -> gets the
        full def -> calls it by name (``call_tool`` resolves any usable tool by name, so a ref
        never makes a tool uncallable).

        Honeypot decoys (opt-in, off by default) are appended LAST so a PROBING agent
        enumerating the surface sees a tempting target; when off, this stays byte-identical
        to a surface with no honeypot layer (at either scale)."""
        usable = self.client.list_tools()
        # The synthetic navigation tools lead every surface: search_capabilities (intent ->
        # endpoint), get_capability (I already know the name, give me its schema) and
        # query_docs (self-heal: WHY a call failed + how to rewrite). All three are full
        # callable defs at either scale — an agent can only use a door it can SEE, and
        # get_capability was callable-by-name but unlisted, so an agent that had not read
        # our docs could not find the cheap door at all.
        synthetic = [_SEARCH_TOOL, _GET_CAPABILITY_TOOL, _QUERY_DOCS_TOOL]
        if self.client.surface_all:
            tools = list(synthetic)
            for t in usable:
                projected = {k: t[k] for k in ("name", "description", "inputSchema")}
                if t.get("annotations") is not None:
                    projected["annotations"] = t["annotations"]
                tools.append(projected)
        else:
            tools = list(synthetic) + [to_lightweight_ref(t) for t in usable]
        # Opt-in only: expose the decoys so a PROBING agent enumerating the surface sees
        # a tempting target. Off by default -> tools stay byte-identical to no honeypots.
        if self.honeypots:
            tools.extend(decoy_tool_defs())
        # The connect->call bridge: an agent that enumerated tools is past connect but
        # may still never call (the comprehension cliff). Emit ONE control-plane-safe
        # funnel event carrying the SAME sanitized correlation fields the other surf
        # events carry (never PII, no tool defs, no payload). Observe, never mutate: the
        # returned list is untouched. Passed None on transports with no request context
        # (stdio / build-time), so the emit is well-formed but uncorrelated there.
        emit_surf_event(
            "surf.list_tools",
            surface_id=self.client.surface_id,
            session_id=session_id,
            user_agent=user_agent,
            client_kind=client_kind,
            client=client,
        )
        return tools

    def get_capability(self, name: str) -> dict[str, Any]:
        """Fetch one tool's full callable def by name — the thin transport wrapper over
        ``client.get_tool`` (dispatch only; all logic is in the package). This is the
        explicit "I already know which tool, give me its full contract" step, so the agent
        recovers the schema the above-scale ``list_tools`` ref projection withholds without
        re-running ``search_capabilities``. Raises ``ToolNotFound`` for an unknown or
        auth-gated-unavailable name."""
        return self.client.get_tool(name)

    def query_docs(self, intent: str) -> dict[str, Any]:
        """Search the surface's virtualized docs for ``intent`` — the self-heal step:
        after a call fails, the agent asks *why* and gets spec-derived doc snippets +
        the relevant tool's inputSchema so it can rewrite. Thin transport wrapper over
        ``docsearch.search_docs`` (all logic lives in the package). Control-plane only:
        the result carries no auth, no ``_invoke`` routing, and no payload/arg value —
        the "filesystem" in the founder's name is a METAPHOR, not a real mount."""
        from .docsearch import search_docs

        return search_docs(self.client, intent)

    def surface_graph_data(self) -> dict[str, Any]:
        """The client's full deterministic call graph as structure-only data — the DATA
        twin of the SVG: operations + provenance-tagged op->op ``feeds`` edges, each with
        join key + provenance + confidence. Control-plane clean (invariant #1): op ids,
        method/path, join-key names, provenance, confidence — never a payload or secret.

        Getattr-guarded: a duck-typed client (catalog aggregator, test fake) that carries
        no ``surface_graph`` gets a safe empty graph instead of a raise."""
        from .surfaceviz import graph_data

        graph = getattr(self.client, "surface_graph", None)
        if graph is None:
            return {
                "operations": [],
                "edges": [],
                "summary": {"operations": 0, "edges": 0},
            }
        return graph_data(graph)

    def surface_graph(self, op: str | None = None) -> dict[str, Any]:
        """The agent-facing, SCOPED view of the deterministic call graph — the first-class
        "give me the graph" door (hidden: callable by name, not enumerated in list_tools).

        Bounded on purpose (token budget / the O(1)-at-scale advantage — a whole-graph edge
        dump on a dense API like Stripe is ~337 edges):

        - ``op`` given -> the NEIGHBORS of that op only (edges where it is the ``from`` or
          the ``to`` — its suppliers + consumers) plus the ordered supplier ``plan`` for it
          (reused from ``client.plan_for``).
        - ``op`` omitted -> a SUMMARY projection: the operation list + the ``summary`` counts,
          but NOT the full edge list. The drop is named honestly in the payload."""
        data = self.surface_graph_data()
        if not op:
            return {
                "operations": data["operations"],
                # Honest drop: the full edge list is withheld to bound tokens; ask per-op.
                "edges": "call get_surface_graph(op=…) for an op's edges",
                "summary": data["summary"],
            }
        neighbors = [
            e for e in data["edges"] if e.get("from") == op or e.get("to") == op
        ]
        result: dict[str, Any] = {
            "op": op,
            "edges": neighbors,
            "summary": {
                "operations": data["summary"]["operations"],
                "edges": len(neighbors),
            },
        }
        # The ordered supplier chain for this op. An empty intent leaves nothing
        # satisfiable from the query, so plan_for returns the FULL supplier plan (not
        # None). Getattr-guarded for duck-typed clients that carry no planner.
        plan_for = getattr(self.client, "plan_for", None)
        if callable(plan_for):
            plan = plan_for("", op)
            if plan is not None:
                result["plan"] = plan
        return result

    def export_arazzo(
        self, op: str, *, title: str = "Gecko derived plan"
    ) -> dict[str, Any]:
        """The derived plan for ``op`` as a portable Arazzo 1.0 document — the handoff
        artifact an agent can hand to any runtime (arazzo-cli, a workflow UI) instead of
        keeping the DAG locked inside this process.

        Thin transport: build the plan with the shipped planner, serialize with the pure
        ``gecko.arazzo.to_arazzo``. Control-plane clean — the document carries operation
        ids, parameter NAMES, per-edge provenance and Arazzo runtime EXPRESSIONS, never a
        value and never an auth header. An op with no confident plan (or a quarantined
        hop) returns a NON-executable refusal document: ``workflows`` is empty, so no
        runtime can execute what Gecko refused. Check ``x-gecko-executable``.

        Getattr-guarded like ``surface_graph_data``: a duck-typed client with no graph
        gets an honest refusal rather than a raise."""
        from .arazzo import to_arazzo
        from .graph import plan as intra_plan

        graph = getattr(self.client, "surface_graph", None)
        if graph is None:
            return to_arazzo(
                None, title=title, refusal_reason="this surface has no graph"
            )
        surface_id = graph.surface_id or "api"
        plan = intra_plan(graph, op, frozenset())
        return to_arazzo(
            plan,
            graphs=(graph,),
            title=title,
            refusal_reason=f"no confident plan for '{op}' on '{surface_id}'",
        )

    def _assess(self, name: str, arguments: dict[str, Any]) -> RiskAssessment | None:
        """Score a call, FAILING OPEN on a scorer bug. Returns the assessment, or ``None``
        (→ treat as allow) if scoring itself raised — a scoring bug must never break the
        product. A *decided* block is still a real block; fail-open only covers the
        "we couldn't score it" case, never a "we scored it dangerous" case."""
        if self._policy is None:
            try:
                self._policy = policy_from_client(self.client)
            except Exception:  # noqa: BLE001 - fail open: can't derive policy -> allow
                logger.warning("risk policy derivation failed; failing open (allow)")
                return None
        try:
            return assess_from_client(self.client, name, arguments, policy=self._policy)
        except Exception:  # noqa: BLE001 - fail open on a scoring bug, never break a call
            logger.warning("risk assessment failed; failing open (allow)")
            return None

    def _is_write_op(self, name: str) -> bool:
        """True iff the named op mutates upstream state — read from the client's OWN
        comprehension, NOT the (possibly-crashed) policy. Used only on the fail-closed
        path (G1/G4): when scoring/policy-derivation raised, a state-changing op is refused
        rather than waved through. An op we can't resolve degrades to read (fail-open) so a
        bare fake client with no operations keeps working — a real hosted client always
        carries its operations, so a real write is always caught."""
        from .tools import tool_name

        for op in getattr(self.client, "operations", None) or []:
            if tool_name(op) == name:
                return is_write_method(getattr(op, "method", "get"))
        return False

    def call_tool(
        self, name: str, arguments: dict[str, Any], session_id: str | None = None
    ) -> Any:
        """Invoke a tool. ``session_id`` (the MCP transport session, when the caller
        is the HTTP surface) is threaded onto the usage event ONLY as an opaque
        correlation token — it joins connect->call for the retention funnel and is
        sanitized by ``emit_surf_event``; it never touches the upstream call."""
        if name == "search_capabilities":
            problem = question_error(arguments)
            if problem is not None:
                # Refuse, loudly. A ranked-looking dump of the whole catalog is worse
                # than an error: the agent cannot tell it asked the wrong thing.
                return {"error": problem}
            query = _question_of(arguments)
            # Prefer the provenance-carrying substrate when the client offers it
            # (``AgentApiClient.search_ranked`` — a pure superset of ``search``): the top
            # hit's ``is_fallback`` feeds the genuine-hit gate below. Duck-typed clients
            # (catalog aggregator, test fakes) keep the frozen ``search`` path; they carry
            # no retrieval provenance, so the gate stays permissive there (and they carry
            # no planner either, so nothing changes).
            ranked_fn = getattr(self.client, "search_ranked", None)
            if callable(ranked_fn):
                # ``search_ranked`` is kept as the ORDERING substrate (genuine lexical
                # hits first, then the below-scale fallback appends) so ``is_fallback`` on
                # the top hit — the genuine-hit gate below — is unchanged. What changes is
                # that its surface-all breadth no longer reaches the agent: ``build_scope``
                # truncates to ``RETRIEVAL_MAX_TOOLS``. surface_all is an ENUMERATION rule
                # that had leaked into retrieval, so every search re-emitted the whole
                # surface the agent already had from connect (measured on the 43-op Pegana
                # P0 fixture: five searches 91,089 B against a 17,766 B connect — 5.1x the
                # connect cost, entirely duplicate). Breadth stays where it belongs, in
                # ``list_tools``, which below scale still shows every usable tool in full.
                ranked = ranked_fn(query, RETRIEVAL_MAX_TOOLS)
                hits = project_hits(ranked)
                top_hit_genuine = bool(ranked) and not ranked[0].is_fallback
            else:
                hits = self.client.search(query, RETRIEVAL_MAX_TOOLS)
                top_hit_genuine = bool(hits)
            # Chain enrichment (§5): when the TOP hit's required inputs are not
            # satisfiable from the intent, the ordered supplier plan + its
            # provenance-carrying explain let the agent chain first-try instead of
            # discovering the sequence by trial and error. A thin projection — all the
            # logic (graph + satisfiability + suppression of trivial plans) lives in the
            # package (client.plan_for). getattr-guarded for duck-typed clients (catalog
            # aggregator, test fakes) that carry no planner.
            #
            # Genuine-hit gate: a fallback top hit is a query-independent ordering
            # artifact (GET-first-then-path), not intent — scoping the answer to a
            # supplier plan there would steer the agent into a chain nobody asked for. A
            # plan rides the top hit ONLY when the lexical arm genuinely corroborated it.
            plan: dict[str, Any] | None = None
            plan_for = getattr(self.client, "plan_for", None)
            if hits and top_hit_genuine and callable(plan_for):
                plan = plan_for(query, hits[0]["name"])
            # Retrieval returns a SCOPE, not a surface: the ordered plan plus full callable
            # schemas for exactly the ops that plan names (strictly smaller than the ranked
            # list AND strictly more informative — it carries the ordering and the join a
            # flat list cannot). With no plan it is the ranked hits, capped. Schemas carry
            # no auth (tool defs hide auth headers, invariant #4) and no ``_invoke``.
            full = {t["name"]: t for t in self.client.list_tools()}
            scope = build_scope(hits, full, plan, limit=RETRIEVAL_MAX_TOOLS)
            # Observe, never mutate: usage metadata only (result breadth k), never the query.
            emit_surf_event(
                "surf.search",
                surface_id=self.client.surface_id,
                k=len(scope["tools"]),
                session_id=session_id,
            )
            return scope

        # Progressive-disclosure fetch-one: resolve a ref to its full callable def. Thin
        # dispatch to the package; not enumerated in list_tools (keeps that projection
        # byte-identical), but callable by name once the agent knows which tool it wants.
        if name == "get_capability":
            return self.get_capability(arguments.get("name", ""))

        # Self-heal: search the virtualized docs so the agent can learn WHY a call
        # failed and rewrite. Sibling of get_capability — dispatched by name, resolved
        # in the package (docsearch), never reaching an upstream call.
        if name == "query_docs":
            problem = question_error(arguments)
            if problem is not None:
                return {"error": problem}
            return self.query_docs(_question_of(arguments))

        # The first-class "give me the graph" door: the deterministic call graph, SCOPED
        # (an op's neighbors + plan, or a summary) to bound tokens. Sibling of
        # get_capability/query_docs — dispatched by name, resolved in the package, never
        # reaching an upstream call; NOT enumerated in list_tools (projection stays
        # byte-identical).
        if name == "get_surface_graph":
            return self.surface_graph(arguments.get("op"))

        # Per-op mode override: a tool named in ``recorded_ops`` stays RECORDED even on a
        # live surface — the catalog-not-the-relay boundary for money-moving writes (Jito
        # sendBundle/sendTransaction). ``eff_mode`` is what flows to the client call and
        # every emitted event for THIS call; the risk gate below still runs unchanged.
        # Default empty set -> eff_mode == self.mode (byte-identical to before).
        eff_mode: CallMode = "recorded" if name in self.recorded_ops else self.mode

        # --- The honeypot tripwire (opt-in): a decoy has no originating operation, so a
        # CALL of one is definitionally hostile probing. Trip BEFORE the normal gate; there
        # is no upstream to invoke (it is a decoy), so nothing is called and no payload is
        # synthesized. Record ONLY the control-plane-safe fingerprint — the sanitized
        # session correlation + the decoy NAME (a code constant) + the code-constant
        # signal — never the args, never a fake output. Detection, not a moat. ----------
        if self.honeypots and is_decoy(name):
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,  # the decoy name is spec-derived, a code constant
                mode=eff_mode,
                decision=HONEYPOT_DECISION,
                reasons=[HONEYPOT_REASON],
                session_id=session_id,
            )
            return honeypot_refusal()

        # --- The enforcement gate: score BEFORE the upstream call, then dispatch through
        # the ONE shared gate (enforce.apply_gate) — no inline block/warn logic here. ----
        assessment = self._assess(name, arguments) if self.enforce != "off" else None
        # Fail CLOSED on a WRITE we could not score (G1/G4): a scorer or policy-derivation
        # crash returns ``None`` (fail-open for reads — a scoring bug must not break a
        # harmless GET), but a state-changing op is refused rather than waved through. A
        # crash-input can no longer turn the fail-open robustness feature into a bypass.
        if assessment is None and self.enforce == "block" and self._is_write_op(name):
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,
                mode=eff_mode,
                decision="block",
                reasons=[FAIL_CLOSED_SIGNAL],
                session_id=session_id,
            )
            return fail_closed_refusal()
        outcome = apply_gate(assessment, self.enforce)
        if outcome.blocked and assessment is not None:
            # Hard block: the upstream API is NEVER called. Emit the countable event
            # (signal NAMES only — never the value-bearing human message) and return a
            # structured refusal the agent can read.
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,
                mode=eff_mode,
                score=assessment.score,
                decision="block",
                reasons=blocked_signals(assessment),
                session_id=session_id,
            )
            return refusal_payload(assessment)

        # Thread the transport session into the client ONLY in probe mode: it keys
        # the per-session sandbox world (synthetic-state isolation) and never touches
        # the upstream call. Conditional on purpose — duck-typed clients (the catalog
        # aggregator, the red-team wrapper) don't accept the kwarg, and no other mode
        # consumes it.
        if eff_mode == "probe":
            result = self.client.call(
                name, arguments, mode=eff_mode, session_id=session_id
            )
        else:
            result = self.client.call(name, arguments, mode=eff_mode)
        # plane="surface": a tool invoked THROUGH the MCP surface. The inner
        # client.call above ALSO emitted its engine-plane outcome event — different
        # planes by design, not double-counting; see events.CallPlane.
        emit_surf_event(
            "surf.call",
            surface_id=self.client.surface_id,
            tool_name=name,
            mode=eff_mode,
            session_id=session_id,
            plane="surface",
        )
        # A step_up (or a warn-mode would-be block) executed — flag it, don't hide it.
        if outcome.warn and assessment is not None:
            return attach_warning(result, assessment)
        return result


_COMPREHEND_TOOL = {
    "name": "comprehend_api",
    "annotations": tool_annotations(
        read_only=True, open_world=True, title="Comprehend an API"
    ),
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

    #: Set by the multi-surface builder AFTER the mount list is known (late
    #: binding beats reordering the builder): a zero-arg callable returning the
    #: PUBLIC surface entries — the same gated-mount withholding the index uses,
    #: so this tool can never leak a surface the index would not.
    surface_index: Callable[[], list[dict[str, str]]] | None = None

    @property
    def instructions(self) -> str:
        """The bare-host landing was a one-tool cul-de-sac with no signage —
        the most likely cold-agent entry (someone pasted the hostname) landed
        here with no way to learn that the real surfaces exist one path over."""
        return (
            "You have connected to Gecko's host root, not to a specific API. "
            "Two tools: `list_surfaces` shows every API served on this host "
            "with its MCP URL — reconnect to the one you need. "
            "`comprehend_api` turns any public OpenAPI URL (or docs page, with "
            "from_docs=true) into agent tools, returned to you only — nothing "
            "is hosted or listed."
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return [_COMPREHEND_TOOL, _LIST_SURFACES_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_surfaces":
            entries = self.surface_index() if self.surface_index else []
            return {
                "surfaces": entries,
                "note": (
                    "reconnect your MCP client to a surface's own `mcp` URL to "
                    "use it; this root surface only comprehends and lists"
                ),
            }
        if name != "comprehend_api":
            raise ComprehendError(
                f"unknown tool: {name} — this surface has exactly two tools: "
                "comprehend_api and list_surfaces"
            )
        url = arguments.get("url", "")
        if not isinstance(url, str) or not url:
            raise ComprehendError("comprehend_api requires a 'url' argument")
        ensure_submittable(url)  # remote door: http(s) only, no local file read
        result = comprehend_submission(
            url, from_docs=bool(arguments.get("from_docs", False))
        )
        return asdict(result)


_LIST_SURFACES_TOOL: dict[str, Any] = {
    "name": "list_surfaces",
    "annotations": tool_annotations(read_only=True, title="List hosted surfaces"),
    "description": (
        "Every API surface served on this host, with the MCP URL to reconnect "
        "to. Use it when you landed on the host root and need a specific API. "
        "Free, instant, and it lists only what the public index lists."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


_STDIO_INSTALL_HINT = (
    "Install the serve extra to run the stdio server: uv sync --extra serve "
    "(or: uv pip install 'gecko-surf[serve]')"
)


def install_unknown_tool_gate(server: Any, surface: Any) -> None:
    """Make an unknown tool name a JSON-RPC -32602 error, not a tool result.

    The SDK's ``@server.call_tool()`` decorator catches EVERY exception from the
    handler — ``McpError`` included — and flattens it into a ``CallToolResult`` with
    ``isError`` (mcp lowlevel server: ``except Exception: _make_error_result``). The
    only place a raised ``McpError`` still becomes the structured JSON-RPC error
    envelope is ``_handle_request`` itself, so the check must sit ABOVE the
    registered handler: wrap it, raise before the SDK's catch can see it. Applied
    by BOTH transports (HTTP and stdio) so the wires cannot diverge.
    """
    import mcp.types as _types

    inner = server.request_handlers[_types.CallToolRequest]

    async def _gated(req: Any) -> Any:
        ensure_known_tool(surface, req.params.name)  # raises McpError on a ghost name
        return await inner(req)

    server.request_handlers[_types.CallToolRequest] = _gated


def serve_stdio(
    spec_or_client: Any,
    base_url: str | None = None,
    mode: CallMode = "recorded",
    *,
    server_name: str = "gecko",
    enforce: EnforceMode | None = None,
) -> None:  # pragma: no cover - exercised by a founder-run / client-spawned smoke
    """Run a real MCP server over **stdio** (requires the `mcp` `serve` extra).

    The client SPAWNS this process and talks JSON-RPC over stdin/stdout — no port,
    no network, no tunnel — so it is the zero-friction local transport. This is the
    SAME comprehended surface + auth injection the HTTP path serves; only the wire
    edge differs (invariant: one code path, two modes). "bypass" = no Gecko-cloud
    hop, not "no Gecko on the machine."

    ``spec_or_client`` accepts a spec (str/dict), an ``AgentApiClient`` (the caller
    resolves + injects the credential at call time), an ``McpSurface``, or any
    duck-typed surface — reusing the HTTP path's single surface builder so the two
    transports can never diverge on comprehension.

    stdout is the protocol channel: this function MUST NOT print anything to stdout
    (a stray banner corrupts the JSON-RPC stream). Callers keep human output on
    stderr. Tools are registered on the LOW-LEVEL server so the question-shaped
    ``inputSchema`` reaches the agent intact (first-call-correct) — FastMCP would
    infer a permissive schema from the Python signature and erase ours.
    """
    try:
        import anyio
        import mcp.types as mcp_types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise SystemExit(_STDIO_INSTALL_HINT) from exc

    # Reuse the HTTP path's surface builder (spec/client/surface duck-typing + the
    # public_session default for a bare spec) so stdio and HTTP share ONE code path.
    from .http_server import _surface_from

    surface = _surface_from(spec_or_client, base_url, mode, enforce)
    # Same instructions as the HTTP transport — stdio used to drop them, so the
    # two transports disagreed on the one text a client shows before tool choice.
    server: Any = Server(
        server_name, instructions=getattr(surface, "instructions", None)
    )

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        # Per-request (not build-time): stdio is a single local session with no HTTP
        # request metadata, so list_tools is called with no correlation kwargs; the emit
        # is a no-op locally (no MONGODB_URI) and never a spurious build-time event.
        tools = surface.list_tools()
        return [
            mcp_types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
                annotations=t.get("annotations"),
            )
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        result = surface.call_tool(name, arguments or {})
        # Return unstructured JSON text; the body is never cached or persisted. An
        # upstream failure is flagged via isError (gecko.toolerror — the SAME decision
        # the HTTP wire uses) so stdio and HTTP can't diverge on what an error is.
        text, is_error = tool_result_payload(result)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)],
            isError=is_error,
        )

    install_unknown_tool_gate(server, surface)

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(_run)
