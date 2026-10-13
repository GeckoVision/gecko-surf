"""Parquet snapshots of a dataset version, and where they are kept.

A snapshot is storage, not a view: values are stored whole (``pii.py`` masks at the
view). It is long, one row per cell, so its schema stays fixed whatever fields a dataset
has. It is write-once: :meth:`SnapshotSink.put_once` never overwrites, and the caller
decides what a clash means by comparing ``rows_sha256`` (see ``datasets``).

The key is ``<customer_hash>/<dataset_id>/v=<n>.parquet``, where the hash is the Mongo
database's own (``db_name_for(customer, prefix="")``): one hash finds both copies on a
deletion request, and the account id never appears in a key or a file.

Two sinks, one protocol: :class:`LocalFolderSink` (offline tests, the first backfill) and
:class:`S3Sink` (conditional ``PutObject`` with ``IfNoneMatch="*"``, SSE-KMS on every put).
Moving a ``local://`` snapshot to S3 is never automatic: ``dataset_storage.upload_pending``
does it when asked.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from gecko.orthogonal.supplier.parquet import Columns

from .store_mongo import db_name_for
from .types import Dataset, DatasetRow

__all__ = [
    "LOCAL_SCHEME",
    "S3_SCHEME",
    "SNAPSHOT_COLUMNS",
    "LocalFolderSink",
    "S3Sink",
    "SnapshotError",
    "SnapshotExistsError",
    "SnapshotSink",
    "customer_prefix",
    "snapshot_key",
    "snapshot_prefix",
    "snapshot_rows",
]

#: The long format, one row per (subject, field). No customer column.
SNAPSHOT_COLUMNS: Columns = (
    ("dataset_id", "string"),
    ("version", "int64"),
    ("subject", "string"),
    ("field", "string"),
    ("value", "string"),
    ("pii", "bool"),
    ("source", "string"),
    ("status", "string"),
    ("fetched_at", "string"),
    ("checked_at", "string"),
    ("stale_since", "string"),
    ("failover", "bool"),
    ("note", "string"),
    ("value_run_id", "string"),
)
LOCAL_SCHEME = "local://"
S3_SCHEME = "s3://"
_META_SUFFIX = ".meta.json"


class SnapshotError(Exception):
    """A refused or failed snapshot operation."""


class SnapshotExistsError(SnapshotError):
    """An object already exists under this key; it was not overwritten."""


class SnapshotSink(Protocol):
    def put_once(self, key: str, data: bytes, metadata: dict[str, str]) -> str:
        """Write ``data`` under ``key`` unless something is there; return its URI.
        Raises :class:`SnapshotExistsError` when the key is taken."""
        ...

    def uri_for(self, key: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def head_meta(self, key: str) -> dict[str, str] | None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def any_under(self, prefix: str) -> bool: ...
    def bytes_under(self, prefix: str) -> int:
        """The snapshot bytes held under ``prefix`` (measured, for the storage quota)."""
        ...


def customer_prefix(customer: str) -> str:
    """Every snapshot of one customer lives under this; a customer delete removes it."""
    return f"{db_name_for(customer, prefix='')}/"


def snapshot_prefix(customer: str, dataset_id: str) -> str:
    """Every snapshot of one dataset lives under this; a delete removes it whole."""
    return f"{db_name_for(customer, prefix='')}/{dataset_id}/"


def snapshot_key(customer: str, dataset_id: str, n: int) -> str:
    return f"{snapshot_prefix(customer, dataset_id)}v={n}.parquet"


def snapshot_rows(
    dataset: Dataset, rows: Sequence[DatasetRow], n: int
) -> list[dict[str, Any]]:
    """The long rows of version ``n``: subjects sorted, cells in schema order."""
    pii = {f.name: f.pii for f in dataset.schema}
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.subject):
        for cell in row.cells:
            out.append(
                {
                    "dataset_id": dataset.dataset_id,
                    "version": n,
                    "subject": row.subject,
                    "field": cell.field,
                    "value": cell.value,
                    "pii": pii.get(cell.field, False),
                    "source": cell.source,
                    "status": cell.status,
                    "fetched_at": cell.fetched_at,
                    "checked_at": cell.checked_at,
                    "stale_since": cell.stale_since,
                    "failover": cell.failover,
                    "note": cell.note,
                    "value_run_id": cell.run_id,
                }
            )
    return out


def _safe_parts(key: str) -> tuple[str, ...]:
    path = PurePosixPath(key)
    # The raw split too: PurePosixPath folds "a//b" into "a/b", and S3 would not.
    raw = key.split("/")
    if (
        not key
        or path.is_absolute()
        or any(p in ("..", ".", "") for p in (*path.parts, *raw))
    ):
        raise SnapshotError("a snapshot key must be a relative path inside the sink")
    return path.parts


@dataclass(frozen=True)
class LocalFolderSink:
    """Snapshots as files under ``root``; metadata in a ``.meta.json`` beside each.

    The URI is ``local://<key>``, not a filesystem path: the database never holds a
    machine's home directory, and the sink resolves the key against its own root."""

    root: Path

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_safe_parts(key))

    def put_once(self, key: str, data: bytes, metadata: dict[str, str]) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = path.with_name(path.name + _META_SUFFIX)
        if meta.exists() and not path.exists():
            # A claim whose data never landed (a crash between the two writes) holds
            # nothing: clear it, so the retry can write.
            meta.unlink()
        # The metadata goes first and exclusively: it is the claim on the key. The data
        # is then renamed into place, so a reader never sees half a file.
        try:
            with open(meta, "x", encoding="utf-8") as handle:
                json.dump(metadata, handle, sort_keys=True)
        except FileExistsError:
            raise SnapshotExistsError(f"a snapshot already exists at {key}") from None
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".snap-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            meta.unlink(missing_ok=True)
            raise
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return self.uri_for(key)

    def uri_for(self, key: str) -> str:
        _safe_parts(key)
        return LOCAL_SCHEME + key

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise SnapshotError(f"no snapshot at {key}") from None

    def head_meta(self, key: str) -> dict[str, str] | None:
        path = self._path(key)
        meta = path.with_name(path.name + _META_SUFFIX)
        if not meta.exists():
            return None
        loaded = json.loads(meta.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in loaded.items()}

    def _under(self, prefix: str) -> list[Path]:
        base = self.root.joinpath(*_safe_parts(prefix.rstrip("/")))
        if not base.is_dir():
            return []
        return [p for p in base.rglob("*") if p.is_file()]

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        for path in self._under(prefix):
            if not path.name.endswith(_META_SUFFIX):
                removed += 1
            path.unlink()
        return removed

    def any_under(self, prefix: str) -> bool:
        return bool(self._under(prefix))

    def bytes_under(self, prefix: str) -> int:
        return sum(
            p.stat().st_size
            for p in self._under(prefix)
            if not p.name.endswith(_META_SUFFIX)
        )


