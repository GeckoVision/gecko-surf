"""The owner's dataset tools on ``/anyquery/mcp``: create, refresh, list.

``create_dataset`` spends nothing: it pins a bundle's plan as the dataset's template and,
with ``from_run``, adopts a run the owner already paid for as version 1 (the backfill's
path, opened to the owner). ``refresh_dataset`` is ``run_query`` for a dataset: the same
quote, the same confirm, the same one charge, and a delivered run is adopted as the next
version. It never charges by itself: the run goes through the ``run_query`` it is handed
(:class:`OwnerDeps`), so there is one paid path, not two.

The plan a refresh runs comes from the dataset's PINNED template, never from the bundle
name: a bundle saved again under the same name may have other fields, and a dataset
that followed the name would change its schema under its readers.

A snapshot is storage written AFTER the version is saved. When it fails (a clash, an
older version that no longer rebuilds, the sink down), the version is still adopted with
a ``pending`` snapshot, and the answer says so: a paid run is never reported as not
adopted because a file could not be written (:func:`_adopt_run`).

This module imports nothing that pays; the service injects ``run_query``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .bundles import plan_from_bundle
from .datasets import DatasetDeps, DatasetError, adopt_run, new_dataset
from .dataset_read import dataset_summary, diff_rows
from .dataset_tools import endpoint_path
from .snapshots import SnapshotError, SnapshotSink, snapshot_prefix
from .store import DuplicateDatasetError, Store
from .types import CallMode, Dataset, DatasetVersion, QueryPlan

__all__ = ["OwnerDeps", "create_dataset", "datasets_list", "refresh_dataset"]

logger = logging.getLogger(__name__)

_ALL_ROWS = 10_000
#: The answer's words for a snapshot left pending; never the sink's own message.
SNAPSHOT_PENDING = (
    "The table is current, but its snapshot file was not written; it stays pending "
    "and is retried on the next refresh."
)


@dataclass(frozen=True)
class OwnerDeps:
    store: Store
    sink: SnapshotSink | None
    clock: Callable[[], str]
    #: The mode this host runs in; a new dataset without ``from_run`` takes it.
    mode: CallMode
    max_subjects: int
    #: The service's ``run_query`` for the owner's account: args in, answer out.
    run_query: Callable[[dict[str, Any]], dict[str, Any]]


def _dataset_deps(owner: OwnerDeps) -> DatasetDeps:
    return DatasetDeps(store=owner.store, sink=owner.sink, clock=owner.clock)


def _names(plan: QueryPlan) -> set[str]:
    return {f.name for f in plan.fields}


def _plan_for_subjects(
    owner: OwnerDeps,
    account: str,
    template: QueryPlan,
    subjects: Sequence[str],
    bundle: str,
) -> QueryPlan:
    """The template's fields for ``subjects``: the stored plan of this same question
    when it has the same fields, else a new one copied from the template. No model."""
    held = owner.store.plan_for(account, template.query, subjects)
    if held is not None and _names(held) == _names(template):
        return held
    plan = plan_from_bundle(template, subjects, name=bundle, now=owner.clock())
    owner.store.save_plan(plan)
    return plan


def _version_view(version: DatasetVersion) -> dict[str, Any]:
    return {
        "n": version.n,
        "materialized_at": version.materialized_at,
        "rows": version.rows,
        "counts": dict(version.counts),
        "snapshot_status": version.snapshot_status,
    }


def _adopt_run(
    owner: OwnerDeps, account: str, dataset_id: str, run_id: str
) -> tuple[DatasetVersion, dict[str, str] | None]:
    """``adopt_run``, and the snapshot problem if the version was saved without its
    file. Raises only when the run was NOT adopted."""
    try:
        return adopt_run(_dataset_deps(owner), account, dataset_id, run_id), None
    except (DatasetError, SnapshotError) as exc:
        held = owner.store.dataset(account, dataset_id)
        version = next(
            (v for v in (held.versions if held else ()) if v.run_id == run_id), None
        )
        if version is None:
            raise
        code = exc.code if isinstance(exc, DatasetError) else "snapshot_unavailable"
        logger.warning(
            "dataset %s v%d adopted, snapshot pending (%s)", dataset_id, version.n, code
        )
        return version, {"code": code, "note": SNAPSHOT_PENDING}


# -- create -------------------------------------------------------------------------------


def create_dataset(
    owner: OwnerDeps,
    account: str,
    *,
    bundle: str,
    dataset_id: str,
    subjects: Sequence[str] | None,
    from_run: str | None,
) -> dict[str, Any]:
    store = owner.store
    if not bundle:
        raise DatasetError("bad_arguments", "create_dataset needs a bundle name.")
    saved = store.bundle(account, bundle)
    template = None if saved is None else store.plan(account, saved.plan_id)
    if saved is None or template is None:
        raise DatasetError(
            "bundle_not_found",
            f"No bundle {bundle} for your account. Call bundles to list yours.",
        )
    run = None
    if from_run is not None:
        run = store.run(account, from_run)
        if run is None:
            raise DatasetError("run_not_found", f"No run {from_run} for your account.")

    pinned = template
    if subjects:
        pinned = _plan_for_subjects(owner, account, template, subjects, bundle)
    elif run is not None and run.plan_id != template.plan_id:
        ran = store.plan(account, run.plan_id)
        if (
            ran is None
            or ran.query != template.query
            or _names(ran) != _names(template)
        ):
            raise DatasetError(
                "run_not_of_bundle",
                f"Run {run.run_id} asked another question; adopt a run of bundle "
                f"{bundle}.",
            )
        pinned = ran
    dataset = new_dataset(
        pinned,
        dataset_id=dataset_id,
        bundle=bundle,
        mode=run.mode if run is not None else owner.mode,
        now=owner.clock(),
        max_subjects=owner.max_subjects,
    )
    if run is not None and run.plan_id not in dataset.plan_ids:
        # A run of the bundle for a subset of these subjects: its plan joins the
        # dataset's plans, and adopt_run checks its fields and subjects.
        dataset = replace(dataset, plan_ids=(*dataset.plan_ids, run.plan_id))
    if owner.sink is not None and owner.sink.any_under(
        snapshot_prefix(account, dataset_id)
    ):
        # A half-finished delete must never mix its snapshots into a new dataset.
        raise DatasetError(
            "dataset_exists",
            f"Snapshots of an earlier dataset {dataset_id} are still being removed; "
            "pick another name or try later.",
        )
    try:
        store.create_dataset(dataset)
    except DuplicateDatasetError:
        raise DatasetError(
            "dataset_exists",
            f"You already have a dataset {dataset_id}. Call datasets to list yours.",
        ) from None

    version = None
    snapshot = None
    if run is not None:
        try:
            version, snapshot = _adopt_run(owner, account, dataset_id, run.run_id)
        except (DatasetError, SnapshotError):
            # Nothing half-made stays behind: the dataset is created with its v1 or not.
            store.delete_dataset(account, dataset_id)
            raise
    held = store.dataset(account, dataset_id) or dataset
    out = {
        "dataset": dataset_summary(held),
        "status": "adopted" if version is not None else "no_version_yet",
        "version": None if version is None else _version_view(version),
        "spent": "nothing",
        "next": (
            f"Readers query it at {endpoint_path(dataset_id)}. "
            + (
                "Call refresh_dataset with this dataset_id to fill it; it quotes first "
                "and spends nothing until confirmed."
                if version is None
                else "Call refresh_dataset later for the next version."
            )
        ),
    }
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


# -- refresh ------------------------------------------------------------------------------


def _load(store: Store, account: str, dataset_id: str) -> Dataset:
    dataset = store.dataset(account, dataset_id)
    if dataset is None:
        raise DatasetError(
            "dataset_not_found",
            f"No dataset {dataset_id} for your account. Call datasets to list yours.",
        )
    return dataset


def refresh_dataset(
    owner: OwnerDeps,
    account: str,
    *,
    dataset_id: str,
    confirm: bool,
    quote_id: str | None,
    subjects: Sequence[str] | None,
    pay_with: str,
) -> dict[str, Any]:
    store = owner.store
    dataset = _load(store, account, dataset_id)
    wanted = tuple(subjects) if subjects else dataset.subjects
    if not set(wanted) <= set(dataset.subjects):
        raise DatasetError(
            "subjects_not_in_dataset",
            "A refresh runs some or all of the dataset's own subjects; call schema on "
            "its endpoint for the list.",
        )
    template = store.plan(account, dataset.template_plan_id)
    if template is None:
        raise DatasetError("plan_not_found", "The dataset's template is not stored.")
    plan = _plan_for_subjects(owner, account, template, wanted, dataset.bundle)
    if plan.plan_id not in dataset.plan_ids:
        grown = replace(
            dataset,
            plan_ids=(*dataset.plan_ids, plan.plan_id),
            revision=dataset.revision + 1,
            updated_at=owner.clock(),
        )
        store.save_dataset(grown, expect_revision=dataset.revision)
        dataset = grown

    args: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "confirm": confirm,
        "pay_with": pay_with,
    }
    if quote_id is not None:
        args["quote_id"] = quote_id
    answer = owner.run_query(args)
    if "error" in answer:
        return {**answer, "dataset_id": dataset_id}
    status = answer.get("status")
    if status == "needs_confirmation":
        return {
            "status": "needs_confirmation",
            "dataset_id": dataset_id,
            "why": answer.get("why"),
            "quote": answer.get("quote"),
            "spent": "nothing",
            "next": (
                "Show the user this price. If they agree, call refresh_dataset again "
                "with this dataset_id, the quote_id and confirm=true."
            ),
        }
    run = answer.get("run") or {}
    if status != "ran":
        out = {**answer, "dataset_id": dataset_id, "adopted": False}
        if status == "payment_pending":
            out["next"] = (
                "The payment is broadcast but not visible yet, so nothing ran. Call "
                "refresh_dataset again with the same dataset_id, quote_id and "
                "confirm=true: it re-checks that payment and never charges again."
            )
        return out
    return _adopt(owner, account, dataset_id, str(run.get("run_id")), answer)


def _adopt(
    owner: OwnerDeps,
    account: str,
    dataset_id: str,
    run_id: str,
    answer: dict[str, Any],
) -> dict[str, Any]:
    store = owner.store
    before = store.dataset_rows(account, dataset_id, limit=_ALL_ROWS)
    try:
        version, snapshot = _adopt_run(owner, account, dataset_id, run_id)
    except (DatasetError, SnapshotError) as exc:
        code = exc.code if isinstance(exc, DatasetError) else "snapshot_unavailable"
        # Paid and delivered, but not adoptable: the rows are still the owner's.
        return {
            "status": "not_adopted",
            "dataset_id": dataset_id,
            "code": code,
            "why": str(exc) if isinstance(exc, DatasetError) else SNAPSHOT_PENDING,
            "run": answer.get("run"),
            "payment": answer.get("payment"),
            "next": f"The run's rows are readable with run_rows run_id={run_id}.",
        }
    after = store.dataset_rows(account, dataset_id, limit=_ALL_ROWS)
    out = {
        "status": "refreshed",
        "dataset_id": dataset_id,
        "version": _version_view(version),
        "changes": len(diff_rows(before, after)),
        "run": answer.get("run"),
        "payment": answer.get("payment"),
        "repeated": answer.get("repeated", False),
        "next": (
            f"Read it at {endpoint_path(dataset_id)}: rows for the table, "
            f"changes_since with version {max(version.n - 1, 1)} for what moved."
        ),
    }
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def datasets_list(store: Store, account: str) -> dict[str, Any]:
    return {"datasets": [dataset_summary(d) for d in store.datasets(account)]}
