"""Surface graph — correlations & multi-call planning (V2 §4/§5).

Builds a deterministic, content-addressed graph from ingest's normalized
``Operation``s and walks it to plan chain-shaped intents (``fixtures/snapshot``
→ ``odds/updates`` via ``FixtureId``). Pure, no I/O — invariant #2 (API-agnostic)
and invariant #1 (surface only: operations/params/fields/edges, never payloads).

Provenance is on **every** edge and is the anti-poisoning control (§2): facts the
spec states are ``EXTRACTED`` (produces/consumes/on); explicit entity hints
(x-gecko / a customer confirmation) mint ``DECLARED`` ``feeds`` edges; links we
*derive* are ``INFERRED`` (``field --feeds--> param``) with a recorded ``basis``
+ ``confidence``. The classes never mix silently — a ``feeds`` edge is always
DECLARED or INFERRED (never EXTRACTED), so a poisoned spec can at worst create
an auditable, disableable edge with its origin on it, never a fact masquerading
as spec-stated. The trust ladder for feeds (§13.2, locked by the §13.6 probe):
DECLARED > INFERRED+signature-corroborated > INFERRED-name; the §13.1
value-domain signature is captured on param/field nodes and used ONLY as a
corroborator (``basis`` gains ``+sig``), never as a standalone join basis.

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
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .ingest import Operation, Param
from .sanitize import key_is_dangerous

# --- single source of truth for the graph's Literal types (CLAUDE.md) -----------
# CLAIMED = an op asserted by an untrusted docs source (Context7 / from-docs) that has
# NOT yet been checked against the live API. It is the entry point to the VAS verified-
# against-reality tier: a CLAIMED op is lifted to a VerifyVerdict once reality answers.
Provenance = Literal["EXTRACTED", "DECLARED", "INFERRED", "CLAIMED"]
Confidence = Literal["high", "low"]
NodeKind = Literal["operation", "param", "field", "resource"]
EdgeKind = Literal["consumes", "produces", "on", "feeds"]
# the verified-against-reality verdict a replay lifts onto a CLAIMED op (VAS-1).
VerifyStatus = Literal["VERIFIED", "REFUTED", "UNVERIFIED"]

_MAX_LEAF_DEPTH = 6  # bound the response-schema walk (ingest already resolved $refs)
_ID_TYPES = ("number", "string")  # a flow key must be id-shaped; drops bool/enum links
# generic-demotion floor: a produced/consumed name in > this many ops is demoted.
# floored at 4 so a small API (seq in 1 of 18 ops) is not over-demoted, scaling to
# ~3% for large APIs (Stripe `limit`, consumed by 381 of 587 ops, is demoted).
_GENERIC_FLOOR = 4
_GENERIC_FRAC = 0.03
# formats that discriminate a value domain (§13.1). int32/int64 are NOT here on
# purpose — every counter shares them; a shared int64 proves nothing (§13.6).
# ``base58`` is a DERIVED domain (from an example shape / a "base58"/"mint address"
# description hint) — a rare, high-entropy value domain that discriminates like a
# declared pattern; folded into the format slot by ``_sig_of`` when no explicit format
# is declared. It is a CORROBORATOR only — like every other signal here it never mints a
# standalone cross-API join (the §13.6 gate lives in ``correlate._score``, untouched).
_DISCRIMINATING_FMT = frozenset(
    {"uuid", "uri", "email", "ipv4", "date-time", "currency", "base58"}
)
# planning tiebreak rank: the §13.2 ladder, lower is preferred.
_PROV_RANK = {"DECLARED": 0, "INFERRED": 1}


# --- graph model (typed dataclasses; no bare dicts as contracts) -----------------
@dataclass(frozen=True)
class VerifyVerdict:
    """Whether a CLAIMED op was confirmed against the live API — the VAS verified-
    against-reality tier (VAS-1).

    Control-plane clean (invariant #1): it carries only the verdict STATUS, the string
    BASIS that earned it (e.g. ``replay:200``, ``replay:404``,
    ``contract-mismatch:missing tokenId``, ``no-access:recorded-only``), and a boolean
    shape marker. ``shape_ok`` is a DERIVED flag — "did the reply's shape match the
    contract" — never the shape, a response value, or a secret. There is deliberately no
    field a response body could be assigned to.
    """

    status: VerifyStatus
    basis: tuple[str, ...]
    shape_ok: bool = (
        False  # non-payload marker: shape confirmed, never the shape itself
    )


@dataclass(frozen=True)
class Node:
    kind: NodeKind
    id: str  # deterministic, unique (namespaced by surface_id when set)
    name: str  # human label: op id / param name / field name / resource noun
    owner: str = ""  # operation_id for param+field+operation nodes; "" for resource
    detail: str = ""  # param: "{location}|{req|opt}"; field: parent object; op: path
    sig: str = ""  # §13.1 value-domain signature (param/field): type|fmt|pat8|enum8
    # VAS-1: the verified-against-reality verdict once a CLAIMED op is replayed. Default
    # None (unverified op == unchanged); excluded from serialize() on purpose — the
    # content hash addresses the derived SHAPE, not a drift-dependent replay result.
    verify: VerifyVerdict | None = None


@dataclass(frozen=True)
class Edge:
    kind: EdgeKind
    src: str  # source node id
    dst: str  # destination node id
    provenance: Provenance
    basis: str = ""  # recorded reason for a feeds edge (e.g. "entity:fixture")
    confidence: Confidence | None = None  # high|low on feeds; None on EXTRACTED


@dataclass(frozen=True)
class PlanStep:
    operation_id: str
    method: str
    path: str
    consumes: tuple[str, ...]  # required non-auth inputs this step needs
    supplies: tuple[str, ...]  # inputs this step supplies to a later step
    surface: str = ""  # owning surface_id — "" on a legacy un-namespaced graph


@dataclass(frozen=True)
class ExplainEntry:
    param: str  # the consumed input this edge satisfies
    source_op: str
    source_field: str
    # DECLARED (an explicit hint) or INFERRED (derived) — never EXTRACTED for
    # feeds; surfaced so a bait chain says what it rests on.
    provenance: Provenance
    basis: str
    confidence: Confidence | None
    source_surface: str = ""  # the supplier's surface_id — set on cross-API plans


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]
    explain: tuple[ExplainEntry, ...]


# --- provenance classification: the CLAIMED entry point (single source of truth) ---
# gecko.docs_reader stamps this on ``info`` when it recovers a draft OpenAPI from a human
# page; the per-op keys are its confidence/review annotations. Either marks an op as a
# claim an untrusted docs source made — provenance CLAIMED — until reality answers.
_DOCS_DRAFT_MARKER = "gecko.docs_reader"
_DRAFT_OP_KEYS = ("x-draft-confidence", "x-review")


def op_provenance(spec: Mapping[str, Any], operation_id: str) -> Provenance:
    """Classify one op's provenance from its spec markers: CLAIMED if it was recovered
    from an untrusted docs source (a from-docs draft), else EXTRACTED. Pure/surface-only
    — reads only the draft markers, never a schema value.

    This lives here (not in the verify driver) because CLAIMED is the canonical top of
    the trust ladder (§13.2) and its value has a single home. It is a SEPARATE axis from
    the ``VerifyVerdict`` a replay later attaches: a CLAIMED op keeps its provenance and
    gains a verdict; an EXTRACTED op keeps EXTRACTED and gains a verdict just the same.
    """
    if not isinstance(spec, Mapping):
        return "EXTRACTED"
    info = spec.get("info")
    if isinstance(info, Mapping) and info.get("x-generated-by") == _DOCS_DRAFT_MARKER:
        return "CLAIMED"
    item = _operation_item(spec, operation_id)
    if item is not None and any(k in item for k in _DRAFT_OP_KEYS):
        return "CLAIMED"
    return "EXTRACTED"


def _operation_item(
    spec: Mapping[str, Any], operation_id: str
) -> Mapping[str, Any] | None:
    """The raw path-item operation object for ``operation_id``, or None — read ONLY for
    its draft markers (provenance), never a schema value."""
    paths = spec.get("paths")
    if not isinstance(paths, Mapping):
        return None
    for path_item in paths.values():
        if not isinstance(path_item, Mapping):
            continue
        for op in path_item.values():
            if isinstance(op, Mapping) and op.get("operationId") == operation_id:
                return op
    return None


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


# base58 value-domain (§13.1 richer signature): a 32-44 char base58 string is the
# Solana address/mint shape — a rare, high-entropy domain. Detected from an example's
# SHAPE or a name/description hint, never from a response value. Bound the scanned text
# so an untrusted, oversized description can't blow up the derivation.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BASE58_HINTS = ("base58", "mint address", "spl mint", "solana address", "pubkey")
_DESC_SCAN_CAP = 400


def _domain_signal(name: str, description: str, example: object) -> str:
    """A DERIVED discriminating value-domain token for a field/param, or "".

    Reads only SURFACE metadata (the field name, its description, and a spec-authored
    example's SHAPE) — never a response value (invariant #1). Today it recognizes the
    ``base58`` Solana-address/mint domain, the join key Pegana/Birdeye/Jupiter share
    (``docs/specs/2026-07-28-pegana-correlation-surface.md``). This lets a field whose
    spec ships NO ``pattern``/``format`` still self-declare its value domain, so an
    intra-API join corroborates where before it had nothing to match on — WITHOUT
    loosening any cross-API gate (that stays DECLARED+CONFIRMED in ``correlate._score``).
    """
    if isinstance(example, str) and _BASE58_RE.match(example):
        return "base58"
    hay = f"{name} {description}"[:_DESC_SCAN_CAP].lower()
    if any(hint in hay for hint in _BASE58_HINTS):
        return "base58"
    return ""


def _sig_of(
    schema: object, name: str = "", description: str = "", example: object = None
) -> str:
    """The §13.1 value-domain signature of one field/param, compact + control-plane clean:
    ``type|format|pattern-hash8|enum-hash8`` ("" components when undeclared, "" when
    nothing is declared at all). Pattern and enum are hashed — the signature carries
    *identity of the constraint*, never the constraint text, so it can sit on a node
    and inside content_hash without bloating either.

    Richer signature (§13.1): when the spec declares NO explicit ``format`` but the
    field's name/description/example self-declares a discriminating value domain (e.g. a
    ``base58`` mint), that DERIVED domain fills the format slot — so more fields carry
    their own value-domain identity and more real joins corroborate. Still surface-only,
    still a corroborator (folded into the same format slot every existing consumer already
    reads), never a standalone cross-API auto-join basis."""
    if not isinstance(schema, dict):
        return ""
    t = schema.get("type") or ""
    fmt = schema.get("format") or ""
    if not fmt:
        # only DERIVE when the spec declared nothing — an explicit format always wins.
        ex = example if example is not None else schema.get("example")
        fmt = _domain_signal(name, description, ex)
    pat = schema.get("pattern") or ""
    pat8 = (
        hashlib.sha256(pat.encode()).hexdigest()[:8]
        if isinstance(pat, str) and pat
        else ""
    )
    enum = schema.get("enum")
    en8 = ""
    if isinstance(enum, list) and enum:
        joined = "|".join(sorted(str(v) for v in enum))
        en8 = hashlib.sha256(joined.encode()).hexdigest()[:8]
    if not (t or fmt or pat8 or en8):
        return ""
    return f"{t}|{fmt}|{pat8}|{en8}"


def _sig_corroborates(a: str, b: str) -> bool:
    """True iff two signatures share a DISCRIMINATING value-domain signal: equal
    pattern, equal enum, or an equal discriminating format — same type required.

    This is the §13.6 role of the signature: a **corroborator** that strengthens an
    entity-name match (basis gains ``+sig``), never a standalone join basis. Bare
    same-type (both plain strings) is NOT corroboration — that's exactly the
    signal-free case the two-API probe measured on real specs."""
    if not a or not b:
        return False
    ta, fa, pa, ea = a.split("|")
    tb, fb, pb, eb = b.split("|")
    if ta != tb:
        return False
    if pa and pa == pb:
        return True
    if ea and ea == eb:
        return True
    return bool(fa) and fa == fb and fa in _DISCRIMINATING_FMT


def _response_leaves(op: Operation) -> list[tuple[str, str | None, str, str]]:
    """(field_name, parent_object_name, id-shape-type, sig) leaves of the 200 response.

    ``id`` and ``number`` collapse to ``number``; cycle- and depth-guarded (ingest
    already resolved $refs, but self-referential schemas can still recurse)."""
    resp = (op.responses or {}).get("200") or {}
    content = (resp.get("content") or {}).get("application/json") or {}
    schema = content.get("schema") or {}
    out: list[tuple[str, str | None, str, str]] = []

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
                out.append(
                    (
                        name,
                        title or parent,
                        "number" if t == "integer" else t,
                        _sig_of(
                            sub,
                            name=name,
                            description=str(sub.get("description") or ""),
                        ),
                    )
                )
            walk(sub, name, depth + 1, seen)
        walk(node.get("items") or {}, title or parent, depth + 1, seen)
        for k in ("oneOf", "anyOf", "allOf"):
            for sub in node.get(k, []) or []:
                walk(sub, parent, depth + 1, seen)

    walk(schema, None, 0, frozenset())
    return out


def _request_body_params(op: Operation) -> list[Param]:
    """REQUIRED, id-shaped scalar leaves of the JSON request body, as synthetic
    ``location="body"`` params (§ roadmap V2.1 — body-carried join keys).

    Modelled as params so a body join key becomes a plannable input exactly like a
    path/query one: the same INFERRED feeds rules (id-shape, genericity, entity) then
    apply, and ``_param_id`` keys on ``location`` so a body ``orderId`` never collides
    with a query ``orderId``.

    Deliberately conservative — the request body is attacker-controllable input, so we
    surface the *smallest* set that can actually block a first call:

    * REQUIRED only — an optional body field never blocks the call, so it is never a
      chain-critical target (mirrors ``_required_inputs`` on path/query).
    * id-shaped scalars only (``_ID_TYPES``) — a bool/enum body field cannot be a join
      key, so it can never mint one however it is named.
    * cycle- and depth-guarded, like ``_response_leaves``.

    A nested required object contributes its own required scalar leaves; ``required`` is
    read per object level, so a field required inside an optional parent is not treated
    as required overall (the parent can be omitted).
    """
    body = op.request_body or {}
    schema = ((body.get("content") or {}).get("application/json") or {}).get(
        "schema"
    ) or {}
    out: list[Param] = []
    seen_names: set[str] = set()

    def walk(node: object, depth: int, seen: frozenset[int]) -> None:
        if depth > _MAX_LEAF_DEPTH or not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}
        required = {r for r in (node.get("required") or []) if isinstance(r, str)}
        for name, sub in (node.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            t = sub.get("type") or ("object" if sub.get("properties") else "?")
            norm_t = "number" if t == "integer" else t
            if (
                name in required
                and norm_t in _ID_TYPES
                and name not in seen_names
                # Defense-in-depth: a body property NAME is attacker-controllable and
                # would ride into the plan block. An instruction-shaped or absurdly long
                # name is dropped here so it never becomes a plannable node — the same
                # treatment sanitize_schema gives a dangerous key in the tool def. The
                # plan projection also fails closed on such a name (belt and braces).
                and not key_is_dangerous(name)
            ):
                out.append(Param(name=name, location="body", required=True, schema=sub))
                seen_names.add(name)
            # only descend into a REQUIRED nested object — an optional parent can be
            # omitted whole, so its leaves never block the call.
            if name in required:
                walk(sub, depth + 1, seen)
        walk(node.get("items") or {}, depth + 1, seen)
        for k in ("allOf",):
            for sub in node.get(k, []) or []:
                walk(sub, depth + 1, seen)

    walk(schema, 0, frozenset())
    return out


def _consumer_params(op: Operation) -> list[Param]:
    """Every input this op consumes that the planner may source: the declared
    path/query/header params plus required body join keys. One list so the phase-1
    node build and both phase-2 feeds loops treat body keys identically to params."""
    return list(op.parameters) + _request_body_params(op)


# --- node id builders (deterministic, unique) -----------------------------------
def _ns(surface_id: str) -> str:
    """The node-id namespace prefix for a surface. "" keeps legacy single-API ids
    byte-identical; a set surface_id scopes every node id (§12 Phase 3) so graphs
    from different surfaces can never collide when composed (§13.2 per-scope
    ladder). One-way: the namespace feeds serialize() and thus content_hash."""
    return f"{surface_id}::" if surface_id else ""


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
    surface_id: str = ""  # namespace of every node id when set (§12 Phase 3)
    # the DECLARED entity vocabulary this graph was built with, as sorted
    # (normalized_name, entity) pairs — carried on the graph so compose() can
    # join across surfaces on entity identity without re-reading any spec.
    declared: tuple[tuple[str, str], ...] = ()
    # the CUSTOMER-CONFIRMED subset of ``declared`` — the (normalized_name, entity)
    # pairs whose source is the workspace confirm store (injected declared_hints),
    # NOT provider-authored x-gecko. This is the TRUSTED subset: only a customer
    # vouch may drive an executable cross-surface plan (compose.cross_plan) or a
    # plan-eligible cross-API correlation (§13.6 guardrail 3/4). ``declared`` stays
    # the merged provider∪customer vocab (untrusted); ``confirmed`` is what a human
    # actually stood behind. Empty when no customer hints were injected -> compose
    # refuses every cross-join (fail-closed: nothing is customer-vouched).
    confirmed: tuple[tuple[str, str], ...] = ()
    # indices (not serialized) — kept out of the content hash on purpose.
    _by_id: dict[str, Node] = field(default_factory=dict, compare=False, repr=False)

    def serialize(self) -> bytes:
        """Deterministic, content-addressed bytes: same spec in -> identical bytes out.

        Sorted nodes then edges (+ surface_id and the declared vocabulary — both
        one-way inputs to the hash, §12 Phase 3), canonical compact JSON. A drifted
        spec produces a reviewable diff (§4)."""
        payload = {
            "surface_id": self.surface_id,
            "declared": sorted(list(pair) for pair in self.declared),
            "confirmed": sorted(list(pair) for pair in self.confirmed),
            "nodes": sorted(
                ([n.kind, n.id, n.name, n.owner, n.detail, n.sig] for n in self.nodes),
            ),
            "edges": sorted(
                (
                    [e.kind, e.src, e.dst, e.provenance, e.basis, e.confidence or ""]
                    for e in self.edges
                ),
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def opnode(self, operation_id: str) -> str:
        """The (namespaced) node id of an operation."""
        return f"{_ns(self.surface_id)}op:{operation_id}"

    def required_inputs(self, operation_id: str) -> list[Node]:
        """Public view of an operation's required non-auth inputs (path/query) —
        the compose seam (§13): cross-surface planning needs to see what the
        intent op consumes without reaching into private machinery."""
        return self._required_inputs(self.opnode(operation_id))

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
            if flag == "req" and loc in ("path", "query", "body"):
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
            best: (
                tuple[int, int, str, list[str], list[ExplainEntry], Edge, Node] | None
            ) = None
            for edge in self.feeds_into(pn.id):
                src_field = self._by_id[edge.src]
                src_op = src_field.owner
                src_op_node = self.opnode(src_op)
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
                # with no unsatisfied inputs of its own); then the §13.2 ladder
                # (DECLARED beats INFERRED at equal cost); deterministic tiebreak by id.
                rank = _PROV_RANK.get(edge.provenance, 1)
                cand = (
                    cost,
                    rank,
                    src_op_node,
                    sub_order,
                    sub_explain,
                    edge,
                    src_field,
                )
                if best is None or (cost, rank, src_op_node) < (
                    best[0],
                    best[1],
                    best[2],
                ):
                    best = cand
            if best is None:
                return None  # honest: no confident supplier -> whole plan is None
            _cost, _rank, src_op_node, sub_order, sub_explain, edge, src_field = best
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
            surface=self.surface_id,
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
    op_node_id = graph.opnode(op_id)
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
        supplied_by[graph.opnode(entry.source_op)].append(entry.param)

    steps = tuple(
        graph._make_step(node_id, tuple(sorted(supplied_by.get(node_id, []))))
        for node_id in [*pred_order, op_node_id]
    )
    return Plan(steps=steps, explain=tuple(explain))


# --- build --------------------------------------------------------------------
def build_graph(
    operations: list[Operation],
    *,
    surface_id: str = "",
    declared: Mapping[str, str] | None = None,
    confirmed: Mapping[str, str] | None = None,
) -> SurfaceGraph:
    """Deterministic surface graph: operation/param/field/resource nodes + EXTRACTED
    produces/consumes/on edges + DECLARED feeds edges (explicit entity hints, the
    ladder's top) + INFERRED feeds edges (v3 basis). Surface only.

    ``surface_id`` namespaces every node id (§12 Phase 3) — "" keeps legacy ids.
    ``declared`` maps a param/field NAME to an entity (from x-gecko hints or a
    customer confirmation); two names declared to the same entity join with
    provenance DECLARED even when their names differ — the only cross-API-grade
    basis (§13.6). Hint names/entities are matched on normalized form.

    ``confirmed`` is the CUSTOMER-vouched subset of ``declared`` (the workspace
    confirm store / injected ``declared_hints``, NEVER provider x-gecko). It is
    carried onto the graph as the trusted provenance source both correlate and
    compose read: only a confirmed entity may drive an executable cross-surface
    plan. Omitted -> empty -> compose refuses all cross-joins (fail-closed)."""
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    ns = _ns(surface_id)
    # normalized declared vocabulary: name -> entity (both sides normalized).
    decl = {_norm(k): _norm(v) for k, v in (declared or {}).items() if k and v}
    # the customer-confirmed subset — same normalization; only names that are also
    # in ``decl`` (a confirmation without a matching declared name is inert).
    conf = {
        _norm(k): _norm(v)
        for k, v in (confirmed or {}).items()
        if k and v and _norm(k) in decl
    }

    def add_node(node: Node) -> None:
        nodes.setdefault(node.id, node)

    # frequency tables for genericity demotion (from the graph itself, no stoplist)
    produced_by: dict[str, set[str]] = defaultdict(set)
    consumed_by: dict[str, set[str]] = defaultdict(set)
    # producers[name] -> [(op_id, field_name, parent, id_type, sig)], first per name/op
    producers: dict[str, list[tuple[str, str, str | None, str, str]]] = defaultdict(
        list
    )
    # declared_producers[entity] -> [(op_id, field_name, parent, id_type)]
    declared_producers: dict[str, list[tuple[str, str, str | None, str]]] = defaultdict(
        list
    )

    # -- phase 1: nodes + EXTRACTED edges -----------------------------------------
    for op in operations:
        oid = op.operation_id
        op_node_id = f"{ns}{_op_id(op)}"
        add_node(Node("operation", op_node_id, oid, oid, f"{op.method} {op.path}"))

        noun = _resource_noun(op)
        if noun:
            rid = f"{ns}{_resource_id(noun)}"
            add_node(Node("resource", rid, noun))
            edges.append(Edge("on", op_node_id, rid, "EXTRACTED"))

        for p in _consumer_params(op):
            pid = f"{ns}{_param_id(_op_id(op), p)}"
            flag = "req" if p.required else "opt"
            add_node(
                Node(
                    "param",
                    pid,
                    p.name,
                    oid,
                    f"{p.location}|{flag}",
                    _sig_of(
                        p.schema,
                        name=p.name,
                        description=p.description,
                        example=p.example,
                    ),
                )
            )
            edges.append(Edge("consumes", op_node_id, pid, "EXTRACTED"))
            consumed_by[_norm(p.name)].add(oid)

        seen_here: set[str] = set()
        for fname, parent, ftype, fsig in _response_leaves(op):
            fid = f"{ns}{_field_id(_op_id(op), fname, parent)}"
            add_node(Node("field", fid, fname, oid, parent or "", fsig))
            edges.append(Edge("produces", op_node_id, fid, "EXTRACTED"))
            n = _norm(fname)
            produced_by[n].add(oid)
            if n not in seen_here:
                producers[n].append((oid, fname, parent, ftype, fsig))
                seen_here.add(n)
            d_ent = decl.get(n)
            # a declared join key must still be id-shaped — an explicit hint on a
            # boolean cannot mint a join key (a nonsense thread, hint or not).
            if d_ent and ftype in _ID_TYPES:
                declared_producers[d_ent].append((oid, fname, parent, ftype))

    generic_t = max(_GENERIC_FLOOR, math.ceil(_GENERIC_FRAC * max(1, len(operations))))

    def is_generic(name: str) -> bool:
        return len(produced_by[name]) > generic_t or len(consumed_by[name]) > generic_t

    # -- phase 2a: DECLARED feeds edges (the ladder's top, §13.2) ------------------
    # An explicit hint joins on ENTITY IDENTITY, not name equality — the whole point
    # of DECLARED is carrying joins the deterministic tier structurally cannot see
    # (synonym names, bare-string domains — the §13.6 finding). Auditable via basis.
    if declared_producers:
        for op in operations:
            oid = op.operation_id
            for p in _consumer_params(op):
                if _is_auth_param(p):
                    continue
                d_ent = decl.get(_norm(p.name))
                if not d_ent:
                    continue
                for src_op, fld, parent, _ftype in declared_producers.get(d_ent, []):
                    if src_op == oid:
                        continue
                    edges.append(
                        Edge(
                            "feeds",
                            f"{ns}{_field_id(f'op:{src_op}', fld, parent)}",
                            f"{ns}{_param_id(_op_id(op), p)}",
                            "DECLARED",
                            f"declared:{d_ent}",
                            "high",
                        )
                    )

    # -- phase 2b: INFERRED feeds edges (v3 basis) ---------------------------------
    for op in operations:
        oid = op.operation_id
        rnoun = _resource_noun(op)
        for p in _consumer_params(op):
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
            p_sig = _sig_of(
                p.schema, name=p.name, description=p.description, example=p.example
            )
            for src_op, fld, parent, ftype, fsig in producers[n]:
                if src_op == oid:
                    continue
                f_ent = _entity_of(fld, parent)
                src_field_id = f"{ns}{_field_id(f'op:{src_op}', fld, parent)}"
                dst_param_id = f"{ns}{_param_id(_op_id(op), p)}"
                if f_ent and p_ent:
                    # rule 1: entity match — entity ids are the spine, exempt from
                    # genericity (the entity scope already prevents over-linking).
                    # BUT still id-shape-filter here (§12 bug a): the endswith("id")
                    # entity heuristic misfires on English words (`paid`->`pa`,
                    # `valid`, `void`, `uuid`, `grid`), so a *boolean* field named
                    # `paid` would otherwise mint a high-confidence join key. A join
                    # key is number/string-shaped; drop bool/enum here too.
                    if ftype not in _ID_TYPES:
                        continue
                    if f_ent == p_ent:
                        basis = f"scoped-id:{f_ent}" if n == "id" else f"entity:{f_ent}"
                        # §13.6: the value-domain signature CORROBORATES a name-entity
                        # match (recorded on the basis, visible in every explain) —
                        # it never mints an edge alone.
                        if _sig_corroborates(fsig, p_sig):
                            basis += "+sig"
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
    return SurfaceGraph(
        nodes=sorted_nodes,
        edges=tuple(uniq_edges),
        surface_id=surface_id,
        declared=tuple(sorted(decl.items())),
        confirmed=tuple(sorted(conf.items())),
        _by_id=by_id,
    )
