"""Parquet in and out, with pyarrow imported only when asked for.

pyarrow is the optional ``datasets`` extra. Without it, every function here raises
``ParquetUnavailableError`` (a sentence with the install command); the job turns that
into a notice and keeps its CSV and JSONL, which are always written.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path
from typing import Any

from ..errors import ParquetUnavailableError

#: column -> pyarrow type name. Kept as data so the job never touches pyarrow itself.
Columns = tuple[tuple[str, str], ...]

TABLE_COLUMNS: Columns = (
    ("run_id", "string"),
    ("mode", "string"),
    ("generated_at", "string"),
    ("supplier", "string"),
    ("field", "string"),
    ("value_summary", "string"),
    ("source", "string"),
    ("second_source", "string"),
    ("agreement", "string"),
    ("model_summary", "string"),
    ("value", "string"),
    ("filled_by", "string"),
    ("failover", "bool"),
    ("note", "string"),
    ("status", "string"),
    ("stale_since", "string"),
)

LEDGER_COLUMNS: Columns = (
    ("ts", "string"),
    ("run_id", "string"),
    ("mode", "string"),
    ("catalog", "string"),
    ("api", "string"),
    ("path", "string"),
    ("page", "int64"),
    ("input_fields", "list<string>"),
    ("output_fields", "list<string>"),
    ("rows", "int64"),
    ("status", "string"),
    ("http_status", "int64"),
    ("quoted_cents", "int64"),
    ("charged_cents", "int64"),
    ("charge_assumed", "bool"),
    ("continuation", "bool"),
    # The x402 receipt of a call paid on chain; null on the key path.
    ("payment_signature", "string"),
    ("paid_usdc_micro", "int64"),
    # "call" rows: one per upstream page. "field" rows: one per (supplier, mapped
    # field) per run — which source filled it, whether a failover happened, and why.
    ("kind", "string"),
    ("subject", "string"),
    ("field", "string"),
    ("filled_by", "string"),
    ("failover", "bool"),
    ("drift", "string"),
    ("outcome", "string"),
)


def _modules() -> tuple[Any, Any]:
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
    except ImportError:
        raise ParquetUnavailableError() from None
    return pa, pq


def available() -> bool:
    try:
        _modules()
    except ParquetUnavailableError:
        return False
    return True


def _schema(pa: Any, columns: Columns) -> Any:
    kinds = {
        "string": pa.string(),
        "int64": pa.int64(),
        "bool": pa.bool_(),
        "list<string>": pa.list_(pa.string()),
    }
    return pa.schema([(name, kinds[kind]) for name, kind in columns])


def write_rows(path: Path, rows: list[dict[str, Any]], columns: Columns) -> Path:
    """Write ``rows`` (only the declared columns) atomically: tmp file, then rename."""
    pa, pq = _modules()
    names = [name for name, _kind in columns]
    clean = [{name: row.get(name) for name in names} for row in rows]
    table = pa.Table.from_pylist(clean, schema=_schema(pa, columns))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".parquet-", suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def table_bytes(
    rows: list[dict[str, Any]],
    columns: Columns,
    *,
    metadata: dict[str, str] | None = None,
) -> bytes:
    """``rows`` (only the declared columns) as one Parquet file in memory, with
    ``metadata`` as the file's key-value metadata. For a sink that is not a local path."""
    pa, pq = _modules()
    names = [name for name, _kind in columns]
    clean = [{name: row.get(name) for name in names} for row in rows]
    schema = _schema(pa, columns)
    if metadata:
        schema = schema.with_metadata(metadata)
    table = pa.Table.from_pylist(clean, schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    data: bytes = sink.getvalue().to_pybytes()
    return data


def read_rows(path: Path) -> list[dict[str, Any]]:
    _pa, pq = _modules()
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows
