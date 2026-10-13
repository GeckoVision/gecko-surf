"""Founder-run: make dataset ``lead-contacts`` from its bundle and adopt a paid run as v1.

    uv run --extra events --extra datasets python scripts/dataset_backfill.py \\
        --customer-hash 0123456789abcdef01234567                 # dry: counts only
    uv run ... scripts/dataset_backfill.py --customer-hash ... --write

Dry by default: the adoption runs on an in-memory copy of what it reads, and prints the
writes it would make, as counts. ``--write`` writes the dataset and its rows into the
customer's own database, and the v1 Parquet snapshot under ``--snapshot-root``
(default ``~/.gecko/datasets``). A second ``--write`` changes nothing.

No spend: no planner, no quote, no run, no payment, no upstream call. This script
imports nothing that could (a test holds that line). The MongoDB connection string
comes from the keychain slot ``MONGODB_URI`` and is never printed. The account is named
by ``--customer`` (or ``GECKO_ACCOUNT``), or found from its database hash among the
registry's enabled accounts with ``--customer-hash``; it is never printed either.

``--upload-pending`` does not backfill: it moves the dataset's ``local://`` snapshots
(read from ``--snapshot-root``) to S3, byte for byte, and writes any version still
pending. The bucket and KMS key come from ``GECKO_DATASETS_BUCKET`` and
``GECKO_DATASETS_KMS_KEY``; AWS credentials come from the usual boto3 chain (the
``s3`` extra). Dry by default too: it lists what would move; ``--write`` uploads.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from gecko.anyquery.config import ConfigError, load_config
from gecko.anyquery.dataset_backfill import BackfillError, backfill, render
from gecko.anyquery.dataset_storage import local_uploads, upload_pending
from gecko.anyquery.datasets import DatasetDeps, DatasetError
from gecko.anyquery.snapshots import (
    LocalFolderSink,
    S3Sink,
    SnapshotError,
    SnapshotSink,
    snapshot_key,
)
from gecko.anyquery.store import Store, StoreError
from gecko.anyquery.store_mongo import db_name_for

DEFAULT_BUNDLE = "lead-contacts"
DEFAULT_RUN = "20261012-192415-867e15"
DEFAULT_ROOT = Path.home() / ".gecko" / "datasets"


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--customer", default=os.environ.get("GECKO_ACCOUNT"))
    parser.add_argument("--customer-hash", default=None)
    parser.add_argument("--bundle", default=DEFAULT_BUNDLE)
    parser.add_argument("--dataset", default=None, help="default: the bundle name")
    parser.add_argument("--run", default=DEFAULT_RUN)
    parser.add_argument("--snapshot-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--upload-pending",
        action="store_true",
        help="move local:// and pending snapshots to S3 (no backfill)",
    )
    return parser.parse_args(argv)


def _account_for_hash(client: Any, wanted: str) -> str | None:
    """The enabled registry account whose database hash is ``wanted``. Read only."""
    from gecko.keyregistry import _MONGO_COLLECTION, _MONGO_DB, MongoKeyRegistry

    wanted = wanted.removeprefix("gq_")
    registry = MongoKeyRegistry(client[_MONGO_DB][_MONGO_COLLECTION])
    for account in registry.enabled_accounts():
        if db_name_for(account, prefix="") == wanted:
            return account
    return None


def _remote_from_env() -> SnapshotSink:
    config = load_config(os.environ)
    if config.datasets_bucket is None or config.datasets_kms_key is None:
        raise ConfigError(
            "set GECKO_DATASETS_BUCKET and GECKO_DATASETS_KMS_KEY to upload snapshots"
        )
    return S3Sink(bucket=config.datasets_bucket, kms_key_id=config.datasets_kms_key)


def _upload(
    store: Store,
    remote: SnapshotSink,
    root: Path,
    *,
    account: str,
    dataset_id: str,
    write: bool,
) -> list[str]:
    """The upload as lines: versions and keys only (the key holds the hash, never the
    account)."""
    dataset = store.dataset(account, dataset_id)
    if dataset is None:
        raise DatasetError("dataset_not_found", f"No dataset {dataset_id!r}.")
    pending = [v.n for v in dataset.versions if v.snapshot_status == "pending"]
    local = [n for n, _ in local_uploads(dataset)]
    head = "WRITE" if write else "DRY RUN (nothing uploaded; pass --write)"
    lines = [f"== snapshot upload: {head}", f"dataset: {dataset_id}"]
    for n in sorted(pending + local):
        origin = "pending (written from its runs)" if n in pending else "local://"
        lines.append(
            f"v={n}: {origin} -> {remote.uri_for(snapshot_key(account, dataset_id, n))}"
        )
    if not write:
        lines.append(f"would upload: {len(pending) + len(local)}")
        return lines
    deps = DatasetDeps(store=store, sink=remote)
    moved = upload_pending(deps, LocalFolderSink(root), account, dataset_id)
    lines.append(f"uploaded: {len(moved)}")
    return lines


def main(
    argv: list[str] | None = None,
    *,
    store: Store | None = None,
    remote: SnapshotSink | None = None,
) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        if store is None:
            from gecko.anyquery.mongo import mongo_store_from_keychain

            mongo = mongo_store_from_keychain()
            store = mongo
            if args.customer is None and args.customer_hash:
                args.customer = _account_for_hash(mongo.client, args.customer_hash)
        if not args.customer:
            print("STOP: name the account with --customer or a known --customer-hash")
            return 1
        root = Path(args.snapshot_root).expanduser()
        if args.upload_pending:
            lines = _upload(
                store,
                remote if remote is not None else _remote_from_env(),
                root,
                account=args.customer,
                dataset_id=args.dataset or args.bundle,
                write=args.write,
            )
            for line in lines:
                print(line)
            return 0
        report = backfill(
            store,
            LocalFolderSink(root) if args.write else None,
            account=args.customer,
            bundle=args.bundle,
            dataset_id=args.dataset or args.bundle,
            run_id=args.run,
            write=args.write,
        )
    except (BackfillError, StoreError, ConfigError, SnapshotError) as exc:
        print(f"STOP: {exc}")
        return 1
    except DatasetError as exc:
        print(f"STOP: {exc.code}: {exc}")
        return 1
    for line in render(report, sink_label=str(root)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
