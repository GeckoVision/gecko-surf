"""Intent → plan wiring (§5, §12 Phase 1) — the seam that makes ``graph.plan()``
reach an agent.

``catalog.search`` finds the top operation. When that operation's REQUIRED inputs
are NOT satisfiable from the agent's stated intent, this module derives the
satisfiable set (deterministic, from the query's own tokens), asks the graph to
plan the supplier chain, and projects the ``Plan`` — with its per-step provenance
intact — into a control-plane-safe dict the MCP surface attaches to the top hit.

Flat search is UNTOUCHED when the top op's inputs ARE satisfiable: a plan is
returned ONLY when a chain is actually needed (>= 1 supplier step). A trivial
single-step "plan" is suppressed so a simple query never grows a plan block.

Pure + surface-only: no I/O, no payloads, no LLM (invariants #1/#2). The plan is a
suggestion-with-provenance; the agent still makes every call itself, so Gecko never
becomes the data plane.
"""

from __future__ import annotations

from typing import Any

from .catalog import _tokens
from .sanitize import key_is_dangerous
from .graph import Plan, SurfaceGraph, _entity_of
from .graph import plan as graph_plan
from .ingest import Operation


def _identifying_tokens(param_name: str) -> set[str]:
    """The query tokens that would signal the agent already holds this input.

    An entity id (``fixtureId`` -> entity ``fixture``) is satisfied when the intent
    NAMES the entity; a non-id flow key (``seq``, ``statKey``) when the intent names
    the key. Entity ids key on the entity, never the literal ``id`` token, so a bare
    ``id`` in the query can't accidentally satisfy an unrelated id param.
    """
    ent = _entity_of(param_name)
    if ent:
        return {ent}
    return _tokens(param_name)


def satisfiable_inputs(query: str, op: Operation) -> frozenset[str]:
    """Which of ``op``'s REQUIRED inputs the stated intent already supplies.

    Deterministic and lexical: an input is satisfiable iff the query references its
    identifying token(s). This is the discriminator for whether a chain is needed — an
    UNsatisfied required input is exactly what the planner sources from a supplier op.

    Covers path/query params AND required body join keys (roadmap V2.1): the graph now
    plans over body keys, so satisfiability must match — otherwise a body key the intent
    already names would be treated as unsatisfied and trigger a needless supplier chain.
    Auth params (header/cookie) are never agent-supplied inputs (invariant #4), so they
    are excluded via the body/param source itself (body keys carry no auth; auth params
    are skipped below).
    """
    from .graph import _request_body_params

    q = _tokens(query)
    sat: set[str] = set()
    # path/query params, plus required body join keys (location == "body")
    candidates = [
        p for p in op.parameters if p.location in ("path", "query")
    ] + _request_body_params(op)
    for p in candidates:
        if not p.required:
            continue
        if _identifying_tokens(p.name) & q:
            sat.add(p.name)
    return frozenset(sat)


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """Project a ``Plan`` into a control-plane-safe dict, PRESERVING provenance end-to-end.

    Every step keeps its ordered ``consumes``/``supplies``; every explain entry keeps its
    ``basis``/``confidence``/``provenance`` + source op/field — the audit trail that lets a
    confirmed relationship be saved later and lets a reviewer see WHY the chain was proposed
    (§5, §12). No payloads, no auth, no values — surface metadata only.
    """
    return {
        "steps": [
            {
                "operation_id": s.operation_id,
                "method": s.method,
                "path": s.path,
                "consumes": list(s.consumes),
                "supplies": list(s.supplies),
                # additive (§12 plane-field precedent): the owning surface — ""
                # single-surface, set on cross-API plans so an agent knows which
                # mount each step belongs to.
                "surface": s.surface,
            }
            for s in plan.steps
        ],
        "explain": [
            {
                "param": e.param,
                "source_op": e.source_op,
                "source_field": e.source_field,
                "provenance": e.provenance,
                "basis": e.basis,
                "confidence": e.confidence,
                "source_surface": e.source_surface,
            }
            for e in plan.explain
        ],
    }


def plan_for_query(
    graph: SurfaceGraph, op: Operation, query: str, *, max_ops: int = 3
) -> dict[str, Any] | None:
    """A plan dict for ``op`` under ``query`` — or ``None`` when no chain is needed.

    ``None`` in three cases, all "flat search untouched": the required inputs are
    already satisfiable from the intent, the top op needs nothing, or no confident
    supplier chain exists within the depth cap. A trivial single-step plan (no supplier,
    empty explain) is suppressed here so a simple/satisfiable query never grows a plan
    block — plans appear ONLY when a chain is actually needed.
    """
    p = graph_plan(graph, op, satisfiable_inputs(query, op), max_ops=max_ops)
    if p is None or len(p.steps) <= 1:
        return None
    if _plan_has_dangerous_name(p):
        # Fail-CLOSED. The plan block is agent-facing advisory TEXT, but the graph is
        # built over raw (un-sanitized) operations, so a poisoned spec can put an
        # injection string in a param/field NAME (esp. a request-body property, where
        # arbitrary names are cheap — roadmap V2.1). The tool def drops such a name via
        # sanitize_schema; the plan must not smuggle it back in. A plan carrying an
        # instruction-shaped or over-long name is suppressed WHOLE rather than emitted
        # with the name scrubbed out (a partial plan is incoherent, and suppression is
        # the safe default for a best-effort advisory channel). Covers every location,
        # so this closes the pre-existing path/query channel too, not only body keys.
        return None
    return plan_to_dict(p)


def _plan_has_dangerous_name(plan: Plan) -> bool:
    """True if any spec-derived NAME the plan would surface to the agent trips the
    injection sanitizer (or is absurdly long). Names come from an attacker-controllable
    spec, so an instruction-shaped one must never reach the agent-facing plan block."""
    for s in plan.steps:
        for name in (*s.consumes, *s.supplies):
            if key_is_dangerous(name):
                return True
    for e in plan.explain:
        if any(
            key_is_dangerous(n) for n in (e.param, e.source_field, e.source_op, e.basis)
        ):
            return True
    return False
