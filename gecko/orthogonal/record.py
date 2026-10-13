"""What the job keeps: a three-column list and a receipt with no names in it.

The list holds ``handle, match, post_id`` and nothing else — no comment text, no display
name, nothing from her sheet. The receipt holds request ids and cents, never a handle.
Both are deleted after ``RETENTION_DAYS``; the sweep runs at the start of every job.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .match import MatchRow
from .paginate import CallLine

RETENTION_DAYS = 30
LIST_SUFFIX = ".gecko-list.csv"
RECEIPT_SUFFIX = ".gecko-receipt.json"


@dataclass
class Receipt:
    mode: str
    post_id: str
    endpoint: str
    calls: list[CallLine] = field(default_factory=list)
    quoted_cents: int = 0
    charged_cents: int = 0
    balance_before: str | None = None
    balance_after: str | None = None
    pages: int = 0
    stop_reason: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    list_sha256: str | None = None
    rehearsal_spent_cents: int = 0
    notices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def list_csv(rows: list[MatchRow], post_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["handle", "match", "post_id"])
    for row in rows:
        writer.writerow([row.handle, row.match, post_id])
    return buffer.getvalue()


def write_outputs(
    folder: Path, rows: list[MatchRow], receipt: Receipt, now: float
) -> tuple[Path, Path]:
    """Write the list and the receipt; set ``receipt.list_sha256`` to the list's hash."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    stem = f"{receipt.post_id}-{stamp}"
    text = list_csv(rows, receipt.post_id)
    receipt.list_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    list_path = folder / f"{stem}{LIST_SUFFIX}"
    receipt_path = folder / f"{stem}{RECEIPT_SUFFIX}"
    list_path.write_text(text, encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt.to_dict(), indent=2), encoding="utf-8")
    return list_path, receipt_path


def prune_outputs(folder: Path, now: float, days: int = RETENTION_DAYS) -> int:
    """Delete OUR files older than ``days``. Never touches anything else in the folder."""
    if not folder.is_dir():
        return 0
    cutoff = now - days * 86400
    removed = 0
    for path in folder.iterdir():
        if not path.name.endswith((LIST_SUFFIX, RECEIPT_SUFFIX)) or path.is_symlink():
            continue
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
