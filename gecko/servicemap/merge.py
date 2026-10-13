"""How a new reading of a registry row combines with the one already held.

The rule the whole registry rests on: catalog claims are never final, observed fields
win. A field's evidence only ever rises (:data:`EVIDENCE_RANK`), so re-importing a
catalog that still lists a field as ``CLAIMED`` cannot undo a real reply that showed it
(``EXTRACTED``). A sighting's claim is the catalog's latest word, because a claim is
only ever what the catalog says now. Times widen: first seen is the earliest reading,
last seen the latest.

Every store applies these functions, so the memory fake and Mongo cannot disagree.
"""

from __future__ import annotations

from dataclasses import replace

from .types import (
    EVIDENCE_RANK,
    Equivalence,
    Evidence,
    EvidenceLevel,
    FieldObs,
    OfferObs,
    Operation,
    Service,
    Sighting,
)

__all__ = [
    "merge_equivalence",
    "merge_field",
    "merge_offer",
    "merge_operation",
    "merge_service",
    "merge_sighting",
    "stronger",
]


def _earliest(a: str, b: str) -> str:
    return min(a, b)


def _latest(a: str, b: str) -> str:
    return max(a, b)


def _latest_opt(a: str | None, b: str | None) -> str | None:
    present = [t for t in (a, b) if t is not None]
    return max(present) if present else None


def stronger(a: Evidence, b: Evidence) -> Evidence:
    return a if EVIDENCE_RANK[a] >= EVIDENCE_RANK[b] else b


def merge_service(held: Service | None, new: Service) -> Service:
    if held is None:
        return new
    return replace(
        new,
        homepage=new.homepage or held.homepage,
        first_seen=_earliest(held.first_seen, new.first_seen),
        last_seen=_latest(held.last_seen, new.last_seen),
    )


def merge_operation(held: Operation | None, new: Operation) -> Operation:
    if held is None:
        return new
    return replace(
        held,
        first_seen=_earliest(held.first_seen, new.first_seen),
        last_seen=_latest(held.last_seen, new.last_seen),
    )


def merge_sighting(held: Sighting | None, new: Sighting) -> Sighting:
    if held is None:
        return new
    return replace(
        new,
        first_seen=_earliest(held.first_seen, new.first_seen),
        last_seen=_latest(held.last_seen, new.last_seen),
    )


def merge_field(held: FieldObs | None, new: FieldObs) -> FieldObs:
    if held is None:
        return new
    evidence = stronger(held.evidence, new.evidence)
    # A type comes from the stronger reading; a claim never overwrites an observed type.
    if EVIDENCE_RANK[new.evidence] >= EVIDENCE_RANK[held.evidence]:
        json_type = new.json_type or held.json_type
    else:
        json_type = held.json_type or new.json_type
    return replace(
        held,
        json_type=json_type,
        evidence=evidence,
        claimed_by=tuple(sorted(set(held.claimed_by) | set(new.claimed_by))),
        first_seen=_earliest(held.first_seen, new.first_seen),
        last_seen=_latest(held.last_seen, new.last_seen),
        last_missing=_latest_opt(held.last_missing, new.last_missing),
    )


def merge_offer(held: OfferObs | None, new: OfferObs) -> OfferObs:
    if held is None:
        return new
    return replace(
        new,
        evidence=stronger(held.evidence, new.evidence),
        first_seen=_earliest(held.first_seen, new.first_seen),
        last_seen=_latest(held.last_seen, new.last_seen),
    )


def merge_equivalence(held: Equivalence | None, new: Equivalence) -> Equivalence:
    if held is None:
        return new
    level: EvidenceLevel = (
        "confirmed" if "confirmed" in (held.level, new.level) else "declared"
    )
    return replace(new, level=level, agreements=max(held.agreements, new.agreements))
