"""The planner's catalog, read from the registry instead of from files.

:func:`load_from_registry` returns the same :class:`LoadedCatalog` that
:func:`gecko.anyquery.catalog_load.load_catalog` returns, by rebuilding the inputs that
function reads (a ``/v1/details``-shaped record per Orthogonal sighting, a pay.sh
``CatalogEntry`` per pay.sh sighting, the observed paths) and handing them to the SAME
admission rules in :mod:`gecko.anyquery.candidates`. Nothing here decides what the
planner may pick; the planner, the validator and retrieval are untouched.

One difference, and it cannot reach a plan: ``confirmed`` holds no pair for
Orthogonal's billing envelope (``credits_consumed``, ...), because the registry never
records envelope keys as fields. The validator only looks up pairs whose path is in a
candidate's ``response_paths``, and envelope paths never are.

``now`` excludes an operation whose health row holds it in quarantine past ``now``
(a changed ``payTo``). No importer writes health yet, so today nothing is excluded.

Which loader runs is :data:`CATALOG_ENV`: unset or ``files`` keeps the file loader,
``registry`` reads Mongo. Anything else is refused rather than read as ``files``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from gecko.anyquery.candidates import (
    PoolReport,
    candidate_set,
    confirmed_pairs,
    orthogonal_candidates,
    paysh_candidates,
)
from gecko.anyquery.catalog_load import LoadedCatalog
from gecko.anyquery.types import Storage
from gecko.paysh_catalog import CatalogEntry

from .registry import Registry, RegistryError
from .types import EVIDENCE_RANK, FieldObs, Sighting

__all__ = [
    "CATALOG_ENV",
    "CatalogSource",
    "CatalogSourceError",
    "catalog_source",
    "load_from_registry",
]

CATALOG_ENV = "GECKO_ANYQUERY_CATALOG"
CatalogSource = Literal["files", "registry"]
_OBSERVED = EVIDENCE_RANK["EXTRACTED"]


class CatalogSourceError(RegistryError):
    """``GECKO_ANYQUERY_CATALOG`` holds a value that names no loader."""


def catalog_source(env: Mapping[str, str]) -> CatalogSource:
    raw = (env.get(CATALOG_ENV) or "files").strip().lower()
    if raw == "files":
        return "files"
    if raw == "registry":
        return "registry"
    raise CatalogSourceError(f"{CATALOG_ENV} must be 'files' or 'registry'")


def _excluded(registry: Registry, now: str) -> frozenset[str]:
    return frozenset(
        h.op_id
        for h in registry.health()
        if h.quarantined_until is not None and h.quarantined_until > now
    )


def _details_record(sighting: Sighting, claimed: list[str]) -> dict[str, Any]:
    claim = sighting.claimed

    def params(location: str) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "required": p.required,
                "type": p.kind,
                "description": p.note,
            }
            for p in claim.params
            if p.location == location
        ]

    return {
        "api": sighting.api,
        "details": {
            "endpoint": {
                "method": sighting.method,
                "path": sighting.path,
                "description": claim.description,
                "hasDynamicPricing": claim.dynamic_price,
                # usd_to_cents rounds up after round(..., 6), so cents survive the trip.
                "price": None if claim.price_cents is None else claim.price_cents / 100,
                "queryParams": params("query"),
                "bodyParams": params("body"),
                "pathParams": params("path"),
                "responseParams": [{"name": p} for p in claimed],
            }
        },
    }


def _paysh_entry(sighting: Sighting) -> CatalogEntry:
    claim = sighting.claimed
    return CatalogEntry(
        fqn=sighting.api,
        title=claim.title,
        service_url=claim.service_url or "",
        description=claim.description,
        use_case=claim.use_case,
        category="",
        endpoint_count=0,
        min_price_usd=(claim.min_price_cents or 0) / 100,
        max_price_usd=(claim.price_cents or 0) / 100,
        has_free_tier=False,
        has_metering=False,
        sha="",
    )


def load_from_registry(
    registry: Registry,
    *,
    now: str,
    storage: Mapping[str, Storage] | None = None,
) -> LoadedCatalog:
    excluded = _excluded(registry, now)
    by_op: dict[str, list[FieldObs]] = {}
    for obs in registry.fields():
        by_op.setdefault(obs.op_id, []).append(obs)

    records: list[dict[str, Any]] = []
    observed: dict[tuple[str, str], frozenset[str]] = {}
    for sighting in sorted(
        registry.sightings("orthogonal"), key=lambda s: s.sighting_id
    ):
        if sighting.op_id in excluded:
            continue
        rows = by_op.get(sighting.op_id, [])
        seen = sorted(o.path for o in rows if EVIDENCE_RANK[o.evidence] >= _OBSERVED)
        claimed = sorted(
            o.path
            for o in rows
            if EVIDENCE_RANK[o.evidence] < _OBSERVED and "orthogonal" in o.claimed_by
        )
        records.append(_details_record(sighting, claimed))
        if seen:
            key = (sighting.api, sighting.path)
            observed[key] = observed.get(key, frozenset()) | frozenset(seen)

    report = PoolReport()
    pool = orthogonal_candidates(
        records, observed=observed, storage=storage, report=report
    )
    pool += paysh_candidates(
        _paysh_entry(s)
        for s in sorted(registry.sightings("paysh"), key=lambda s: s.sighting_id)
        if s.op_id not in excluded
    )
    return LoadedCatalog(candidate_set(pool), confirmed_pairs(observed), report)
