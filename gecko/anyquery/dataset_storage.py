"""Where a dataset's snapshots live, and how many bytes a customer holds.

Two things, both explicit:

- :func:`upload_pending` moves a version's ``local://`` snapshot to the remote sink (S3),
  byte for byte, and writes any version still ``pending``. It is never called on its
  own: a founder runs it (``scripts/dataset_backfill.py --upload-pending``). The local
  file must still say the version's ``rows_sha256``, and a remote object already under
  the key is adopted only when it says the same (the write-once clash rule).
- :func:`storage_usage` measures one customer's dataset storage (their table rows plus
  their snapshot bytes) for the free plan's 500 MB (founder ruling, 12 Oct 2026). It
  measures; nothing here refuses a write on it.

Nothing here spends, plans, quotes or calls a source.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from .datasets import (
    DatasetDeps,
    DatasetError,
    put_or_adopt,
    snapshot_pending,
)
from .snapshots import LOCAL_SCHEME, SnapshotSink, customer_prefix, snapshot_key
from .store import Store
from .types import Dataset

__all__ = [
    "FREE_PLAN_DATASET_BYTES",
    "StorageUsage",
    "local_uploads",
    "storage_usage",
    "upload_pending",
]

#: The free plan's dataset storage (founder ruling, 12 Oct 2026): 500 MB, decimal.
#: Reported beside the measure; NOT enforced anywhere yet.
FREE_PLAN_DATASET_BYTES = 500_000_000
#: Enough to read every row of any dataset: subjects are capped far below this.
_ALL_ROWS = 10_000


def local_uploads(dataset: Dataset) -> list[tuple[int, str]]:
    """The versions whose snapshot is still a ``local://`` file, as (n, key)."""
    return [
        (v.n, v.snapshot_uri.removeprefix(LOCAL_SCHEME))
        for v in dataset.versions
        if v.snapshot_status == "written"
        and v.snapshot_uri is not None
        and v.snapshot_uri.startswith(LOCAL_SCHEME)
    ]


def _load(store: Store, account: str, dataset_id: str) -> Dataset:
    dataset = store.dataset(account, dataset_id)
    if dataset is None:
        raise DatasetError("dataset_not_found", f"No dataset {dataset_id!r}.")
    return dataset


def upload_pending(
    deps: DatasetDeps, local: SnapshotSink, account: str, dataset_id: str
) -> list[int]:
    """Put every snapshot not yet in ``deps.sink`` there; return the versions moved.

    A ``pending`` version is written from its runs (``snapshot_pending``). A version on
    ``local://`` is uploaded with the local file's own bytes and metadata, after its
    ``rows_sha256`` is checked against the version's, and its URI becomes the remote
    one (the version's content does not change). The dataset is saved under the
    optimistic lock after each version, so a crash leaves at most one to redo."""
    remote = deps.sink
    if remote is None:
        raise DatasetError("no_sink", "Uploading needs a remote snapshot sink.")
    dataset = _load(deps.store, account, dataset_id)
    moved = [v.n for v in dataset.versions if v.snapshot_status == "pending"]
    if moved:
        snapshot_pending(deps, account, dataset_id)
        dataset = _load(deps.store, account, dataset_id)
    for n, key in local_uploads(dataset):
        version = dataset.versions[n - 1]
        if key != snapshot_key(dataset.customer, dataset.dataset_id, n):
            raise DatasetError(
                "snapshot_mismatch",
                f"Version {n}'s local snapshot is not under this dataset's key.",
            )
        meta = local.head_meta(key)
        if meta is None or meta.get("rows_sha256") != version.rows_sha256:
            raise DatasetError(
                "snapshot_mismatch",
                f"Version {n}'s local snapshot does not say the rows the version "
                "recorded; nothing was uploaded.",
            )
        uri = put_or_adopt(remote, key, local.get(key), meta, version)
        versions = list(dataset.versions)
        versions[n - 1] = replace(version, snapshot_uri=uri)
        saved = replace(
            dataset,
            versions=tuple(versions),
            revision=dataset.revision + 1,
            updated_at=deps.clock(),
        )
        deps.store.save_dataset(saved, expect_revision=dataset.revision)
        dataset = saved
        moved.append(n)
    return sorted(moved)


@dataclass(frozen=True)
class StorageUsage:
    """One customer's dataset storage, in bytes. Counts only: no ids, no values."""

    datasets: int
    rows: int
    #: The dataset documents and their table rows, as canonical UTF-8 JSON. A proxy for
    #: what Mongo holds (BSON differs by a few bytes per field; indexes are not counted).
    rows_bytes: int
    #: The snapshot objects under the customer's prefix in the sink measured.
    snapshot_bytes: int
    free_plan_bytes: int = FREE_PLAN_DATASET_BYTES

    @property
    def total_bytes(self) -> int:
        return self.rows_bytes + self.snapshot_bytes


def _size(value: object) -> int:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


def storage_usage(
    store: Store, sink: SnapshotSink | None, customer: str
) -> StorageUsage:
    """Measure ``customer``'s dataset storage: rows from the store, snapshot bytes from
    ``sink`` under the customer's own prefix (``None``: snapshots not counted). Reads
    only this customer's database and prefix, so nothing is pooled."""
    datasets = store.datasets(customer)
    rows = 0
    rows_bytes = 0
    for dataset in datasets:
        rows_bytes += _size(asdict(dataset))
        for row in store.dataset_rows(customer, dataset.dataset_id, limit=_ALL_ROWS):
            rows += 1
            rows_bytes += _size(asdict(row))
    snapshot_bytes = sink.bytes_under(customer_prefix(customer)) if sink else 0
    return StorageUsage(
        datasets=len(datasets),
        rows=rows,
        rows_bytes=rows_bytes,
        snapshot_bytes=snapshot_bytes,
    )
