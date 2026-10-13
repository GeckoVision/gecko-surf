"""Load the planner's catalog from disk: Orthogonal details, field ledgers, pay.sh.

Reads files only. Nothing here fetches: the details pull, the field ledgers and the
pay.sh catalog are produced elsewhere (a founder-run pull, live runs, ``fetch_catalog``)
and handed in.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paysh_catalog import CatalogEntry as PayshEntry
from .candidates import (
    PoolReport,
    candidate_set,
    confirmed_pairs,
    load_details_dir,
    load_observed_paths,
    orthogonal_candidates,
    paysh_candidates,
)
from .types import CandidateSet, Storage

__all__ = ["LoadedCatalog", "load_catalog", "read_jsonl"]

_MAX_LEDGER_BYTES = 20_000_000


@dataclass(frozen=True)
class LoadedCatalog:
    candidates: CandidateSet
    confirmed: frozenset[tuple[str, str]]
    report: PoolReport


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Rows of a JSONL file; a missing, oversized or broken file or line is skipped."""
    if not path.is_file() or path.stat().st_size > _MAX_LEDGER_BYTES:
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_catalog(
    details: Path | Iterable[Mapping[str, Any]],
    *,
    ledgers: Iterable[Path] = (),
    paysh: Iterable[PayshEntry] = (),
    storage: Mapping[str, Storage] | None = None,
) -> LoadedCatalog:
    """``details`` is a directory of ``/v1/details`` JSON files, or the records already
    loaded (a committed fixture)."""
    records = load_details_dir(details) if isinstance(details, Path) else list(details)
    rows: list[dict[str, Any]] = []
    for path in ledgers:
        rows.extend(read_jsonl(path))
    observed = load_observed_paths(rows)
    report = PoolReport()
    pool = orthogonal_candidates(
        records, observed=observed, storage=storage, report=report
    )
    pool += paysh_candidates(paysh)
    return LoadedCatalog(candidate_set(pool), confirmed_pairs(observed), report)
