"""Surface graph — correlations & multi-call planning (V2 §4/§5).

Builds a deterministic, content-addressed graph from ingest's normalized
``Operation``s and walks it to plan chain-shaped intents (``fixtures/snapshot``
→ ``odds/updates`` via ``FixtureId``). Pure, no I/O — invariant #2 (API-agnostic)
and invariant #1 (surface only: operations/params/fields/edges, never payloads).

Provenance is on **every** edge and is the anti-poisoning control (§2): facts the
spec states are ``EXTRACTED`` (produces/consumes/on); links we *derive* are
``INFERRED`` (``field --feeds--> param``) with a recorded ``basis`` +
``confidence``. The two never mix silently — a ``feeds`` edge is *always*
INFERRED, so a poisoned spec can at worst create an auditable, disableable
INFERRED edge, never a fact masquerading as spec-stated.

The ``feeds`` inference is the v3 basis that passed the §7 probe (both TxLINE
chains found; Stripe control 66,984 → 337 edges): entity ids are the join-key
spine (exempt from genericity), non-id flow keys survive only by statistical
rarity, and a produce-OR-consume frequency floor (not a hand stoplist) demotes
generic names. Ported from ``scripts/surface_graph_probe.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from .ingest import Operation, Param

# --- single source of truth for the graph's Literal types (CLAUDE.md) -----------
Provenance = Literal["EXTRACTED", "INFERRED"]
Confidence = Literal["high", "low"]
NodeKind = Literal["operation", "param", "field", "resource"]
EdgeKind = Literal["consumes", "produces", "on", "feeds"]

_MAX_LEAF_DEPTH = 6  # bound the response-schema walk (ingest already resolved $refs)
_ID_TYPES = ("number", "string")  # a flow key must be id-shaped; drops bool/enum links
# generic-demotion floor: a produced/consumed name in > this many ops is demoted.
# floored at 4 so a small API (seq in 1 of 18 ops) is not over-demoted, scaling to
# ~3% for large APIs (Stripe `limit`, consumed by 381 of 587 ops, is demoted).
_GENERIC_FLOOR = 4
_GENERIC_FRAC = 0.03


# --- graph model (typed dataclasses; no bare dicts as contracts) -----------------
@dataclass(frozen=True)
class Node:
    kind: NodeKind
    id: str  # deterministic, unique
    name: str  # human label: op id / param name / field name / resource noun
    owner: str = ""  # operation_id for param+field+operation nodes; "" for resource
    detail: str = ""  # param: "{location}|{req|opt}"; field: parent object; op: path


@dataclass(frozen=True)
class Edge:
    kind: EdgeKind
    src: str  # source node id
    dst: str  # destination node id
    provenance: Provenance
    basis: str = ""  # recorded reason for an INFERRED edge (e.g. "entity:fixture")
    confidence: Confidence | None = None  # high|low on INFERRED; None on EXTRACTED


@dataclass(frozen=True)
class PlanStep:
    operation_id: str
    method: str
    path: str
    consumes: tuple[str, ...]  # required non-auth inputs this step needs
    supplies: tuple[str, ...]  # inputs this step supplies to a later step


@dataclass(frozen=True)
class ExplainEntry:
    param: str  # the consumed input this edge satisfies
    source_op: str
    source_field: str
    provenance: (
        Provenance  # always INFERRED for feeds — surfaced so a bait chain says so
    )
    basis: str
    confidence: Confidence | None


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]
    explain: tuple[ExplainEntry, ...]


# --- name / entity helpers (ported from the v3 probe) ---------------------------
def _norm(s: str) -> str:
    return s.replace("_", "").replace("-", "").lower()


def _entity_of(name: str, parent: str | None = None) -> str | None:
    """The entity a name refers to. ``fixtureId`` -> ``fixture``; a bare ``id``
    under parent ``Customer`` -> ``customer``; else None (not an entity ref)."""
    n = _norm(name)
    if n.endswith("id") and len(n) > 2:
        return n[:-2]
    if n == "id" and parent:
        p = _norm(parent)
        return p[:-1] if p.endswith("s") else p  # depluralize
    return None


def _resource_noun(op: Operation) -> str | None:
    """The entity a path operates on: last non-parameter path segment, singularized."""
    segs = [s for s in op.path.split("/") if s and not s.startswith("{")]
    if not segs:
        return None
    last = _norm(segs[-1])
    return last[:-1] if last.endswith("s") else last


def _response_leaves(op: Operation) -> list[tuple[str, str | None, str]]:
    """(field_name, parent_object_name, id-shape-type) leaves of the 200 response.

    ``id`` and ``number`` collapse to ``number``; cycle- and depth-guarded (ingest
    already resolved $refs, but self-referential schemas can still recurse)."""
    resp = (op.responses or {}).get("200") or {}
    content = (resp.get("content") or {}).get("application/json") or {}
    schema = content.get("schema") or {}
    out: list[tuple[str, str | None, str]] = []

    def walk(
        node: object, parent: str | None, depth: int, seen: frozenset[int]
    ) -> None:
        if depth > _MAX_LEAF_DEPTH or not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}
        title = node.get("title")
        for name, sub in (node.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            t = sub.get("type") or ("object" if sub.get("properties") else "?")
            if t in ("integer", "number", "string", "boolean"):
                out.append((name, title or parent, "number" if t == "integer" else t))
            walk(sub, name, depth + 1, seen)
        walk(node.get("items") or {}, title or parent, depth + 1, seen)
        for k in ("oneOf", "anyOf", "allOf"):
            for sub in node.get(k, []) or []:
                walk(sub, parent, depth + 1, seen)

    walk(schema, None, 0, frozenset())
    return out


# --- node id builders (deterministic, unique) -----------------------------------
def _op_id(op: Operation) -> str:
    return f"op:{op.operation_id}"


def _param_id(op_id: str, p: Param) -> str:
    return f"param:{op_id}:{p.location}:{p.name}"


def _field_id(op_id: str, name: str, parent: str | None) -> str:
    return f"field:{op_id}:{parent or ''}:{name}"


def _resource_id(noun: str) -> str:
    return f"resource:{noun}"


def _is_auth_param(p: Param) -> bool:
    """Auth is invisible to the agent (invariant #4). Ingest surfaces auth headers
    as ordinary required params; the planner must not treat them as inputs needing
    a supplier — they are injected at call time by the access seam."""
    return p.location in ("header", "cookie")


@dataclass(frozen=True)
class SurfaceGraph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    # indices (not serialized) — kept out of the content hash on purpose.
    _by_id: dict[str, Node] = field(default_factory=dict, compare=False, repr=False)

    def serialize(self) -> bytes:
        """Deterministic, content-addressed bytes: same spec in -> identical bytes out.

        Sorted nodes then edges, canonical compact JSON. A drifted spec produces a
        reviewable diff (§4)."""
        payload = {
            "nodes": sorted(
                ([n.kind, n.id, n.name, n.owner, n.detail] for n in self.nodes),
            ),
            "edges": sorted(
                (
                    [e.kind, e.src, e.dst, e.provenance, e.basis, e.confidence or ""]
                    for e in self.edges
                ),
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def content_hash(self) -> str:
        return hashlib.sha256(self.serialize()).hexdigest()

    def feeds_into(self, param_node_id: str, *, high_only: bool = True) -> list[Edge]:
        """The INFERRED ``feeds`` edges whose destination is this param node.
        ``high_only`` excludes genericity-demoted (low) edges from planning (§10)."""
        return [
            e
            for e in self.edges
            if e.kind == "feeds"
            and e.dst == param_node_id
            and (not high_only or e.confidence == "high")
        ]

    # -- planning (§5): walk feeds backward to satisfy unsatisfied required inputs --
    def _required_inputs(self, op_node_id: str) -> list[Node]:
        """Required, non-auth param nodes this op consumes (path/query only)."""
        out = []
        for e in self.edges:
            if e.kind != "consumes" or e.src != op_node_id:
                continue
            pn = self._by_id[e.dst]
            loc, _, flag = pn.detail.partition("|")
            if flag == "req" and loc in ("path", "query"):
                out.append(pn)
        return out

    def _resolve(
        self,
        op_node_id: str,
        satisfiable: frozenset[str],
        budget: int,
        visited: frozenset[str],
    ) -> tuple[list[str], list[ExplainEntry]] | None:
        """Ordered predecessor op node ids + explain to satisfy op's unsatisfied
        required inputs, or None if no confident supplier fits within ``budget``
        predecessor ops (depth cap §5). Never a speculative chain."""
        unsat = [
            pn
            for pn in self._required_inputs(op_node_id)
            if _norm(pn.name) not in satisfiable
        ]
        if not unsat:
            return ([], [])
        if budget <= 0:
            return None

        order: list[str] = []
        added: set[str] = set()
        explain: list[ExplainEntry] = []
        for pn in sorted(unsat, key=lambda n: _norm(n.name)):
            best: tuple[int, str, list[str], list[ExplainEntry], Edge, Node] | None = (
                None
            )
            for edge in self.feeds_into(pn.id):
                src_field = self._by_id[edge.src]
                src_op = src_field.owner
                src_op_node = f"op:{src_op}"
                if src_op_node in visited or src_op_node == op_node_id:
                    continue  # cycle guard: an op cannot supply its own missing input
                sub = self._resolve(
                    src_op_node, satisfiable, budget - 1, visited | {op_node_id}
                )
                if sub is None:
                    continue
                sub_order, sub_explain = sub
                new_ops = {
                    o
                    for o in [*sub_order, src_op_node]
                    if o not in added and o not in order
                }
                cost = len(new_ops)
                # prefer the supplier that adds the fewest new ops (the clean source
                # with no unsatisfied inputs of its own), deterministic tiebreak by id.
                cand = (cost, src_op_node, sub_order, sub_explain, edge, src_field)
                if best is None or (cost, src_op_node) < (best[0], best[1]):
                    best = cand
            if best is None:
                return None  # honest: no confident supplier -> whole plan is None
            _cost, src_op_node, sub_order, sub_explain, edge, src_field = best
            for o in [*sub_order, src_op_node]:
                if o not in added:
                    order.append(o)
                    added.add(o)
            explain.extend(sub_explain)
            explain.append(
                ExplainEntry(
                    param=pn.name,
                    source_op=src_field.owner,
                    source_field=src_field.name,
                    provenance=edge.provenance,
                    basis=edge.basis,
                    confidence=edge.confidence,
                )
            )
        if len(order) > budget:
            return None
        return (order, explain)

    def _make_step(self, op_node_id: str, supplies: tuple[str, ...]) -> PlanStep:
        op_node = self._by_id[op_node_id]
        consumes = tuple(sorted(pn.name for pn in self._required_inputs(op_node_id)))
        method, _, path = op_node.detail.partition(" ")
        return PlanStep(
            operation_id=op_node.name,
            method=method,
            path=path,
            consumes=consumes,
            supplies=supplies,
        )


def plan(
    graph: SurfaceGraph,
    intent_op: Operation | str,
    satisfiable_inputs: frozenset[str] | set[str] | list[str],
    *,
    max_ops: int = 3,
) -> Plan | None:
    """Plan a chain for ``intent_op`` when its required inputs are not all
    satisfiable from the agent's intent (§5). Walks ``feeds`` edges backward to
    find suppliers; returns ordered steps + a provenance-carrying explain block,
    or None (honest "no confident plan") when the chain would exceed the depth
    cap or has an unsatisfiable input."""
    op_id = intent_op if isinstance(intent_op, str) else intent_op.operation_id
    op_node_id = f"op:{op_id}"
    if op_node_id not in graph._by_id:
        return None
    sat = frozenset(_norm(x) for x in satisfiable_inputs)
    resolved = graph._resolve(op_node_id, sat, budget=max_ops - 1, visited=frozenset())
    if resolved is None:
        return None
    pred_order, explain = resolved

    # which inputs each predecessor supplies to a later step (from the explain block)
    supplied_by: dict[str, list[str]] = defaultdict(list)
    for entry in explain:
        supplied_by[f"op:{entry.source_op}"].append(entry.param)

    steps = tuple(
        graph._make_step(node_id, tuple(sorted(supplied_by.get(node_id, []))))
        for node_id in [*pred_order, op_node_id]
    )
    return Plan(steps=steps, explain=tuple(explain))


# --- build --------------------------------------------------------------------
def build_graph(operations: list[Operation]) -> SurfaceGraph:
    """Deterministic surface graph: operation/param/field/resource nodes + EXTRACTED
    produces/consumes/on edges + INFERRED feeds edges (v3 basis). Surface only."""
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def add_node(node: Node) -> None:
        nodes.setdefault(node.id, node)

    # frequency tables for genericity demotion (from the graph itself, no stoplist)
    produced_by: dict[str, set[str]] = defaultdict(set)
    consumed_by: dict[str, set[str]] = defaultdict(set)
    # producers[name] -> [(op_id, field_name, parent, id_type)], first field per name/op
    producers: dict[str, list[tuple[str, str, str | None, str]]] = defaultdict(list)

    # -- phase 1: nodes + EXTRACTED edges -----------------------------------------
    for op in operations:
        oid = op.operation_id
        op_node_id = _op_id(op)
        add_node(Node("operation", op_node_id, oid, oid, f"{op.method} {op.path}"))

        noun = _resource_noun(op)
        if noun:
            rid = _resource_id(noun)
            add_node(Node("resource", rid, noun))
            edges.append(Edge("on", op_node_id, rid, "EXTRACTED"))

        for p in op.parameters:
            pid = _param_id(op_node_id, p)
            flag = "req" if p.required else "opt"
            add_node(Node("param", pid, p.name, oid, f"{p.location}|{flag}"))
            edges.append(Edge("consumes", op_node_id, pid, "EXTRACTED"))
            consumed_by[_norm(p.name)].add(oid)

        seen_here: set[str] = set()
        for fname, parent, ftype in _response_leaves(op):
            fid = _field_id(op_node_id, fname, parent)
            add_node(Node("field", fid, fname, oid, parent or ""))
            edges.append(Edge("produces", op_node_id, fid, "EXTRACTED"))
            n = _norm(fname)
            produced_by[n].add(oid)
            if n not in seen_here:
                producers[n].append((oid, fname, parent, ftype))
                seen_here.add(n)

    generic_t = max(_GENERIC_FLOOR, math.ceil(_GENERIC_FRAC * max(1, len(operations))))

    def is_generic(name: str) -> bool:
        return len(produced_by[name]) > generic_t or len(consumed_by[name]) > generic_t

    # -- phase 2: INFERRED feeds edges (v3 basis) ---------------------------------
    for op in operations:
        oid = op.operation_id
        op_node_id = _op_id(op)
        rnoun = _resource_noun(op)
        for p in op.parameters:
            if _is_auth_param(p):
                continue  # never infer a supplier for an auth header (invariant #4)
            n = _norm(p.name)
            if n not in producers:
                continue
            is_path = f"{{{p.name}}}" in op.path
            # param entity: an id-suffix name, or a bare path param scoped by its own
            # name / the path's resource noun.
            p_ent = (
                _entity_of(p.name)
                or (n if is_path else None)
                or (rnoun if is_path else None)
            )
            for src_op, fld, parent, ftype in producers[n]:
                if src_op == oid:
                    continue
                f_ent = _entity_of(fld, parent)
                src_field_id = _field_id(f"op:{src_op}", fld, parent)
                dst_param_id = _param_id(op_node_id, p)
                if f_ent and p_ent:
                    # rule 1: entity match — entity ids are the spine, exempt from
                    # genericity (the entity scope already prevents over-linking).
                    if f_ent == p_ent:
                        basis = f"scoped-id:{f_ent}" if n == "id" else f"entity:{f_ent}"
                        edges.append(
                            Edge(
                                "feeds",
                                src_field_id,
                                dst_param_id,
                                "INFERRED",
                                basis,
                                "high",
                            )
                        )
                elif f_ent is None and p_ent is None:
                    if ftype not in _ID_TYPES:
                        continue  # rule 3: non-id flow keys must be id-shaped
                    if is_generic(n):
                        # rule 2: genericity demotion -> INFERRED but LOW, quarantined
                        # out of plans (still visible/auditable with its basis).
                        edges.append(
                            Edge(
                                "feeds",
                                src_field_id,
                                dst_param_id,
                                "INFERRED",
                                f"generic:{n}",
                                "low",
                            )
                        )
                    else:
                        edges.append(
                            Edge(
                                "feeds",
                                src_field_id,
                                dst_param_id,
                                "INFERRED",
                                f"rare-key:{n}",
                                "high",
                            )
                        )

    by_id = dict(nodes)
    # dedupe + deterministic order so the dataclass itself is stable in memory too.
    uniq_edges = sorted(
        set(edges),
        key=lambda e: (e.kind, e.src, e.dst, e.provenance, e.basis, e.confidence or ""),
    )
    sorted_nodes = tuple(sorted(nodes.values(), key=lambda n: (n.kind, n.id)))
    return SurfaceGraph(nodes=sorted_nodes, edges=tuple(uniq_edges), _by_id=by_id)