#: S3 answers a conditional put whose key is taken with 412 PreconditionFailed.
_TAKEN = frozenset({"PreconditionFailed", "412"})
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
#: ``delete_objects`` takes at most this many keys per call.
_DELETE_BATCH = 1000


def _error_code(exc: BaseException) -> tuple[str, int]:
    """botocore's ClientError carries ``.response``; read it without importing botocore."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", 0
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code, int(status) if isinstance(status, int) else 0


def _boto3_s3_client() -> Any:
    try:
        import boto3  # type: ignore[import-not-found,import-untyped,unused-ignore]  # optional: the `s3` extra
    except ImportError:
        raise SnapshotError(
            "boto3 is not installed: install the `s3` extra to write snapshots to S3"
        ) from None
    return boto3.client("s3")


class S3Sink:
    """Snapshots in one S3 bucket, each put write-once and encrypted with one KMS key.

    The URI is ``s3://<bucket>/<key>``. ``put_once`` sends ``IfNoneMatch="*"``, so S3
    itself refuses to overwrite (412), and always passes ``ServerSideEncryption=aws:kms``
    with ``SSEKMSKeyId``: there is no path that writes without the key. ``client`` is an
    injected S3 client (tests); unset, boto3 is imported on first use, never at import.
    An error names the operation and S3's code, never the KMS key id."""

    def __init__(self, *, bucket: str, kms_key_id: str, client: Any = None) -> None:
        if not bucket or not kms_key_id:
            raise SnapshotError("an S3 sink needs a bucket and a KMS key id")
        self.bucket = bucket
        self._kms_key_id = kms_key_id
        self._client = client

    def __repr__(self) -> str:
        return f"S3Sink(bucket={self.bucket!r})"

    def _s3(self) -> Any:
        if self._client is None:
            self._client = _boto3_s3_client()
        return self._client

    def _fail(self, what: str, exc: BaseException) -> SnapshotError:
        code, status = _error_code(exc)
        detail = code or type(exc).__name__
        return SnapshotError(
            f"S3 {what} failed ({detail}{f', {status}' if status else ''})"
        )

    def put_once(self, key: str, data: bytes, metadata: dict[str, str]) -> str:
        _safe_parts(key)
        try:
            self._s3().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self._kms_key_id,
                BucketKeyEnabled=True,
                ContentType="application/vnd.apache.parquet",
                Metadata=dict(metadata),
            )
        except SnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001 - boto3 raises its own ClientError
            code, status = _error_code(exc)
            if code in _TAKEN or status == 412:
                raise SnapshotExistsError(
                    f"a snapshot already exists at {key}"
                ) from None
            raise self._fail("put", exc) from None
        return self.uri_for(key)

    def uri_for(self, key: str) -> str:
        _safe_parts(key)
        return f"{S3_SCHEME}{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        _safe_parts(key)
        try:
            body = self._s3().get_object(Bucket=self.bucket, Key=key)["Body"]
            return cast(bytes, body.read())
        except SnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001
            code, status = _error_code(exc)
            if code in _MISSING or status == 404:
                raise SnapshotError(f"no snapshot at {key}") from None
            raise self._fail("get", exc) from None

    def head_meta(self, key: str) -> dict[str, str] | None:
        _safe_parts(key)
        try:
            head = self._s3().head_object(Bucket=self.bucket, Key=key)
        except SnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001
            code, status = _error_code(exc)
            if code in _MISSING or status == 404:
                return None
            raise self._fail("head", exc) from None
        return {str(k): str(v) for k, v in (head.get("Metadata") or {}).items()}

    def _listed(self, prefix: str) -> list[tuple[str, int]]:
        """Every (key, size) under ``prefix``, following continuation tokens."""
        out: list[tuple[str, int]] = []
        token: str | None = None
        while True:
            args: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                args["ContinuationToken"] = token
            try:
                page = self._s3().list_objects_v2(**args)
            except SnapshotError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._fail("list", exc) from None
            out.extend(
                (str(o["Key"]), int(o.get("Size", 0))) for o in page.get("Contents", ())
            )
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return out

    @staticmethod
    def _dir(prefix: str) -> str:
        """A prefix always ends in ``/``: ``h/lead`` must never match ``h/leads/``. An
        empty prefix (the whole bucket) is refused."""
        _safe_parts(prefix.rstrip("/"))
        return prefix.rstrip("/") + "/"

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix`` in batches, then list again and expect
        it empty: a delete-on-request that leaves an object behind raises."""
        prefix = self._dir(prefix)
        keys = [key for key, _ in self._listed(prefix)]
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            try:
                answer = self._s3().delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except SnapshotError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._fail("delete", exc) from None
            if answer.get("Errors"):
                raise SnapshotError(
                    f"S3 delete left {len(answer['Errors'])} object(s) under {prefix}"
                )
        if self._listed(prefix):
            raise SnapshotError(f"S3 still holds objects under {prefix} after delete")
        return len(keys)

    def any_under(self, prefix: str) -> bool:
        prefix = self._dir(prefix)
        try:
            page = self._s3().list_objects_v2(
                Bucket=self.bucket, Prefix=prefix, MaxKeys=1
            )
        except SnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail("list", exc) from None
        return int(page.get("KeyCount", 0)) > 0

    def bytes_under(self, prefix: str) -> int:
        return sum(size for _, size in self._listed(self._dir(prefix)))
