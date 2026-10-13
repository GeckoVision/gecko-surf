"""The backfill of ``lead-contacts`` v1: dry-run by default, counts only, no spend.

A dry run writes nothing anywhere (the store, the snapshot folder); its output names
counts and never a value or a subject. ``--write`` adopts the run once; a second
``--write`` changes nothing. The script never imports billing or the runner."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.mongo import MongoStore
from tests.anyquery_dataset_fixtures import ACCT, R1, SUBJECTS, seed
from tests.anyquery_fake_mongo import FakeMongoClient

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dataset_backfill.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("dataset_backfill_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(client: FakeMongoClient) -> Any:
    return (
        {
            db: {name: copy.deepcopy(c.docs) for name, c in d.collections.items()}
            for db, d in client.dbs.items()
        },
        set(client.created),
    )


def _seeded() -> tuple[FakeMongoClient, MongoStore]:
    client = FakeMongoClient()
    store = MongoStore(client)
    seed(store)
    return client, store


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--customer",
        ACCT,
        "--snapshot-root",
        str(tmp_path / "snap"),
        *extra,
    ]


def test_dry_run_writes_nothing_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, store = _seeded()
    before = _state(client)
    assert _script().main(_args(tmp_path), store=store) == 0
    assert _state(client) == before
    assert not (tmp_path / "snap").exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "rows to write: 2" in out and "cells: 8" in out
    assert "repaired=5" in out and "no data=3" in out
    assert "version: 1" in out
    for secret_ish in (*SUBJECTS, "+44", "analyst", "bletchley", ACCT):
        assert secret_ish not in out


def test_write_adopts_once_and_a_second_write_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, store = _seeded()
    payments_calls = (store.ledger(ACCT, limit=10_000), store.calls(ACCT))
    assert _script().main(_args(tmp_path, "--write"), store=store) == 0
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None and [v.run_id for v in ds.versions] == [R1]
    assert ds.versions[0].snapshot_status == "written"
    assert len(store.dataset_rows(ACCT, "lead-contacts")) == 2
    assert (store.ledger(ACCT, limit=10_000), store.calls(ACCT)) == payments_calls
    files = list((tmp_path / "snap").rglob("*.parquet"))
    assert [f.name for f in files] == ["v=1.parquet"]
    capsys.readouterr()

    before = _state(client)
    assert _script().main(_args(tmp_path, "--write"), store=store) == 0
    assert _state(client) == before
    again = store.dataset(ACCT, "lead-contacts")
    assert again == ds
    assert "already adopted" in capsys.readouterr().out


def test_a_missing_bundle_or_run_stops_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, store = _seeded()
    before = _state(client)
    code = _script().main(_args(tmp_path, "--write", "--bundle", "nope"), store=store)
    assert code == 1 and _state(client) == before
    code = _script().main(
        _args(tmp_path, "--write", "--run", "20990101-000000-ffffff"), store=store
    )
    assert code == 1 and _state(client) == before
    assert "STOP" in capsys.readouterr().out


def test_the_script_never_imports_anything_that_pays() -> None:
    probe = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('s', {str(SCRIPT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('gecko'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=SCRIPT.parents[1],
    )
    loaded = set(json.loads(done.stdout.strip().splitlines()[-1]))
    forbidden = {
        "gecko.anyquery.billing",
        "gecko.anyquery.solana_billing",
        "gecko.anyquery.payment_tx",
        "gecko.anyquery.settlement",
        "gecko.anyquery.settlement_tx",
        "gecko.anyquery.run",
        "gecko.anyquery.execute",
        "gecko.anyquery.service",
        "gecko.anyquery.upstream_x402",
        "gecko.anyquery.upstream_spend",
        "gecko.anyquery.planner",
        "gecko.anyquery.model_planner",
    }
    assert not (loaded & forbidden), sorted(loaded & forbidden)
    assert "gecko.anyquery.datasets" in loaded


def test_upload_pending_is_dry_by_default_then_uploads_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyarrow")
    from gecko.anyquery.snapshots import S3Sink, snapshot_key
    from tests.test_anyquery_dataset_s3 import BUCKET, KMS, FakeS3

    client, store = _seeded()
    assert _script().main(_args(tmp_path, "--write"), store=store) == 0
    capsys.readouterr()
    fake = FakeS3()
    remote = S3Sink(bucket=BUCKET, kms_key_id=KMS, client=fake)
    key = snapshot_key(ACCT, "lead-contacts", 1)

    before = _state(client)
    code = _script().main(
        _args(tmp_path, "--upload-pending"), store=store, remote=remote
    )
    assert code == 0 and fake.puts == [] and _state(client) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "v=1" in out and ACCT not in out

    code = _script().main(
        _args(tmp_path, "--upload-pending", "--write"), store=store, remote=remote
    )
    assert code == 0 and len(fake.puts) == 1 and key in fake.objects
    ds = store.dataset(ACCT, "lead-contacts")
    assert ds is not None and ds.versions[0].snapshot_uri == f"s3://{BUCKET}/{key}"
    out = capsys.readouterr().out
    assert "uploaded: 1" in out and ACCT not in out

    # Without the bucket settings and no injected sink, it stops by name.
    monkeypatch.delenv("GECKO_DATASETS_BUCKET", raising=False)
    monkeypatch.delenv("GECKO_DATASETS_KMS_KEY", raising=False)
    assert _script().main(_args(tmp_path, "--upload-pending"), store=store) == 1
    assert "GECKO_DATASETS_BUCKET" in capsys.readouterr().out
