"""Parquet snapshots of a dataset version: long format, values whole, write-once.

The round trip is the contract: what the sink holds, read back through
``parquet.read_rows``, equals ``dataset_rows`` cell for cell."""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko.anyquery.datasets import (
    DatasetDeps,
    DatasetError,
    adopt_run,
    new_dataset,
)
from gecko.anyquery.snapshots import (
    SNAPSHOT_COLUMNS,
    LocalFolderSink,
    SnapshotError,
    snapshot_key,
    snapshot_rows,
)
from gecko.anyquery.store import MemoryStore
from gecko.anyquery.store_mongo import db_name_for
from gecko.orthogonal.supplier import parquet
from tests.anyquery_dataset_fixtures import ACCT, NOW, R1, plan, seed

pytest.importorskip("pyarrow")


def _adopted(tmp_path: Path) -> tuple[MemoryStore, LocalFolderSink]:
    store = MemoryStore()
    seed(store)
    store.create_dataset(
        new_dataset(
            plan(),
            dataset_id="lead-contacts",
            bundle="lead-contacts",
            mode="live",
            now=NOW,
        )
    )
    sink = LocalFolderSink(tmp_path)
    adopt_run(DatasetDeps(store, sink, lambda: NOW), ACCT, "lead-contacts", R1)
    return store, sink


def test_table_bytes_round_trips_with_metadata(tmp_path: Path) -> None:
    cols = (("a", "string"), ("n", "int64"), ("ok", "bool"))
    rows = [{"a": "x", "n": 1, "ok": True, "extra": "dropped"}]
    data = parquet.table_bytes(rows, cols, metadata={"k": "v"})
    path = tmp_path / "t.parquet"
    path.write_bytes(data)
    assert parquet.read_rows(path) == [{"a": "x", "n": 1, "ok": True}]
    import pyarrow.parquet as pq

    assert pq.read_schema(path).metadata[b"k"] == b"v"


def test_key_is_the_customer_hash_never_the_account() -> None:
    key = snapshot_key(ACCT, "lead-contacts", 3)
    assert key == f"{db_name_for(ACCT, prefix='')}/lead-contacts/v=3.parquet"
    assert ACCT not in key


def test_round_trip_equals_dataset_rows(tmp_path: Path) -> None:
    store, sink = _adopted(tmp_path)
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None
    version = ds.versions[-1]
    key = snapshot_key(ACCT, "lead-contacts", 1)
    assert version.snapshot_uri == "local://" + key
    read = parquet.read_rows(tmp_path / key)
    expected = snapshot_rows(ds, store.dataset_rows(ACCT, "lead-contacts"), 1)
    assert read == expected and len(read) == 8
    assert [c for c, _ in SNAPSHOT_COLUMNS] == list(read[0])
    assert "customer" not in read[0]
    phone = next(
        r for r in read if r["field"] == "mobile_phone" and r["value"] is not None
    )
    assert phone["value"] == "+44 20 7946 0001" and phone["pii"] is True
    meta = sink.head_meta(key)
    assert meta is not None and meta["rows_sha256"] == version.rows_sha256
    assert meta["dataset_id"] == "lead-contacts" and meta["version"] == "1"
    import pyarrow.parquet as pq

    file_meta = pq.read_schema(tmp_path / key).metadata
    assert file_meta[b"rows_sha256"] == version.rows_sha256.encode()


def test_the_same_rows_written_again_are_adopted(tmp_path: Path) -> None:
    sink = LocalFolderSink(tmp_path)
    first = sink.put_once("h/d/v=1.parquet", b"one", {"rows_sha256": "s"})
    with pytest.raises(SnapshotError):
        sink.put_once("h/d/v=1.parquet", b"two", {"rows_sha256": "s"})
    assert sink.get("h/d/v=1.parquet") == b"one"  # never overwritten
    assert first == "local://h/d/v=1.parquet"


def test_a_clash_with_another_sha_leaves_the_version_pending(tmp_path: Path) -> None:
    store = MemoryStore()
    seed(store)
    store.create_dataset(
        new_dataset(
            plan(),
            dataset_id="lead-contacts",
            bundle="lead-contacts",
            mode="live",
            now=NOW,
        )
    )
    sink = LocalFolderSink(tmp_path)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    sink.put_once(key, b"someone else's bytes", {"rows_sha256": "different"})
    with pytest.raises(DatasetError) as err:
        adopt_run(DatasetDeps(store, sink, lambda: NOW), ACCT, "lead-contacts", R1)
    assert err.value.code == "snapshot_clash"
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None and ds.versions[-1].snapshot_status == "pending"
    assert len(store.dataset_rows(ACCT, "lead-contacts")) == 2  # Mongo still correct


def test_a_clash_with_the_same_sha_is_adopted(tmp_path: Path) -> None:
    store, sink = _adopted(tmp_path)
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None
    # A retry after a crash between the put and the save: the object is already there.
    from dataclasses import replace

    pending = replace(ds.versions[-1], snapshot_status="pending", snapshot_uri=None)
    store.save_dataset(
        replace(ds, versions=(pending,), revision=ds.revision + 1),
        expect_revision=ds.revision,
    )
    from gecko.anyquery.datasets import snapshot_pending

    done = snapshot_pending(
        DatasetDeps(store, sink, lambda: NOW), ACCT, "lead-contacts"
    )
    assert done is not None and done.snapshot_status == "written"


@pytest.mark.parametrize("key", ["../x.parquet", "/abs.parquet", "a/../../b", ""])
def test_local_sink_refuses_a_key_outside_its_root(tmp_path: Path, key: str) -> None:
    with pytest.raises(SnapshotError):
        LocalFolderSink(tmp_path).put_once(key, b"x", {})


def test_local_sink_prefix_operations(tmp_path: Path) -> None:
    sink = LocalFolderSink(tmp_path)
    sink.put_once("h/d/v=1.parquet", b"1", {"rows_sha256": "a"})
    sink.put_once("h/d/v=2.parquet", b"2", {"rows_sha256": "b"})
    sink.put_once("h/e/v=1.parquet", b"3", {"rows_sha256": "c"})
    assert sink.any_under("h/d/") and not sink.any_under("h/z/")
    assert sink.delete_prefix("h/d/") == 2
    assert not sink.any_under("h/d/") and sink.any_under("h/e/")
    assert sink.head_meta("h/d/v=1.parquet") is None
