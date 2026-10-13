"""Backfill a dataset from a bundle and one run already paid for. No spend, ever.

It reads the bundle, its plan and the run; builds the dataset if it does not exist; and
adopts the run (``datasets.adopt_run``). It never plans, quotes, runs or pays, and it
imports nothing that could: no billing, no runner, no planner.

A dry run (the default) performs the same adoption on an in-memory COPY of exactly the
documents it read, so the plan it prints is the code path ``--write`` takes, and the
live store and the snapshot folder are never touched. The report holds counts only:
never a value, a subject or the account id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .datasets import (
    DatasetDeps,
    DatasetError,
    adopt_run,
    iso_now,
    new_dataset,
    rows_sha256,
)
from .snapshots import SnapshotSink, snapshot_key
from .store import DuplicateDatasetError, MemoryStore, Store
from .store_mongo import db_name_for
from .types import Dataset, RunRecord

__all__ = ["BackfillError", "BackfillReport", "backfill", "render"]

_MANY = 100_000


class BackfillError(Exception):
    """The backfill cannot start: a missing bundle, plan or run. Nothing was written."""


@dataclass(frozen=True)
class BackfillReport:
    write: bool
    customer_hash: str
    dataset_id: str
    dataset_existed: bool
    already_adopted: bool
    run_mode: str
    run_status: str
    run_payment: str
    version: int
    subjects: int
    fields: int
    rows: int
    cells: int
    counts: dict[str, int]
    rows_removed: int
    revision_before: int | None
    revision_after: int
    snapshot_key: str
    snapshot_status: str
    payments_before: int
    calls_before: int
    payments_after: int
    calls_after: int
    #: --write only: the rows read back hash to the version's rows_sha256.
    rows_sha_matches: bool | None


def _counts(store: Store, account: str) -> tuple[int, int]:
    return len(store.ledger(account, limit=_MANY)), len(store.calls(account))


def _dataset_to_create(
    store: Store,
    account: str,
    bundle_name: str,
    dataset_id: str,
    run: RunRecord,
    now: str,
) -> Dataset:
    bundle = store.bundle(account, bundle_name)
    if bundle is None:
        raise BackfillError(f"no bundle named {bundle_name!r} for this account")
    template = store.plan(account, bundle.plan_id)
    if template is None:
        raise BackfillError(
            f"bundle {bundle_name!r} points at a plan that is not stored"
        )
    try:
        return new_dataset(
            template,
            dataset_id=dataset_id,
            bundle=bundle_name,
            mode=run.mode,
            now=now,
        )
    except DatasetError as exc:
        raise BackfillError(str(exc)) from None


def _shadow(
    store: Store, account: str, dataset: Dataset, existed: bool, run_id: str
) -> MemoryStore:
    """An in-memory copy of exactly what adopting ``run_id`` reads."""
    shadow = MemoryStore()
    run_ids = [v.run_id for v in dataset.versions] + [run_id]
    plan_ids = {dataset.template_plan_id, *dataset.plan_ids}
    for run_id_ in run_ids:
        run = store.run(account, run_id_)
        if run is None:
            continue
        plan_ids.add(run.plan_id)
        shadow.start_run(run)
        shadow.write_facts(account, store.facts(account, run_id=run_id_))
    for plan_id in sorted(plan_ids):
        plan = store.plan(account, plan_id)
        if plan is not None:
            shadow.save_plan(plan)
    shadow.create_dataset(dataset)
    if existed:
        rows = store.dataset_rows(account, dataset.dataset_id, limit=_MANY)
        shadow.write_dataset_rows(account, dataset.dataset_id, rows)
    return shadow


def backfill(
    store: Store,
    sink: SnapshotSink | None,
    *,
    account: str,
    bundle: str,
    dataset_id: str,
    run_id: str,
    write: bool,
    clock: Callable[[], str] = iso_now,
) -> BackfillReport:
    run = store.run(account, run_id)
    if run is None:
        raise BackfillError(f"no stored run {run_id!r} for this account")
    held = store.dataset(account, dataset_id)
    existed = held is not None
    dataset = (
        held
        if held is not None
        else _dataset_to_create(store, account, bundle, dataset_id, run, clock())
    )
    already = any(v.run_id == run_id for v in dataset.versions)
    rows_before = (
        {r.subject for r in store.dataset_rows(account, dataset_id, limit=_MANY)}
        if existed
        else set()
    )
    payments_before, calls_before = _counts(store, account)

    target: Store
    if write:
        target = store
        if not existed:
            try:
                store.create_dataset(dataset)
            except DuplicateDatasetError:
                pass  # created by a concurrent backfill: adopt_run reloads it
        deps = DatasetDeps(store=store, sink=sink, clock=clock)
    else:
        target = _shadow(store, account, dataset, existed, run_id)
        deps = DatasetDeps(store=target, sink=None, clock=clock)

    try:
        version = adopt_run(deps, account, dataset_id, run_id)
    except DatasetError as exc:
        raise BackfillError(f"{exc.code}: {exc}") from None

    after = target.dataset(account, dataset_id)
    assert after is not None
    rows = target.dataset_rows(account, dataset_id, limit=_MANY)
    payments_after, calls_after = _counts(store, account)
    return BackfillReport(
        write=write,
        customer_hash=db_name_for(account, prefix=""),
        dataset_id=dataset_id,
        dataset_existed=existed,
        already_adopted=already,
        run_mode=run.mode,
        run_status=run.status,
        run_payment=run.payment_status,
        version=version.n,
        subjects=len(after.subjects),
        fields=len(after.schema),
        rows=len(rows),
        cells=sum(len(r.cells) for r in rows),
        counts={str(k): v for k, v in version.counts.items()},
        rows_removed=len(rows_before - {r.subject for r in rows}),
        revision_before=None if held is None else held.revision,
        revision_after=after.revision,
        snapshot_key=snapshot_key(account, dataset_id, version.n),
        snapshot_status=version.snapshot_status,
        payments_before=payments_before,
        calls_before=calls_before,
        payments_after=payments_after,
        calls_after=calls_after,
        rows_sha_matches=(rows_sha256(rows) == version.rows_sha256) if write else None,
    )


def render(report: BackfillReport, *, sink_label: str) -> list[str]:
    """The report as lines: counts, statuses and the snapshot key. No values."""
    head = "WRITE" if report.write else "DRY RUN (nothing written; pass --write)"
    counts = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items()))
    if report.already_adopted:
        action = "already adopted: nothing to write"
    elif report.dataset_existed:
        action = "adopt into the existing dataset"
    else:
        action = "create the dataset, then adopt"
    snapshot = (
        report.snapshot_status
        if report.write
        else ("would be written" if sink_label != "none" else "would stay pending")
    )
    lines = [
        f"== dataset backfill: {head}",
        f"customer db hash: {report.customer_hash}",
        f"dataset: {report.dataset_id} ({action})",
        f"run: mode={report.run_mode} status={report.run_status} "
        f"payment={report.run_payment}",
        f"version: {report.version}",
        f"subjects: {report.subjects}  fields: {report.fields}",
        f"rows to write: {0 if report.already_adopted else report.rows}  "
        f"cells: {0 if report.already_adopted else report.cells}",
        f"table after: {report.rows} rows, {report.cells} cells",
        f"cell status counts: {counts}",
        f"rows removed: {report.rows_removed}",
        f"revision: {report.revision_before} -> {report.revision_after}",
        f"snapshot: {sink_label}/{report.snapshot_key} ({snapshot})",
        f"payments: {report.payments_before} -> {report.payments_after}  "
        f"calls: {report.calls_before} -> {report.calls_after}",
    ]
    if report.rows_sha_matches is not None:
        lines.append(f"rows read back match rows_sha256: {report.rows_sha_matches}")
    return lines
