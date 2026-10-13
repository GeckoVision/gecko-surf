"""The supplier dataset as an API: latest, history, changes since a run, sources.

Free and read-only: every answer comes from the stored Parquet runs in the lists
folder and the field ledger — no call, no spend. Rows go through
``history.safe_row``, so a tool result carries code-built summaries, sources and
states, never upstream text. Runs are read in the surface's own mode: a recorded
surface never answers from live runs, nor the other way round.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .deps import SupplierDeps
from .drift import baseline_from
from .history import changes_since, history, latest
from .sources import SOURCE_MAP, UnknownFieldError, equivalences_for
from .store import read_runs

#: Sorts after every real run id, so every stored run counts as "before".
_AFTER_ALL = "~"


def _rows(deps: SupplierDeps) -> list[dict[str, Any]]:
    return [r for r in read_runs(deps.base.folder) if r.get("mode") == deps.mode]


def supplier_latest(deps: SupplierDeps) -> dict[str, Any]:
    return {"mode": deps.mode, **latest(_rows(deps), deps.mode)}


def supplier_history(deps: SupplierDeps, supplier: str, field: str) -> dict[str, Any]:
    rows = history(_rows(deps), supplier, field)
    out: dict[str, Any] = {"mode": deps.mode, "supplier": supplier, "field": field}
    out["runs"] = rows
    if not rows:
        out["note"] = "No stored rows for that supplier and field in this mode."
    return out


def supplier_changes_since(deps: SupplierDeps, run_id: str) -> dict[str, Any]:
    return {"mode": deps.mode, **changes_since(_rows(deps), run_id).to_dict()}


def supplier_sources(deps: SupplierDeps, field: str | None = None) -> dict[str, Any]:
    """The ranked sources per mapped field, with what the last run saw of each."""
    names = [field] if field else list(SOURCE_MAP)
    for name in names:
        if name not in SOURCE_MAP:
            raise UnknownFieldError(name)
    prints = baseline_from(
        deps.field_ledger.rows(), mode=deps.mode, before_run=_AFTER_ALL
    ).prints
    fields = []
    for name in names:
        ranked = []
        for rank, src in enumerate(SOURCE_MAP[name].sources, start=1):
            seen = prints.get(src.endpoint)
            ranked.append(
                {
                    "rank": rank,
                    "source": src.name,
                    "provider": src.provider,
                    "kind": src.kind,
                    "cents": src.price_cents,
                    "field_path": src.field_path,
                    "reads": src.reads,
                    "evidence": src.evidence,
                    "last_seen": (
                        {
                            "run_id": seen.run_id,
                            "path_present": src.field_path in seen.fields,
                        }
                        if seen
                        else None
                    ),
                }
            )
        fields.append(
            {
                "field": name,
                "sources": ranked,
                "equivalences": [asdict(e) for e in equivalences_for(name)],
            }
        )
    return {"mode": deps.mode, "fields": fields}
