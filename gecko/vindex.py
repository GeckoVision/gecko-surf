"""The value-domain index (§13 Phase 3.3) — "what correlates with X across ALL
connected providers," as a deterministic lookup by DECLARED entity.

Given a set of loaded surfaces, :func:`value_domain_index` builds a plain
``entity -> {producers, consumers}`` map keyed by the surface's declared
value-domain vocabulary. It answers the cross-provider question ("everything that
produces or consumes a ``solana-token-mint``") in one dictionary lookup, and — via
:meth:`ValueDomainIndex.find_correlations` — reports which producer/consumer pairs
form a cross-provider join that is **plan-eligible** now vs a quarantined
**candidate**.

Built ON the shipped primitives — it re-derives no join or trust logic:

  * ``correlate.py`` — ``_outputs`` / ``_inputs`` / ``_declared_split`` enumerate the
    control-plane ``Correland`` descriptors; ``_score`` (the §13.2 ladder) decides
    every cross-provider join's plan-eligibility. Reusing the exact Phase-2 scorer is
    what makes the index and ``correlate_surfaces`` **unable to disagree**.
  * ``surfaces.surface_rev`` + ``graph.declared`` / ``graph.confirmed`` — the content
    address. The index is keyed by ``surface_rev`` + the declared/confirmed vocab, so a
    re-comprehend (new rev) or a re-confirm (new vocab) auto-invalidates a cached index.

**No vectors** (§13.6): fuzzy matching manufactures false cross-API edges, so the index
is a strict lookup by *declared* entity only — a name-only or signature-only match never
creates a cross-provider bucket. **No moat:** the index is re-derivable structure — a
deterministic function of the surfaces + their declared vocabs. Materialize it only as a
cache (:class:`IndexCache`); the sole accreting asset is the customer ``confirmed`` set
(``hints.py``), not this index.

**Control-plane only (invariant #1):** every entry carries surface / op / field NAMES +
the entity + a confirmed flag — never a response value, payload, secret, or any
traffic-derived signal (the ``Correland`` shape structurally forbids it).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .correlate import (
    Correland,
    _declared_split,
    _inputs,
    _outputs,
    _score,
)

if TYPE_CHECKING:
    from .surface import Surface


@dataclass(frozen=True)
class IndexRef:
    """One producer or consumer of an entity — surface / op / field NAMES only.

    ``confirmed`` is the Phase-2 trust bit: ``True`` when the declaration is
    CUSTOMER-vouched (``graph.confirmed``), ``False`` when it is only provider
    x-gecko-declared (untrusted spec). It is the same distinction that gates an
    executable cross-surface plan — never re-introduced here, only surfaced."""

    surface_id: str
    op_id: str
    field: str
    confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface_id,
            "op": self.op_id,
            "field": self.field,
            "confirmed": self.confirmed,
        }


@dataclass(frozen=True)
class EntityGroup:
    """The producers and consumers of one declared entity, across all loaded surfaces."""

    entity: str
    producers: tuple[IndexRef, ...]
    consumers: tuple[IndexRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "producers": [ref.to_dict() for ref in self.producers],
            "consumers": [ref.to_dict() for ref in self.consumers],
        }


@dataclass(frozen=True)
class CrossJoin:
    """A candidate cross-provider join: a producer field on one surface feeding a
    consumer param on another, both sharing a declared entity.

    ``plan_eligible`` is the §13.2/§13.6 verdict straight from the Phase-2 scorer:
    ``True`` ONLY when both sides are customer-confirmed AND the id-types are
    compatible; a provider-only declaration (or a type mismatch) is a quarantined
    candidate a human must confirm before it can drive a plan."""

    entity: str
    producer: IndexRef
    consumer: IndexRef
    plan_eligible: bool

    @property
    def candidate(self) -> bool:
        return not self.plan_eligible

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "producer": self.producer.to_dict(),
            "consumer": self.consumer.to_dict(),
            "plan_eligible": self.plan_eligible,
            "candidate": self.candidate,
        }


def _ref(c: Correland) -> IndexRef:
    return IndexRef(
        surface_id=c.surface_id,
        op_id=c.op_id,
        field=c.name,
        confirmed=c.declared_source == "customer",
    )


def _sort_key(c: Correland) -> tuple[str, str, str]:
    return (c.surface_id, c.op_id, c.name)


@dataclass(frozen=True)
class ValueDomainIndex:
    """The deterministic ``entity -> {producers, consumers}`` map + the cross-provider
    join query. Content-addressed by :attr:`rev` (a re-comprehend or re-confirm bumps
    it). ``to_dict`` is JSON-serializable and control-plane clean (structure only)."""

    rev: str
    # entity -> the id-shaped, declared producer/consumer descriptors. Kept as
    # Correlands (themselves control-plane typed) so ``find_correlations`` can reuse
    # the Phase-2 ``_score`` exactly, instead of re-deriving the trust ladder.
    _producers: dict[str, tuple[Correland, ...]]
    _consumers: dict[str, tuple[Correland, ...]]

    def entities(self) -> tuple[str, ...]:
        """Every declared entity present on at least one loaded surface (sorted)."""
        return tuple(sorted(set(self._producers) | set(self._consumers)))

    def group(self, entity: str) -> EntityGroup | None:
        """The producers/consumers of ``entity`` across all surfaces, or None."""
        prods = self._producers.get(entity, ())
        cons = self._consumers.get(entity, ())
        if not prods and not cons:
            return None
        return EntityGroup(
            entity=entity,
            producers=tuple(_ref(c) for c in prods),
            consumers=tuple(_ref(c) for c in cons),
        )

    def find_correlations(self, entity: str) -> tuple[CrossJoin, ...]:
        """The cross-PROVIDER joins for ``entity``: every producer on surface X feeding
        a consumer on surface Y (X != Y), each tagged plan-eligible vs candidate via the
        Phase-2 scorer. Intra-surface pairs are excluded — this is the cross-provider
        question. A type-incompatible pair is dropped (the §13.2 tier-0 hard gate)."""
        prods = self._producers.get(entity, ())
        cons = self._consumers.get(entity, ())
        joins: list[CrossJoin] = []
        for producer in prods:
            for consumer in cons:
                if producer.surface_id == consumer.surface_id:
                    continue  # cross-provider only
                scored = _score(producer, consumer)
                if scored is None:
                    continue  # type-gated out (never a join key)
                _basis, plan_eligible = scored
                joins.append(
                    CrossJoin(
                        entity=entity,
                        producer=_ref(producer),
                        consumer=_ref(consumer),
                        plan_eligible=plan_eligible,
                    )
                )
        joins.sort(
            key=lambda j: (
                not j.plan_eligible,  # plan-eligible first
                j.producer.surface_id,
                j.producer.op_id,
                j.producer.field,
                j.consumer.surface_id,
                j.consumer.op_id,
                j.consumer.field,
            )
        )
        return tuple(joins)

    def to_dict(self) -> dict[str, Any]:
        """The index as JSON — one entry per entity with its producers/consumers and
        the cross-provider joins. Control-plane clean: names + entity + provenance only."""
        entities: list[dict[str, Any]] = []
        for entity in self.entities():
            group = self.group(entity)
            if group is None:
                continue
            payload = group.to_dict()
            payload["cross_joins"] = [
                j.to_dict() for j in self.find_correlations(entity)
            ]
            entities.append(payload)
        return {"rev": self.rev, "entities": entities}


def _fingerprint(surface: Surface) -> tuple[str, str, list[list[str]], list[list[str]]]:
    """A surface's content address: its id + spec ``surface_rev`` + declared + confirmed
    vocab. Including the vocab means a re-confirm (same spec) still bumps the index rev."""
    graph = surface.graph
    return (
        surface.surface_id,
        surface.client.surface_rev,
        sorted([list(pair) for pair in graph.declared]),
        sorted([list(pair) for pair in graph.confirmed]),
    )


def index_rev(surfaces: Iterable[Surface]) -> str:
    """A stable short content hash of the loaded surfaces — order-independent. Same
    surfaces (specs + declared/confirmed vocab) -> same rev; any change bumps it, so a
    cached index is content-addressed and can never go stale."""
    prints = sorted(_fingerprint(s) for s in surfaces)
    blob = json.dumps(prints, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def value_domain_index(surfaces: Iterable[Surface]) -> ValueDomainIndex:
    """Build the value-domain index from a set of loaded surfaces.

    Each surface contributes its id-shaped, DECLARED producer fields and consumer
    params (a field/param with no declared entity does NOT enter the index — §13.6
    forbids a name-only cross-provider bucket). Deterministic and control-plane only."""
    surfaces = list(surfaces)
    producers: dict[str, list[Correland]] = defaultdict(list)
    consumers: dict[str, list[Correland]] = defaultdict(list)
    for surface in surfaces:
        merged, customer = _declared_split(surface)
        for c in _outputs(surface, merged, customer):
            if c.declared_entity:
                producers[c.declared_entity].append(c)
        for c in _inputs(surface, merged, customer):
            if c.declared_entity:
                consumers[c.declared_entity].append(c)
    return ValueDomainIndex(
        rev=index_rev(surfaces),
        _producers={k: tuple(sorted(v, key=_sort_key)) for k, v in producers.items()},
        _consumers={k: tuple(sorted(v, key=_sort_key)) for k, v in consumers.items()},
    )


class IndexCache:
    """A tiny rev-keyed cache of built indexes (§13 Phase 3.1, scoped honestly).

    The registry itself stays in-memory / per-workspace; this only memoizes the DERIVED
    index by its content address (:func:`index_rev`). A re-comprehend or re-confirm bumps
    the rev -> a cache miss -> a fresh index, so the cache can never serve a stale index.
    A hosted, multi-tenant persistent store is a deployment concern gated on WTP — NOT
    built here (the follow-up). Correlation edges themselves stay stateless (recomputed by
    ``find_correlations`` on demand); a ``(rev_A, rev_B)``-keyed edge cache is a later
    optimization to add only if the O(surfaces²) scan measurably hurts."""

    def __init__(self) -> None:
        self._by_rev: dict[str, ValueDomainIndex] = {}

    def get(self, surfaces: Iterable[Surface]) -> ValueDomainIndex:
        surfaces = list(surfaces)
        rev = index_rev(surfaces)
        cached = self._by_rev.get(rev)
        if cached is not None:
            return cached
        built = value_domain_index(surfaces)
        self._by_rev[rev] = built
        return built


__all__ = [
    "CrossJoin",
    "EntityGroup",
    "IndexCache",
    "IndexRef",
    "ValueDomainIndex",
    "index_rev",
    "value_domain_index",
]
