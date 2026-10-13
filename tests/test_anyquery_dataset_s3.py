"""The S3 snapshot sink, the explicit local -> S3 upload, and the storage byte counter.

Every test injects a fake S3 client: no network, no boto3, no AWS account. The fake
answers the way S3 does for the calls the sink makes (a conditional put that finds the
key taken answers 412 PreconditionFailed), so the write-once and clash rules are tested
against S3's own semantics, not ours."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.config import ConfigError, load_config
from gecko.anyquery.dataset_storage import (
    FREE_PLAN_DATASET_BYTES,
    local_uploads,
    storage_usage,
    upload_pending,
)
from gecko.anyquery.datasets import DatasetDeps, DatasetError, adopt_run, new_dataset
from gecko.anyquery.snapshots import (
    LocalFolderSink,
    S3Sink,
    SnapshotError,
    SnapshotExistsError,
    customer_prefix,
    snapshot_key,
    snapshot_prefix,
)
from gecko.anyquery.store import MemoryStore
from gecko.anyquery.store_mongo import db_name_for
from tests.anyquery_dataset_fixtures import ACCT, NOW, OTHER, R1, plan, seed

BUCKET = "gecko-datasets"
KMS = "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: the sink reads ``.response`` only."""

    def __init__(self, code: str, status: int) -> None:
        self.response = {
            "Error": {"Code": code, "Message": "fake"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        super().__init__(code)


class FakeS3:
    """One bucket in memory. Records every put's arguments so headers can be checked."""

    def __init__(self, bucket: str = BUCKET, *, page: int = 1000) -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.puts: list[dict[str, Any]] = []
        self.delete_calls: list[int] = []
        self.page = page

    def _check(self, bucket: str) -> None:
        if bucket != self.bucket:
            raise FakeClientError("NoSuchBucket", 404)

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self._check(kw["Bucket"])
        self.puts.append(kw)
        if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.objects:
            raise FakeClientError("PreconditionFailed", 412)
        self.objects[kw["Key"]] = (bytes(kw["Body"]), dict(kw.get("Metadata", {})))
        return {"ETag": '"x"'}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check(Bucket)
        if Key not in self.objects:
            raise FakeClientError("404", 404)
        data, meta = self.objects[Key]
        return {"Metadata": dict(meta), "ContentLength": len(data)}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check(Bucket)
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        self._check(kw["Bucket"])
        keys = sorted(k for k in self.objects if k.startswith(kw.get("Prefix", "")))
        start = int(kw.get("ContinuationToken", "0"))
        limit = min(kw.get("MaxKeys", 1000), self.page)
        chunk = keys[start : start + limit]
        out: dict[str, Any] = {
            "KeyCount": len(chunk),
            "Contents": [{"Key": k, "Size": len(self.objects[k][0])} for k in chunk],
            "IsTruncated": start + limit < len(keys),
        }
        if not chunk:
            del out["Contents"]
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + limit)
        return out

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self._check(Bucket)
        assert len(Delete["Objects"]) <= 1000
        self.delete_calls.append(len(Delete["Objects"]))
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Deleted": Delete["Objects"]}


def _sink(fake: FakeS3 | None = None) -> tuple[S3Sink, FakeS3]:
    fake = fake or FakeS3()
    return S3Sink(bucket=BUCKET, kms_key_id=KMS, client=fake), fake


def _dataset_store() -> MemoryStore:
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
    return store


# -- the sink ------------------------------------------------------------------------


def test_put_once_writes_once_with_the_kms_headers() -> None:
    sink, fake = _sink()
    uri = sink.put_once("h/d/v=1.parquet", b"one", {"rows_sha256": "s"})
    assert uri == f"s3://{BUCKET}/h/d/v=1.parquet"
    put = fake.puts[0]
    assert put["IfNoneMatch"] == "*"
    assert put["ServerSideEncryption"] == "aws:kms"
    assert put["SSEKMSKeyId"] == KMS
    assert put["Metadata"] == {"rows_sha256": "s"}
    with pytest.raises(SnapshotExistsError):
        sink.put_once("h/d/v=1.parquet", b"two", {"rows_sha256": "s"})
    assert sink.get("h/d/v=1.parquet") == b"one"  # never overwritten
    assert sink.head_meta("h/d/v=1.parquet") == {"rows_sha256": "s"}
    assert sink.head_meta("h/d/v=2.parquet") is None


def test_a_sink_without_a_kms_key_is_refused() -> None:
    with pytest.raises(SnapshotError):
        S3Sink(bucket=BUCKET, kms_key_id="", client=FakeS3())


def test_an_s3_error_names_no_key_material() -> None:
    sink, _ = _sink(FakeS3(bucket="another-bucket"))
    with pytest.raises(SnapshotError) as err:
        sink.put_once("h/d/v=1.parquet", b"x", {})
    assert KMS not in str(err.value)


def test_a_key_outside_the_sink_is_refused() -> None:
    sink, fake = _sink()
    for bad in ("", "/abs/v=1.parquet", "h/../x", "h//x"):
        with pytest.raises(SnapshotError):
            sink.put_once(bad, b"x", {})
    with pytest.raises(SnapshotError):
        sink.delete_prefix("")
    assert fake.puts == []


def test_adopt_run_through_s3_writes_once() -> None:
    store = _dataset_store()
    sink, fake = _sink()
    deps = DatasetDeps(store, sink, lambda: NOW)
    version = adopt_run(deps, ACCT, "lead-contacts", R1)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    assert version.snapshot_status == "written"
    assert version.snapshot_uri == f"s3://{BUCKET}/{key}"
    assert fake.objects[key][1]["rows_sha256"] == version.rows_sha256
    again = adopt_run(deps, ACCT, "lead-contacts", R1)
    assert again == version and len(fake.puts) == 1


def test_s3_clash_with_the_same_sha_is_adopted() -> None:
    store = _dataset_store()
    sink, fake = _sink()
    deps = DatasetDeps(store, None, lambda: NOW)
    pending = adopt_run(deps, ACCT, "lead-contacts", R1)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    # A retry after a crash between the put and the save: the object is already there.
    fake.objects[key] = (b"earlier bytes", {"rows_sha256": pending.rows_sha256})
    done = adopt_run(replace(deps, sink=sink), ACCT, "lead-contacts", R1)
    assert done.snapshot_status == "written"
    assert done.snapshot_uri == f"s3://{BUCKET}/{key}"
    assert fake.objects[key][0] == b"earlier bytes"  # adopted, not overwritten


def test_s3_clash_with_another_sha_is_refused_and_stays_pending() -> None:
    store = _dataset_store()
    sink, fake = _sink()
    key = snapshot_key(ACCT, "lead-contacts", 1)
    fake.objects[key] = (b"someone else's", {"rows_sha256": "different"})
    with pytest.raises(DatasetError) as err:
        adopt_run(DatasetDeps(store, sink, lambda: NOW), ACCT, "lead-contacts", R1)
    assert err.value.code == "snapshot_clash"
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None and ds.versions[-1].snapshot_status == "pending"
    assert fake.objects[key][0] == b"someone else's"


def test_delete_prefix_empties_only_that_prefix_in_batches() -> None:
    sink, fake = _sink(FakeS3(page=700))
    prefix = snapshot_prefix(ACCT, "lead-contacts")
    for n in range(1, 1502):
        fake.objects[f"{prefix}v={n}.parquet"] = (b"x", {})
    # A sibling whose name starts with the slug must survive: the prefix ends in "/".
    sibling = f"{snapshot_prefix(ACCT, 'lead-contacts-2')}v=1.parquet"
    fake.objects[sibling] = (b"y", {})
    assert sink.delete_prefix(prefix.rstrip("/")) == 1501
    assert all(size <= 1000 for size in fake.delete_calls)
    assert not sink.any_under(prefix)
    assert sink.any_under(snapshot_prefix(ACCT, "lead-contacts-2"))
    assert list(fake.objects) == [sibling]


# -- the explicit upload of local:// snapshots ---------------------------------------


def _local_then_s3(
    tmp_path: Path,
) -> tuple[MemoryStore, LocalFolderSink, S3Sink, FakeS3]:
    store = _dataset_store()
    local = LocalFolderSink(tmp_path)
    adopt_run(DatasetDeps(store, local, lambda: NOW), ACCT, "lead-contacts", R1)
    sink, fake = _sink()
    return store, local, sink, fake


def test_a_local_snapshot_is_uploaded_only_when_asked(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    store, local, sink, fake = _local_then_s3(tmp_path)
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None
    key = snapshot_key(ACCT, "lead-contacts", 1)
    assert ds.versions[0].snapshot_uri == "local://" + key
    assert fake.puts == []  # nothing moves on its own
    assert local_uploads(ds) == [(1, key)]

    deps = DatasetDeps(store, sink, lambda: "2026-10-13T00:00:00Z")
    done = upload_pending(deps, local, ACCT, "lead-contacts")
    assert done == [1]
    after = store.dataset(ACCT, "lead-contacts")
    assert after is not None
    assert after.versions[0].snapshot_uri == f"s3://{BUCKET}/{key}"
    assert after.versions[0].snapshot_status == "written"
    assert after.versions[0].rows_sha256 == ds.versions[0].rows_sha256
    assert after.revision == ds.revision + 1
    assert fake.objects[key][0] == local.get(key)  # the same bytes, not a rebuild
    assert fake.puts[0]["SSEKMSKeyId"] == KMS
    assert local_uploads(after) == []
    # A second call has nothing to do and writes nothing.
    assert upload_pending(deps, local, ACCT, "lead-contacts") == []
    assert len(fake.puts) == 1


def test_upload_adopts_an_equal_object_and_refuses_another(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    store, local, sink, fake = _local_then_s3(tmp_path)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    fake.objects[key] = (b"other", {"rows_sha256": "different"})
    deps = DatasetDeps(store, sink, lambda: NOW)
    with pytest.raises(DatasetError) as err:
        upload_pending(deps, local, ACCT, "lead-contacts")
    assert err.value.code == "snapshot_clash"
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None and ds.versions[0].snapshot_uri == "local://" + key

    meta = local.head_meta(key)
    assert meta is not None
    fake.objects[key] = (local.get(key), meta)
    assert upload_pending(deps, local, ACCT, "lead-contacts") == [1]


def test_upload_refuses_a_local_file_that_no_longer_matches(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    store, local, sink, fake = _local_then_s3(tmp_path)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    meta_path = tmp_path.joinpath(*key.split("/")).with_name("v=1.parquet.meta.json")
    meta_path.write_text('{"rows_sha256": "tampered"}', encoding="utf-8")
    with pytest.raises(DatasetError) as err:
        upload_pending(
            DatasetDeps(store, sink, lambda: NOW), local, ACCT, "lead-contacts"
        )
    assert err.value.code == "snapshot_mismatch"
    assert fake.puts == []


def test_upload_also_writes_a_version_still_pending() -> None:
    pytest.importorskip("pyarrow")
    store = _dataset_store()
    adopt_run(DatasetDeps(store, None, lambda: NOW), ACCT, "lead-contacts", R1)
    sink, fake = _sink()
    local = LocalFolderSink(Path("/nonexistent-never-read"))
    done = upload_pending(
        DatasetDeps(store, sink, lambda: NOW), local, ACCT, "lead-contacts"
    )
    assert done == [1]
    assert snapshot_key(ACCT, "lead-contacts", 1) in fake.objects


# -- the per-customer byte counter (measure only) ------------------------------------


def test_storage_usage_counts_rows_and_snapshot_bytes_per_customer() -> None:
    pytest.importorskip("pyarrow")
    store = _dataset_store()
    sink, fake = _sink()
    empty = storage_usage(store, sink, OTHER)
    assert (empty.rows_bytes, empty.snapshot_bytes, empty.total_bytes) == (0, 0, 0)

    adopt_run(DatasetDeps(store, sink, lambda: NOW), ACCT, "lead-contacts", R1)
    # Another customer's object never counts toward this one.
    fake.objects[f"{customer_prefix(OTHER)}x/v=1.parquet"] = (b"z" * 5000, {})
    usage = storage_usage(store, sink, ACCT)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    assert usage.datasets == 1 and usage.rows == 2
    assert usage.snapshot_bytes == len(fake.objects[key][0])
    assert usage.rows_bytes > 0
    assert usage.total_bytes == usage.rows_bytes + usage.snapshot_bytes
    assert usage.free_plan_bytes == FREE_PLAN_DATASET_BYTES == 500_000_000
    assert customer_prefix(ACCT) == db_name_for(ACCT, prefix="") + "/"
    no_sink = storage_usage(store, None, ACCT)
    assert no_sink.snapshot_bytes == 0 and no_sink.rows_bytes == usage.rows_bytes


def test_local_sink_counts_bytes_too(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    store = _dataset_store()
    local = LocalFolderSink(tmp_path)
    adopt_run(DatasetDeps(store, local, lambda: NOW), ACCT, "lead-contacts", R1)
    key = snapshot_key(ACCT, "lead-contacts", 1)
    assert local.bytes_under(customer_prefix(ACCT)) == len(local.get(key))


# -- config ----------------------------------------------------------------------------


def test_config_reads_the_bucket_and_key_together() -> None:
    config = load_config(
        {"GECKO_DATASETS_BUCKET": BUCKET, "GECKO_DATASETS_KMS_KEY": KMS}
    )
    assert (config.datasets_bucket, config.datasets_kms_key) == (BUCKET, KMS)
    assert load_config({}).datasets_bucket is None
    alias = load_config(
        {"GECKO_DATASETS_BUCKET": BUCKET, "GECKO_DATASETS_KMS_KEY": "alias/gecko-ds"}
    )
    assert alias.datasets_kms_key == "alias/gecko-ds"


def test_config_reads_the_ssm_unset_sentinel_as_unset() -> None:
    # The host provisions both parameters as `__unset__` (push-ssm-params.sh) until a
    # bucket exists; that must mean "no S3 sink", not a malformed bucket at boot.
    config = load_config(
        {"GECKO_DATASETS_BUCKET": "__unset__", "GECKO_DATASETS_KMS_KEY": "__unset__"}
    )
    assert (config.datasets_bucket, config.datasets_kms_key) == (None, None)


@pytest.mark.parametrize(
    "env",
    [
        {"GECKO_DATASETS_BUCKET": BUCKET},
        {"GECKO_DATASETS_KMS_KEY": KMS},
        {"GECKO_DATASETS_BUCKET": "Not_A_Bucket", "GECKO_DATASETS_KMS_KEY": KMS},
        {"GECKO_DATASETS_BUCKET": BUCKET, "GECKO_DATASETS_KMS_KEY": "not a key"},
    ],
)
def test_config_refuses_half_or_malformed_settings_by_name(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env)
    assert "GECKO_DATASETS_" in str(err.value)
    for value in env.values():
        assert value not in str(err.value)


def test_the_put_parameters_validate_against_botocore_s3_model() -> None:
    """The fake cannot tell a misspelt parameter; botocore's Stubber validates every
    call against S3's own service model. Skipped without the ``s3`` extra."""
    boto3 = pytest.importorskip("boto3")
    stub_mod = pytest.importorskip("botocore.stub")
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - a stub, never sent
    )
    sink = S3Sink(bucket=BUCKET, kms_key_id=KMS, client=client)
    with stub_mod.Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"e"'},
            {
                "Bucket": BUCKET,
                "Key": "h/d/v=1.parquet",
                "Body": b"x",
                "IfNoneMatch": "*",
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": KMS,
                "BucketKeyEnabled": True,
                "ContentType": "application/vnd.apache.parquet",
                "Metadata": {"rows_sha256": "s"},
            },
        )
        stub.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stub.add_client_error(
            "head_object", service_error_code="404", http_status_code=404
        )
        assert sink.put_once("h/d/v=1.parquet", b"x", {"rows_sha256": "s"}).startswith(
            "s3://"
        )
        with pytest.raises(SnapshotExistsError):
            sink.put_once("h/d/v=1.parquet", b"x", {"rows_sha256": "s"})
        assert sink.head_meta("h/d/v=2.parquet") is None
        stub.assert_no_pending_responses()
