"""What a supplier watch keeps, and how to get it back.

Per run, in ``~/gecko-lists/``: ``<run>.supplier-watch.csv`` (the columns the person
reads, with each field's status, the source that filled it and a note),
``<run>.supplier-watch.parquet`` (the same rows plus run id, mode, time, the canonical
value and the model-safe summary, for retrieval and the next run's snapshot) and
``<run>.supplier-watch.receipt.json`` (request ids and cents, no values).

Per call, in ``~/.gecko/orthogonal/``: one row appended to ``field-ledger.jsonl`` —
endpoint, input field NAMES, output field NAMES present, status, quoted vs charged
cents, continuation, and a hashed supplier key (``calls.subject_of``). Per mapped field,
one ``kind="field"`` row: which source filled it, whether a failover happened, why. No
values. ``field-ledger.parquet`` is REWRITTEN from the whole
JSONL after each run (the JSONL is the record; the Parquet is a copy of it), which is
simpler than appending to Parquet and cannot drift from it.

Unlike the commenter list, these files are not swept after 30 days: they hold company
facts the person asked to keep, not other people's handles.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..sheet import default_folder
from . import parquet
from .fields import FieldRow
from .text import csv_safe

TABLE_SUFFIX = ".supplier-watch.csv"
PARQUET_SUFFIX = ".supplier-watch.parquet"
RECEIPT_SUFFIX = ".supplier-watch.receipt.json"
CSV_HEADER = (
    "supplier",
    "field",
    "value summary",
    "source",
    "second source",
    "agreement",
    "status",
    "filled by",
    "note",
)


def default_state_dir() -> Path:
    return Path.home() / ".gecko" / "orthogonal"


def table_csv(rows: list[FieldRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [
                csv_safe(cell)
                for cell in (
                    row.supplier,
                    row.field,
                    row.value_summary,
                    row.source,
                    row.second_source,
                    row.agreement,
                    row.state,
                    row.filled_by,
                    row.note,
                )
            ]
        )
    return buffer.getvalue()


def table_records(
    rows: list[FieldRow], *, run_id: str, mode: str, generated_at: str
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "mode": mode,
            "generated_at": generated_at,
            "supplier": r.supplier,
            "field": r.field,
            "value_summary": r.value_summary,
            "source": r.source,
            "second_source": r.second_source,
            "agreement": r.agreement,
            "model_summary": r.model_summary,
            "value": r.value,
            "filled_by": r.filled_by,
            "failover": r.failover,
            "note": r.note,
            "status": r.state,
            "stale_since": r.stale_since,
        }
        for r in rows
    ]


@dataclass
class Written:
    table_path: Path
    receipt_path: Path
    table_sha256: str
    parquet_path: Path | None = None
    notices: list[str] = field(default_factory=list)


def write_files(
    folder: Path,
    stem: str,
    *,
    csv_text: str,
    records: list[dict[str, Any]],
    columns: parquet.Columns,
    receipt: dict[str, Any],
    suffixes: tuple[str, str, str] = (TABLE_SUFFIX, PARQUET_SUFFIX, RECEIPT_SUFFIX),
) -> Written:
    """Any job's run: CSV + receipt always; Parquet when pyarrow is there, else a notice.

    ``suffixes`` is (table, parquet, receipt), so each job keeps its own files and its
    own ``read_runs`` glob in the one lists folder.
    """
    table_suffix, parquet_suffix, receipt_suffix = suffixes
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    table_path = folder / f"{stem}{table_suffix}"
    table_path.write_text(csv_text, encoding="utf-8")
    written = Written(table_path, folder / f"{stem}{receipt_suffix}", digest)
    try:
        written.parquet_path = parquet.write_rows(
            folder / f"{stem}{parquet_suffix}", records, columns
        )
    except parquet.ParquetUnavailableError as exc:
        written.notices.append(exc.message)
    receipt = {
        **receipt,
        "table_sha256": digest,
        "table_path": str(table_path),
        "parquet_path": str(written.parquet_path) if written.parquet_path else None,
    }
    written.receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return written


def write_run(
    folder: Path,
    run_id: str,
    rows: list[FieldRow],
    receipt: dict[str, Any],
    *,
    mode: str,
    generated_at: str,
) -> Written:
    """The supplier watch's files for one run (``write_files`` with its columns)."""
    return write_files(
        folder,
        run_id,
        csv_text=table_csv(rows),
        records=table_records(
            rows, run_id=run_id, mode=mode, generated_at=generated_at
        ),
        columns=parquet.TABLE_COLUMNS,
        receipt=receipt,
    )


def read_runs(
    folder: Path | None = None, *, suffix: str = PARQUET_SUFFIX
) -> list[dict[str, Any]]:
    """Every stored row of one job (by Parquet ``suffix``), oldest run first.

    Needs pyarrow."""
    folder = folder if folder is not None else default_folder()
    if not parquet.available():
        # Say so up front: an empty answer would read as "no runs yet".
        raise parquet.ParquetUnavailableError()
    if not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob(f"*{suffix}")):
        if path.is_file() and not path.is_symlink():
            rows.extend(parquet.read_rows(path))
    return rows


# --- the field ledger: names and counts, never values ------------------------------


class FieldLedger(Protocol):
    def append(self, rows: list[dict[str, Any]]) -> list[str]: ...

    def rows(self) -> list[dict[str, Any]]: ...


@dataclass
class MemoryFieldLedger:
    """Recorded runs and tests: made-up calls never reach the real ledger."""

    kept: list[dict[str, Any]] = field(default_factory=list)

    def append(self, rows: list[dict[str, Any]]) -> list[str]:
        self.kept.extend(rows)
        return []

    def rows(self) -> list[dict[str, Any]]:
        return list(self.kept)


@dataclass
class FileFieldLedger:
    """``<stem>.jsonl`` (appended) and ``<stem>.parquet`` (rewritten).

    ``stem`` lets a second job keep its own ledger, so one job's calls never become
    the other's drift baseline.
    """

    directory: Path = field(default_factory=default_state_dir)
    stem: str = "field-ledger"

    @property
    def jsonl_path(self) -> Path:
        return self.directory / f"{self.stem}.jsonl"

    @property
    def parquet_path(self) -> Path:
        return self.directory / f"{self.stem}.parquet"

    def append(self, rows: list[dict[str, Any]]) -> list[str]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        try:
            parquet.write_rows(self.parquet_path, self.rows(), parquet.LEDGER_COLUMNS)
        except parquet.ParquetUnavailableError as exc:
            return [exc.message]
        return []

    def rows(self) -> list[dict[str, Any]]:
        if not self.jsonl_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn line is skipped, not fatal: the ledger is a record
            if isinstance(row, dict):
                out.append(row)
        return out
