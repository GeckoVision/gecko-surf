"""What the living file keeps, through the supplier watch's store.

Per run, in ``~/gecko-lists/``: ``<run>-<symbol>.xstock-file.csv`` (what a person
reads), ``.xstock-file.parquet`` (the same rows plus run id, mode and time, for the next
run's previous version and for ``changes_since``) and ``.xstock-file.receipt.json``
(request ids, cents, sources, no values). Written by ``store.write_files``, read back by
``store.read_runs`` with this job's suffix.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from ..orthogonal.supplier import parquet
from ..orthogonal.supplier.store import Written, read_runs, write_files
from ..orthogonal.supplier.text import csv_safe
from .facts import FactRow

SUFFIXES = (".xstock-file.csv", ".xstock-file.parquet", ".xstock-file.receipt.json")
SUBJECT_KEY = "mint"
CSV_HEADER = (
    "symbol",
    "mint",
    "field",
    "value",
    "status",
    "check",
    "changed",
    "previous value",
    "previous run",
    "source",
    "fetched at",
    "note",
    "summary",
)
COLUMNS: parquet.Columns = (
    ("run_id", "string"),
    ("mode", "string"),
    ("generated_at", "string"),
    ("mint", "string"),
    ("symbol", "string"),
    ("field", "string"),
    ("kind", "string"),
    ("value", "string"),
    ("value_summary", "string"),
    ("model_summary", "string"),
    ("source", "string"),
    ("fetched_at", "string"),
    ("status", "string"),
    ("previous_value", "string"),
    ("previous_run_id", "string"),
    ("changed", "bool"),
    ("check", "string"),
    ("note", "string"),
    ("stale_since", "string"),
)


def table_csv(rows: list[FactRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for r in rows:
        cells = (
            r.symbol,
            r.mint,
            r.field,
            r.value or "",
            r.status,
            r.check or "",
            "yes" if r.changed else "",
            r.previous_value or "",
            r.previous_run_id or "",
            r.source,
            r.fetched_at or "",
            r.note,
            r.value_summary,
        )
        writer.writerow([csv_safe(c) for c in cells])
    return buffer.getvalue()


def records(
    rows: list[FactRow], *, run_id: str, mode: str, generated_at: str
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "mode": mode,
            "generated_at": generated_at,
            **{name: getattr(r, name) for name, _kind in COLUMNS[3:]},
        }
        for r in rows
    ]


def write(
    folder: Path,
    run_id: str,
    symbol: str,
    rows: list[FactRow],
    receipt: dict[str, Any],
    *,
    mode: str,
    generated_at: str,
) -> Written:
    return write_files(
        folder,
        f"{run_id}-{symbol}",
        csv_text=table_csv(rows),
        records=records(rows, run_id=run_id, mode=mode, generated_at=generated_at),
        columns=COLUMNS,
        receipt=receipt,
        suffixes=SUFFIXES,
    )


def stored_rows(folder: Path) -> list[dict[str, Any]]:
    return read_runs(folder, suffix=SUFFIXES[1])
