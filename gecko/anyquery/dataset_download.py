"""The dataset downloads: ``GET /d/<id>.json`` and ``GET /d/<id>.parquet``.

The current table, long (one row per cell, the snapshot's columns), masked unless the
caller asks (``?reveal_pii=true``) AND the key is the owner's own (founder ruling, 12 Oct
2026: only the customer's key gets the unmasked Parquet; a reader key always gets masked
data). It reads ``dataset_rows``, the table the latest version wrote, so it needs no
snapshot sink; the write-once snapshot stays storage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from gecko.orthogonal.supplier import parquet

from .dataset_read import (
    ALL_ROWS,
    DatasetCaller,
    ReadRefusal,
    latest_version,
    readable,
    shown_subject,
)
from .reader_keys import shown_value
from .snapshots import SNAPSHOT_COLUMNS, snapshot_rows
from .store import Store, StoreError

__all__ = ["Download", "DownloadFormat", "dataset_download"]


DownloadFormat = Literal["json", "parquet"]


@dataclass(frozen=True)
class Download:
    body: bytes
    media_type: str
    filename: str


def dataset_download(
    store: Store, caller: DatasetCaller | None, fmt: DownloadFormat, *, reveal: bool
) -> Download | dict[str, Any]:
    """The current table, long (one row per cell, the snapshot's columns), masked unless
    ``reveal`` is asked AND the key is the owner's. A refusal comes back as a dict."""
    try:
        dataset, access = readable(store, caller)
        version = latest_version(dataset)
        rows = store.dataset_rows(dataset.customer, dataset.dataset_id, limit=ALL_ROWS)
    except ReadRefusal as refusal:
        return refusal.to_dict()
    except StoreError:
        return {
            "error": "The dataset store could not answer.",
            "code": "store_unavailable",
        }
    long = snapshot_rows(dataset, rows, version)
    hid = False
    for row in long:
        row["value"], masked_one = shown_value(
            access, row["field"], row["value"], reveal=reveal
        )
        row["subject"], masked_subject = shown_subject(
            access, row["subject"], reveal=reveal
        )
        hid = hid or masked_one or masked_subject
    name = f"{dataset.dataset_id}-v{version}"
    if fmt == "json":
        body = {
            "dataset_id": dataset.dataset_id,
            "version": version,
            "pii_masked": hid,
            "columns": [c for c, _ in SNAPSHOT_COLUMNS],
            "rows": long,
        }
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        return Download(data, "application/json", f"{name}.json")
    if not parquet.available():
        return {
            "error": "Parquet is not available on this host; use the .json download.",
            "code": "parquet_unavailable",
        }
    metadata = {
        "dataset_id": dataset.dataset_id,
        "version": str(version),
        "rows_sha256": dataset.versions[-1].rows_sha256,
        "pii_masked": "true" if hid else "false",
    }
    data = parquet.table_bytes(long, SNAPSHOT_COLUMNS, metadata=metadata)
    return Download(data, "application/vnd.apache.parquet", f"{name}.parquet")
