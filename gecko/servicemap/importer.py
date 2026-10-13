"""Write reader output into a registry, catalog claims first and observations last.

The order matters only for counting: the merge (:mod:`gecko.servicemap.merge`) gives
the same row in any order, because evidence only rises. An observed listing that no
catalog sighting names is counted and skipped, not invented: a ledger row carries no
method, so it cannot name an operation on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gecko.anyquery.candidates import load_details_dir
from gecko.anyquery.catalog_load import read_jsonl

from .readers import (
    ObservedFields,
    OperationDraft,
    ReadReport,
    observed_fields,
    orthogonal_sightings,
    paysh_entries_from_text,
    paysh_sightings,
)
from .registry import Registry
from .types import FieldObs, Operation, Service, Sighting

__all__ = ["ImportCounts", "import_files", "import_observed", "import_sightings"]


@dataclass
class ImportCounts:
    sightings: dict[str, int] = field(default_factory=dict)  # per catalog
    operations: int = 0
    services: int = 0
    claimed_fields: int = 0
    observed_fields: int = 0
    observed_unmatched: int = 0  # ledger listings no sighting names
    skipped: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [f"sightings {k}: {v}" for k, v in sorted(self.sightings.items())]
        out += [
            f"operations: {self.operations}",
            f"services: {self.services}",
            f"claimed field rows: {self.claimed_fields}",
            f"observed field rows: {self.observed_fields}",
            f"observed listings with no sighting: {self.observed_unmatched}",
        ]
        out += [f"skipped ({k}): {v}" for k, v in sorted(self.skipped.items())]
        return out


def import_sightings(
    registry: Registry,
    pairs: Iterable[tuple[Sighting, OperationDraft]],
    *,
    now: str,
    counts: ImportCounts | None = None,
) -> ImportCounts:
    tally = counts if counts is not None else ImportCounts()
    ops: set[str] = set()
    services: set[str] = set()
    for sighting, draft in pairs:
        registry.put_service(
            Service(draft.service_id, draft.service_id, None, now, now)
        )
        registry.put_operation(
            Operation(
                draft.op_id,
                draft.service_id,
                draft.method,
                draft.url_template,
                now,
                now,
            )
        )
        registry.put_sighting(sighting)
        for obs in draft.claimed_fields(sighting.catalog, now):
            registry.put_field(obs)
            tally.claimed_fields += 1
        tally.sightings[sighting.catalog] = tally.sightings.get(sighting.catalog, 0) + 1
        ops.add(draft.op_id)
        services.add(draft.service_id)
    tally.operations += len(ops)
    tally.services += len(services)
    return tally


def import_observed(
    registry: Registry,
    observed: Iterable[ObservedFields],
    *,
    now: str,
    counts: ImportCounts | None = None,
) -> ImportCounts:
    """Observed paths -> ``EXTRACTED`` fields on the operation each listing names."""
    tally = counts if counts is not None else ImportCounts()
    listing: dict[tuple[str, str, str], set[str]] = {}
    for s in registry.sightings():
        listing.setdefault((s.catalog, s.api, s.path), set()).add(s.op_id)
    for obs in observed:
        ops = listing.get((obs.catalog, obs.api, obs.path))
        if not ops:
            tally.observed_unmatched += 1
            continue
        first, last = obs.first_seen or now, obs.last_seen or now
        for op in sorted(ops):
            for path in sorted(obs.paths):
                registry.put_field(
                    FieldObs(op, path, None, "EXTRACTED", (), first, last, None)
                )
                tally.observed_fields += 1
    return tally


def import_files(
    registry: Registry,
    *,
    details: Path | Iterable[Mapping[str, Any]],
    ledgers: Iterable[Path] = (),
    paysh_catalog: Path | None = None,
    now: str,
) -> ImportCounts:
    """Everything ``load_catalog`` reads, into ``registry``. ``details`` is a folder of
    ``/v1/details`` files or the records already loaded, as for ``load_catalog``."""
    counts = ImportCounts()
    records: list[Mapping[str, Any]]
    if isinstance(details, Path):
        records = list(load_details_dir(details))
        found = f"file:{details.name}"
    else:
        records, found = list(details), "file:records"
    report = ReadReport()
    import_sightings(
        registry,
        orthogonal_sightings(records, now=now, found_url=found, report=report),
        now=now,
        counts=counts,
    )
    if paysh_catalog is not None:
        entries = paysh_entries_from_text(paysh_catalog.read_text(encoding="utf-8"))
        import_sightings(
            registry, paysh_sightings(entries, now=now), now=now, counts=counts
        )
    counts.skipped.update(report.skipped)
    rows: list[dict[str, Any]] = []
    for path in ledgers:
        rows.extend(read_jsonl(path))
    import_observed(registry, observed_fields(rows), now=now, counts=counts)
    return counts
