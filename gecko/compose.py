"""Cross-surface composition (§12 Phase 4, §13) — per-surface graphs composed at
plan time, NEVER merged.

Each API keeps its own ``SurfaceGraph`` (its own genericity statistics, its own
namespace — merging would pollute the per-union frequency math, §12). A
``Workspace`` holds the graphs; ``cross_plan`` answers a two-API intent
("price it in A, act in B") by sourcing the target op's unsatisfied inputs from
OTHER surfaces.

**Cross-surface joins are DECLARED-only** — the §13.6 two-API probe proved on
real specs (Stripe × Adyen) that neither name equality nor the value-domain
signature can carry a cross-API join: real APIs ship bare ``type: string``
domains, and a shared name across two independent APIs proves nothing. So a
cross edge exists ONLY when BOTH sides map to the same entity in their DECLARED
vocabulary (x-gecko / ``gecko graph confirm``); INFERRED never crosses a
surface boundary, whatever the name looks like. Within each surface the full
ladder still applies (intra-surface suppliers resolve through ``graph.plan``).

Pure + surface-only (invariants #1/#2): no I/O, no payloads; a plan is advice —
each step's call still goes through its own surface's client and session
(invariant #3 holds because compose never touches transport).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .graph import (
    ExplainEntry,
    Node,
    Plan,
    PlanStep,
    SurfaceGraph,
    _norm,
    plan as intra_plan,
)

_MAX_SUB_OPS = 2  # a cross supplier may bring at most this many ops of its own
_MAX_TOTAL_STEPS = 5  # hard cap on a composed plan (honesty over heroics)
_ID_SIG_TYPES = ("string", "integer", "number")  # id-shaped raw schema types


class ComposeError(Exception):
    """A workspace that cannot be composed (duplicate/empty surface ids)."""


@dataclass(frozen=True)
class Workspace:
    """The per-workspace set of composed surfaces. Graphs are held side by side —
    never merged — and every graph must carry a unique non-empty ``surface_id``
    (the §12 Phase 3 namespace is what makes composition collision-free)."""

    graphs: tuple[SurfaceGraph, ...]

    def __post_init__(self) -> None:
        ids = [g.surface_id for g in self.graphs]
        if any(not i for i in ids):
            raise ComposeError("every composed graph needs a non-empty surface_id")
        if len(set(ids)) != len(ids):
            raise ComposeError(f"duplicate surface_id in workspace: {sorted(ids)}")

    def graph(self, surface_id: str) -> SurfaceGraph | None:
        for g in self.graphs:
            if g.surface_id == surface_id:
                return g
        return None


def _declared_field_producers(
    graph: SurfaceGraph, entity: str
) -> list[tuple[str, str]]:
    """(producer_op_id, field_name) for id-shaped response fields this graph's
    DECLARED vocabulary maps to ``entity`` — deterministic order."""
    decl = dict(graph.declared)
    out: list[tuple[str, str]] = []
    for node in graph.nodes:
        if node.kind != "field" or decl.get(_norm(node.name)) != entity:
            continue
        raw_type = node.sig.split("|", 1)[0] if node.sig else ""
        if raw_type not in _ID_SIG_TYPES:
            continue  # a declared join key must still be id-shaped
        out.append((node.owner, node.name))
    return sorted(set(out))


def _unsatisfied(
    graph: SurfaceGraph, operation_id: str, satisfiable: frozenset[str]
) -> list[Node]:
    return [
        pn
        for pn in graph.required_inputs(operation_id)
        if _norm(pn.name) not in satisfiable
    ]


def cross_plan(
    workspace: Workspace,
    surface_id: str,
    intent_op_id: str,
    satisfiable_inputs: frozenset[str] | set[str] | list[str],
    *,
    max_ops: int = 3,
) -> Plan | None:
    """Plan a (possibly cross-surface) chain for ``intent_op_id`` on ``surface_id``.

    Resolution order (each stage honest — no stage ever guesses):
    1. **Intra first**: the target surface's own ``plan()`` — if the chain closes
       within one API, no cross machinery is involved at all.
    2. **Cross, DECLARED-only**: for each still-unsatisfied required input whose
       name maps to an entity in the target's DECLARED vocabulary, find another
       surface whose DECLARED vocabulary produces that entity (id-shaped field),
       and whose producing op itself resolves within its own surface. The cross
       explain entry records provenance DECLARED, ``basis declared:<entity>``,
       and ``source_surface`` — auditable end to end.
    3. Re-plan the target intra with the cross-supplied inputs marked satisfiable
       (so remaining intra suppliers still resolve through the normal ladder).

    Returns None when any required input has no confident source — an honest
    "no plan" (§5), never a speculative cross join.
    """
    target = workspace.graph(surface_id)
    if target is None:
        return None
    sat = frozenset(_norm(x) for x in satisfiable_inputs)

    intra = intra_plan(target, intent_op_id, sat, max_ops=max_ops)
    if intra is not None:
        return intra
    if target.opnode(intent_op_id) not in target._by_id:
        return None

    decl_target = dict(target.declared)
    cross_steps: list[PlanStep] = []
    cross_explain: list[ExplainEntry] = []
    supplied: set[str] = set()

    for pn in sorted(_unsatisfied(target, intent_op_id, sat), key=lambda n: n.name):
        entity = decl_target.get(_norm(pn.name))
        if not entity:
            continue  # not declared on the consuming side -> not cross-sourceable
        resolved = False
        for other in sorted(workspace.graphs, key=lambda g: g.surface_id):
            if other.surface_id == surface_id or resolved:
                continue
            for src_op, field_name in _declared_field_producers(other, entity):
                # the supplier must resolve within its OWN surface (its own
                # ladder, its own genericity stats) — never a dangling step.
                sub = intra_plan(other, src_op, sat, max_ops=_MAX_SUB_OPS)
                if sub is None:
                    continue
                steps = list(sub.steps)
                # the supplier's final step supplies the cross param
                steps[-1] = replace(
                    steps[-1],
                    supplies=tuple(sorted({*steps[-1].supplies, pn.name})),
                )
                cross_steps.extend(steps)
                cross_explain.extend(sub.explain)
                cross_explain.append(
                    ExplainEntry(
                        param=pn.name,
                        source_op=src_op,
                        source_field=field_name,
                        provenance="DECLARED",
                        basis=f"declared:{entity}",
                        confidence="high",
                        source_surface=other.surface_id,
                    )
                )
                supplied.add(_norm(pn.name))
                resolved = True
                break

    if not supplied:
        return None  # nothing cross-sourceable -> same honest None as before

    finish = intra_plan(target, intent_op_id, sat | supplied, max_ops=max_ops)
    if finish is None:
        return None  # some input has no source on any surface

    # de-duplicate a supplier op appearing twice (two params fed by one op)
    seen: set[tuple[str, str]] = set()
    ordered: list[PlanStep] = []
    for step in [*cross_steps, *finish.steps]:
        key = (step.surface, step.operation_id)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(step)
    if len(ordered) > _MAX_TOTAL_STEPS:
        return None  # over the honesty cap — refuse, don't truncate

    return Plan(steps=tuple(ordered), explain=tuple([*cross_explain, *finish.explain]))
