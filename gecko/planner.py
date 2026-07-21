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
    """Which of ``op``'s REQUIRED path/query inputs the stated intent already supplies.

    Deterministic and lexical: an input is satisfiable iff the query references its
    identifying token(s). This is the discriminator for whether a chain is needed — an
    UNsatisfied required input is exactly what the planner sources from a supplier op.
    Auth params (header/cookie) are never inputs the agent supplies (invariant #4), so
    only path/query params are considered.
    """
    q = _tokens(query)
    sat: set[str] = set()
    for p in op.parameters:
        if not p.required or p.location not in ("path", "query"):
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
    return plan_to_dict(p)
