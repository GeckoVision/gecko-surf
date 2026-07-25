"""The correlation engine (§13 Phase 2) — the confidence, done honestly.

``correlate_surfaces(a, b)`` reports, WITH PROVENANCE, which of A's outputs
correlate to B's inputs and *why*. The "confidence" is the §13.2 **provenance
ladder**, never a learned float (that would re-import the retired corpus-moat
framing). It builds ON the shipped primitives — it re-derives no join logic:

  * ``graph.py`` — ``_entity_of`` (name -> entity), ``_sig_corroborates`` /
    ``_sig_of`` (the §13.1 value-domain signature), the genericity floor.
  * ``hints.py`` — ``declared_entity_hints`` (provider x-gecko, UNTRUSTED spec)
    vs. the client's injected customer-confirmed vocabulary.

The ladder (all deterministic; cross-API is effectively BINARY — §13.6):

  tier 3 DECLARED   both names map to the same entity in the DECLARED vocab.
                    Cross-API + both sides CUSTOMER-confirmed -> plan-eligible,
                    high. Cross-API with a PROVIDER-only declaration -> candidate
                    (guardrail 3/4: an untrusted spec can't mint a cross-system
                    join). Intra-API -> plan-eligible.
  tier 2 SIG        a shared discriminating pattern/enum/format corroborates a
                    name match. **INTRA-API ONLY** — the §13.6 gate forbids a
                    cross-API signature from carrying a join (zero false-high).
  tier 1 NAME       same ``_entity_of``, id-shaped, not genericity-demoted.
                    Intra-API -> plan-eligible. Cross-API -> a **candidate**,
                    quarantined, NEVER auto-joined.
  tier 0            none. **Type is a HARD gate**: a non-id-type or a mismatched
                    id-type drops to 0 before any scoring.

Control-plane only (invariant #1, guardrail 2): the scorer's ONLY input is a
:class:`Correland`, whose typed shape carries ``{name, type, value-domain
signature, name/declared entity, provenance}`` — a response value, sampled
overlap, or observed cardinality literally cannot be passed in.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .graph import (
    _GENERIC_FLOOR,
    _GENERIC_FRAC,
    SurfaceGraph,
    _entity_of,
    _norm,
    _sig_corroborates,
)

if TYPE_CHECKING:
    from .surface import Surface

# single source of truth for this module's Literal types (CLAUDE.md).
Provenance = Literal["DECLARED", "INFERRED"]
Confidence = Literal["high", "medium", "low"]
Origin = Literal["SPEC", "DOCS"]
DeclaredSource = Literal["customer", "provider"]

# id-shaped raw schema types — a join key is number/string-shaped (mirrors
# graph._ID_TYPES / compose._ID_SIG_TYPES); a bool/enum can never be a join key.
_ID_SIG_TYPES = ("string", "integer", "number")
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _id_type(sig: str) -> str | None:
    """The normalized id-type of a signature's leading raw type, or None (non-id).
    ``integer`` and ``number`` collapse to ``number`` so a counter and a float join."""
    raw = sig.split("|", 1)[0] if sig else ""
    if raw == "string":
        return "string"
    if raw in ("integer", "number"):
        return "number"
    return None


def _sig_signal(a: str, b: str) -> str | None:
    """The named discriminating signal two signatures share (§13.1), or None. A
    corroborator only — never a standalone cross-API basis (that's the §13.6 gate)."""
    if not _sig_corroborates(a, b):
        return None
    _, fa, pa, ea = a.split("|")
    _, fb, pb, eb = b.split("|")
    if pa and pa == pb:
        return "pattern-eq"
    if ea and ea == eb:
        return "enum-eq"
    return "format-eq"  # a shared discriminating format (uuid/uri/...)


# --- the scorer's ONLY input: a control-plane-typed value object (guardrail 2) ---
@dataclass(frozen=True)
class Correland:
    """One side of a candidate correlation — a **control-plane-typed** descriptor.

    Its fields are exactly ``{name, id-type, value-domain signature, name entity,
    declared entity + source, origin}``. There is deliberately NO field for a
    response value, a sampled overlap, or an observed cardinality — so a payload
    cannot be fed to the scorer (invariant #1). ``sig`` carries the *identity* of a
    schema constraint (type|format|pattern-hash|enum-hash), never any constraint or
    value text.
    """

    surface_id: str
    op_id: str
    name: str
    id_type: str | None  # "string" | "number" | None (non-id -> hard-gated)
    sig: str  # value-domain signature; hashes only, no value text
    name_entity: str | None  # _entity_of(name): the entity the NAME refers to
    declared_entity: str | None  # entity from the surface's DECLARED vocab
    declared_source: DeclaredSource | None  # customer-confirmed vs provider x-gecko
    origin: Origin = "SPEC"  # weakest data origin this side rests on


# --- the verdict objects (frozen; no bare dicts as contracts) -------------------
@dataclass(frozen=True)
class CorrelationBasis:
    """The *why*, riding each link — mirrors the graph's per-edge provenance."""

    entity: str | None  # value-domain entity, e.g. "solanatokenmint"
    tier: int
    provenance: Provenance
    src_surface: str
    dst_surface: str
    src_field: str
    dst_param: str
    signals: tuple[str, ...]  # each rung that fired
    weakest_origin: Origin
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "tier": self.tier,
            "provenance": self.provenance,
            "src_surface": self.src_surface,
            "dst_surface": self.dst_surface,
            "src_field": self.src_field,
            "dst_param": self.dst_param,
            "signals": list(self.signals),
            "weakest_origin": self.weakest_origin,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class CorrelationLink:
    """One A.output <-> B.input correlation, with its basis and whether it is usable
    NOW (``plan_eligible``) or a quarantined ``candidate`` needing a human confirm."""

    src_op: str
    dst_op: str
    basis: CorrelationBasis
    plan_eligible: bool

    @property
    def candidate(self) -> bool:
        """Quarantined — surfaced for a human to confirm, never auto-joined."""
        return not self.plan_eligible

    def to_dict(self) -> dict[str, Any]:
        return {
            "src_op": self.src_op,
            "dst_op": self.dst_op,
            "plan_eligible": self.plan_eligible,
            "candidate": self.candidate,
            "basis": self.basis.to_dict(),
        }


@dataclass(frozen=True)
class CorrelationResult:
    """The ranked links + a summary. ``to_dict`` is JSON-serializable and
    control-plane clean (structure only, never a payload)."""

    links: tuple[CorrelationLink, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "links": [link.to_dict() for link in self.links],
        }


# --- the pure scorer (control-plane; the §13.2 ladder) --------------------------
def _basis(
    tier: int,
    provenance: Provenance,
    src: Correland,
    dst: Correland,
    entity: str | None,
    signals: tuple[str, ...],
    confidence: Confidence,
) -> CorrelationBasis:
    weakest: Origin = "DOCS" if "DOCS" in (src.origin, dst.origin) else "SPEC"
    return CorrelationBasis(
        entity=entity,
        tier=tier,
        provenance=provenance,
        src_surface=src.surface_id,
        dst_surface=dst.surface_id,
        src_field=src.name,
        dst_param=dst.name,
        signals=signals,
        weakest_origin=weakest,
        confidence=confidence,
    )


def _score(src: Correland, dst: Correland) -> tuple[CorrelationBasis, bool] | None:
    """The §13.2 ladder over TWO control-plane descriptors. Returns
    ``(basis, plan_eligible)`` or None (tier 0). Cross-API is DECLARED-only for
    plan-eligibility; a name/signature match across the boundary is at most a
    quarantined candidate (§13.6)."""
    # tier 0 — TYPE IS A HARD GATE (before any name/signature scoring).
    if src.id_type is None or dst.id_type is None:
        return None  # a non-id-shaped side can never be a join key
    if src.id_type != dst.id_type:
        return None  # a string field cannot feed an integer param
    cross = src.surface_id != dst.surface_id

    # tier 3 — DECLARED entity identity (the only cross-API-grade basis, §13.6).
    if src.declared_entity and src.declared_entity == dst.declared_entity:
        entity = src.declared_entity
        signals = [
            f"declared:{entity}",
            f"declared-by:{src.surface_id}",
            f"declared-by:{dst.surface_id}",
        ]
        sig = _sig_signal(src.sig, dst.sig)
        if sig:
            signals.append(sig)
        if not cross:
            return _basis(3, "DECLARED", src, dst, entity, tuple(signals), "high"), True
        both_confirmed = (
            src.declared_source == "customer" and dst.declared_source == "customer"
        )
        if both_confirmed:
            return (
                _basis(
                    3, "DECLARED", src, dst, entity, (*signals, "confirmed"), "high"
                ),
                True,
            )
        # guardrail 3/4: a provider-DECLARED-from-spec entity is untrusted — it is a
        # candidate until the workspace owner confirms it. An untrusted provider must
        # not unilaterally mint a cross-system plan basis.
        return (
            _basis(
                3,
                "DECLARED",
                src,
                dst,
                entity,
                (*signals, "provider-declared-unconfirmed"),
                "medium",
            ),
            False,
        )

    # tier 2 — signature corroboration, INTRA-API ONLY (the §13.6 gate).
    sig = _sig_signal(src.sig, dst.sig)
    if sig and not cross:
        ent2: str | None = (
            src.name_entity
            if src.name_entity and src.name_entity == dst.name_entity
            else None
        )
        signals2 = [sig] if ent2 is None else [f"name-entity:{ent2}", sig]
        return _basis(2, "INFERRED", src, dst, ent2, tuple(signals2), "medium"), True

    # tier 1 — INFERRED name-entity match.
    if src.name_entity and src.name_entity == dst.name_entity:
        entity = src.name_entity
        signals1 = [f"name-entity:{entity}"]
        if sig:
            signals1.append(sig)  # signature corroborates the name (not a cross basis)
        if cross:
            # §13.6: a name (and even signature) match across the boundary is a
            # quarantined candidate — NEVER an auto-join.
            return (
                _basis(
                    1,
                    "INFERRED",
                    src,
                    dst,
                    entity,
                    (*signals1, "cross-api-quarantined"),
                    "low",
                ),
                False,
            )
        return _basis(1, "INFERRED", src, dst, entity, tuple(signals1), "low"), True

    return None  # tier 0 — nothing correlates


# --- enumeration: a surface -> its control-plane descriptors --------------------
def _declared_split(
    surface: Surface,
) -> tuple[dict[str, str], set[str]]:
    """``(name -> entity merged vocab, customer-confirmed names)`` — both normalized.

    The graph carries the MERGED declared vocabulary but loses provenance; the
    provider (untrusted spec x-gecko) vs. customer (injected confirmations) split is
    reconstructed here so guardrail 3/4 can gate on it. Customer wins on conflict,
    matching the client's own merge order."""
    client = surface.client
    merged = dict(surface.graph.declared)  # already normalized name -> entity
    customer = {_norm(k) for k in client._declared_hints}
    return merged, {n for n in merged if n in customer}


def _generic_names(graph: SurfaceGraph) -> frozenset[str]:
    """The genericity-demoted names of a surface — reuses graph.py's exact floor so
    an intra tier-1 link on an over-common name is quarantined, never planned."""
    produced: dict[str, set[str]] = defaultdict(set)
    consumed: dict[str, set[str]] = defaultdict(set)
    n_ops = 0
    for node in graph.nodes:
        if node.kind == "operation":
            n_ops += 1
        elif node.kind == "field":
            produced[_norm(node.name)].add(node.owner)
        elif node.kind == "param":
            consumed[_norm(node.name)].add(node.owner)
    floor = max(_GENERIC_FLOOR, math.ceil(_GENERIC_FRAC * max(1, n_ops)))
    return frozenset(
        name
        for name in set(produced) | set(consumed)
        if len(produced[name]) > floor or len(consumed[name]) > floor
    )


def _source_of(
    name_norm: str, merged: dict[str, str], customer: set[str]
) -> DeclaredSource | None:
    if name_norm not in merged:
        return None
    return "customer" if name_norm in customer else "provider"


def _outputs(
    surface: Surface, merged: dict[str, str], customer: set[str]
) -> list[Correland]:
    """Producer descriptors: id-shaped response FIELDS this surface emits."""
    out: list[Correland] = []
    for node in surface.graph.nodes:
        if node.kind != "field":
            continue
        idt = _id_type(node.sig)
        if idt is None:
            continue
        nn = _norm(node.name)
        out.append(
            Correland(
                surface_id=surface.surface_id,
                op_id=node.owner,
                name=node.name,
                id_type=idt,
                sig=node.sig,
                name_entity=_entity_of(node.name, node.detail or None),
                declared_entity=merged.get(nn),
                declared_source=_source_of(nn, merged, customer),
            )
        )
    return out


def _inputs(
    surface: Surface, merged: dict[str, str], customer: set[str]
) -> list[Correland]:
    """Consumer descriptors: required id-shaped PARAMS this surface consumes
    (path/query/body — never auth headers/cookies, invariant #4)."""
    out: list[Correland] = []
    for node in surface.graph.nodes:
        if node.kind != "param":
            continue
        loc, _, flag = node.detail.partition("|")
        if flag != "req" or loc not in ("path", "query", "body"):
            continue
        idt = _id_type(node.sig)
        if idt is None:
            continue
        nn = _norm(node.name)
        out.append(
            Correland(
                surface_id=surface.surface_id,
                op_id=node.owner,
                name=node.name,
                id_type=idt,
                sig=node.sig,
                name_entity=_entity_of(node.name),
                declared_entity=merged.get(nn),
                declared_source=_source_of(nn, merged, customer),
            )
        )
    return out


def _link(src: Correland, dst: Correland) -> CorrelationLink | None:
    scored = _score(src, dst)
    if scored is None:
        return None
    basis, plan_eligible = scored
    return CorrelationLink(
        src_op=src.op_id, dst_op=dst.op_id, basis=basis, plan_eligible=plan_eligible
    )


def _demote_generic(link: CorrelationLink, generic: frozenset[str]) -> CorrelationLink:
    """An intra tier-1 name link on a genericity-demoted name is quarantined (the
    §10 demotion, applied to the report exactly as graph.py applies it to plans)."""
    if link.basis.tier != 1 or not link.plan_eligible:
        return link
    if link.basis.src_surface != link.basis.dst_surface:
        return link
    names = {_norm(link.basis.src_field), _norm(link.basis.dst_param)}
    if not (names & generic):
        return link
    demoted = CorrelationBasis(
        **{
            **link.basis.to_dict(),
            "signals": (*link.basis.signals, "generic-demoted"),
            "confidence": "low",
        }
    )
    return CorrelationLink(link.src_op, link.dst_op, demoted, plan_eligible=False)


def correlate_surfaces(a: Surface, b: Surface) -> CorrelationResult:
    """Which of A's outputs correlate to B's inputs (and, when ``a is not b``, B's
    outputs to A's inputs), each with a provenance-carrying basis. Deterministic,
    control-plane only. Cross-API links are DECLARED-only for plan-eligibility; a
    bare name/signature match across the boundary is a quarantined candidate."""
    a_merged, a_customer = _declared_split(a)
    b_merged, b_customer = _declared_split(b)
    a_gen = _generic_names(a.graph)

    a_out = _outputs(a, a_merged, a_customer)
    a_in = _inputs(a, a_merged, a_customer)
    b_out = _outputs(b, b_merged, b_customer)
    b_in = _inputs(b, b_merged, b_customer)

    pairs = [(src, dst) for src in a_out for dst in b_in]
    if a is not b:
        pairs += [(src, dst) for src in b_out for dst in a_in]

    links: list[CorrelationLink] = []
    for src, dst in pairs:
        link = _link(src, dst)
        if link is None:
            continue
        links.append(_demote_generic(link, a_gen))

    links.sort(
        key=lambda lk: (
            not lk.plan_eligible,  # plan-eligible first
            -lk.basis.tier,  # then higher tier
            _CONF_RANK.get(lk.basis.confidence, 3),
            lk.src_op,
            lk.dst_op,
            lk.basis.src_field,
            lk.basis.dst_param,
        )
    )
    ranked = tuple(links)
    summary: dict[str, Any] = {
        "total": len(ranked),
        "plan_eligible": sum(1 for lk in ranked if lk.plan_eligible),
        "candidates": sum(1 for lk in ranked if lk.candidate),
        "by_tier": {
            str(t): sum(1 for lk in ranked if lk.basis.tier == t) for t in (3, 2, 1)
        },
    }
    return CorrelationResult(links=ranked, summary=summary)


__all__ = [
    "Correland",
    "CorrelationBasis",
    "CorrelationLink",
    "CorrelationResult",
    "correlate_surfaces",
]
