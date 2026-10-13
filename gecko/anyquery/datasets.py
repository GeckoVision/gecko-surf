"""Datasets: a customer's living table, built from the facts of the runs it adopted.

A dataset pins a template plan, holds a subject set, and keeps one version per adopted
run. Its table (``dataset_rows``, one row per subject) is never written by hand: it is
:func:`materialize` over the adopted runs' facts, a pure function, so any version can be
rebuilt from the runs alone.

Only a DELIVERED run of the dataset's own mode and plans may be adopted
(``history.is_delivered``): a credited or unpaid run's facts are withheld everywhere
else, and they never leak in here. ``Store.last_good`` is deliberately not used: it
ignores mode and delivery, and it is keyed by one plan_id.

Values are stored whole, as in facts. Masking is a view's job (``pii.masked``), never
the table's or the snapshot's. Nothing here spends, plans, quotes or calls a source.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace

from gecko.orthogonal.supplier import parquet

from .bundles import BUNDLE_NAME_RE
from .config import AnyQueryConfig
from .history import is_delivered
from .pii import is_pii_field
from .snapshots import (
    SNAPSHOT_COLUMNS,
    SnapshotExistsError,
    SnapshotSink,
    snapshot_key,
    snapshot_rows,
)
from .store import Store
from .types import (
    CallMode,
    Dataset,
    DatasetCell,
    DatasetField,
    DatasetRow,
    DatasetVersion,
    FactRow,
    FieldState,
    QueryPlan,
    RunRecord,
)

__all__ = [
    "DatasetDeps",
    "DatasetError",
    "adopt_run",
    "adopted_runs_and_facts",
    "cell_counts",
    "dataset_row_id",
    "materialize",
    "new_dataset",
    "put_or_adopt",
    "rows_sha256",
    "snapshot_pending",
    "subjects_sha",
    "version_rows",
]

_GOOD: frozenset[FieldState] = frozenset({"fresh", "repaired"})
NOT_FETCHED = "not fetched yet"
#: Enough to read every row of any dataset: subjects are capped far below this.
_ALL_ROWS = 10_000


class DatasetError(Exception):
    """A refused dataset operation. ``code`` is stable; the message is our words."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def iso_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DatasetDeps:
    """What adopting a run needs, and nothing that pays: no billing, no runner."""

    store: Store
    #: ``None`` keeps every new version's snapshot ``pending``.
    sink: SnapshotSink | None = None
    clock: Callable[[], str] = iso_now


# -- ids and hashes ----------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dataset_row_id(dataset_id: str, subject: str) -> str:
    return _sha(dataset_id + "\n" + subject)[:24]


def subjects_sha(subjects: Sequence[str]) -> str:
    return _sha(_canonical(sorted(set(subjects))))


def rows_sha256(rows: Sequence[DatasetRow]) -> str:
    """The sha of what the table SAYS: subject and cells, sorted by subject. Row ids,
    the writing version and ``updated_at`` are bookkeeping and stay out, so a rebuild
    of the same runs hashes the same."""
    body = [
        {"subject": r.subject, "cells": [asdict(c) for c in r.cells]}
        for r in sorted(rows, key=lambda r: r.subject)
    ]
    return _sha(_canonical(body))


def _schema_sha(dataset: Dataset) -> str:
    return _sha(_canonical([asdict(f) for f in dataset.schema]))


def cell_counts(rows: Sequence[DatasetRow]) -> dict[FieldState, int]:
    counted: Counter[FieldState] = Counter(c.status for r in rows for c in r.cells)
    return dict(sorted(counted.items()))


# -- creation -----------------------------------------------------------------------------


def new_dataset(
    template: QueryPlan,
    *,
    dataset_id: str,
    bundle: str,
    mode: CallMode,
    now: str,
    max_subjects: int = AnyQueryConfig.max_subjects,
) -> Dataset:
    """A dataset with no version yet, its template pinned to ``template``."""
    if not BUNDLE_NAME_RE.match(dataset_id):
        raise DatasetError(
            "bad_slug",
            "a dataset name is 1-40 lowercase letters, digits or dashes, "
            "starting with a letter or digit",
        )
    subjects = tuple(dict.fromkeys(template.subjects))
    if len(subjects) > max_subjects:
        raise DatasetError(
            "too_many_subjects", f"a dataset holds at most {max_subjects} subjects"
        )
    schema = tuple(
        DatasetField(f.name, f.meaning, f.compare, is_pii_field(f.name))
        for f in template.fields
    )
    return Dataset(
        dataset_id=dataset_id,
        customer=template.customer,
        bundle=bundle,
        template_plan_id=template.plan_id,
        query=template.query,
        mode=mode,
        subjects=subjects,
        schema=schema,
        plan_ids=(template.plan_id,),
        versions=(),
        revision=0,
        created_at=now,
        updated_at=now,
    )


# -- materialize ----------------------------------------------------------------------------


def _no_data(name: str, checked_at: str | None, note: str) -> DatasetCell:
    return DatasetCell(
        name, None, "", None, checked_at, "no data", None, False, note, None
    )


def _cell(name: str, history: Sequence[FactRow]) -> DatasetCell:
    """One cell from its facts, oldest run first (DATASET-DESIGN.md section 3)."""
    if not history:
        return _no_data(name, None, NOT_FETCHED)
    latest = history[-1]
    if latest.status in _GOOD:
        return DatasetCell(
            field=name,
            value=latest.value,
            source=latest.source,
            fetched_at=latest.fetched_at,
            checked_at=latest.fetched_at,
            status=latest.status,
            stale_since=None,
            failover=latest.failover,
            note=latest.note,
            run_id=latest.run_id,
        )
    good_at = max((i for i, f in enumerate(history) if f.status in _GOOD), default=None)
    if good_at is None:
        return _no_data(name, latest.fetched_at, latest.note)
    good = history[good_at]
    # A stale fact from execute is not good either: staleness is recomputed across
    # every adopted run here, which execute (one plan at a time) cannot do.
    return DatasetCell(
        field=name,
        value=good.value,
        source=good.source,
        fetched_at=good.fetched_at,
        checked_at=latest.fetched_at,
        status="stale",
        stale_since=history[good_at + 1].fetched_at,
        failover=False,
        note=latest.note,
        run_id=good.run_id,
    )


def materialize(
    dataset: Dataset,
    runs: Sequence[RunRecord],
    facts: Sequence[FactRow],
    *,
    now: str,
) -> tuple[DatasetRow, ...]:
    """The table after ``runs``: one row per subject, one cell per schema field.

    Pure: no store, no clock, no I/O. A fact counts only when its run is in ``runs``,
    delivered and of the dataset's mode, and its subject and field are the dataset's."""
    usable = {
        r.run_id: r
        for r in runs
        if r.customer == dataset.customer and r.mode == dataset.mode and is_delivered(r)
    }
    subjects = set(dataset.subjects)
    names = {f.name for f in dataset.schema}
    kept = sorted(
        (
            f
            for f in facts
            if f.run_id in usable
            and f.customer == dataset.customer
            and f.mode == dataset.mode
            and f.subject in subjects
            and f.field in names
        ),
        # Run ids are YYYYMMDD-HHMMSS-hex in UTC: lexical order is time order.
        key=lambda f: f.run_id,
    )
    history: dict[tuple[str, str], list[FactRow]] = {}
    for f in kept:
        history.setdefault((f.subject, f.field), []).append(f)
    version = len(usable)
    return tuple(
        DatasetRow(
            row_id=dataset_row_id(dataset.dataset_id, subject),
            customer=dataset.customer,
            dataset_id=dataset.dataset_id,
            subject=subject,
            version=version,
            cells=tuple(
                _cell(f.name, history.get((subject, f.name), ()))
                for f in dataset.schema
            ),
            updated_at=now,
        )
        for subject in sorted(subjects)
    )


# -- adopt ------------------------------------------------------------------------------------


def _load(store: Store, account: str, dataset_id: str) -> Dataset:
    dataset = store.dataset(account, dataset_id)
    if dataset is None:
        raise DatasetError("dataset_not_found", f"No dataset {dataset_id!r}.")
    return dataset


def _check_run(store: Store, dataset: Dataset, run: RunRecord) -> None:
    """The invariants of DATASET-DESIGN.md section 1, for one run to adopt."""
    if not is_delivered(run):
        raise DatasetError(
            "run_withheld", f"Run {run.run_id} was not delivered; it cannot be adopted."
        )
    if run.mode != dataset.mode:
        raise DatasetError(
            "wrong_mode",
            f"Run {run.run_id} is {run.mode}; this dataset holds {dataset.mode} runs.",
        )
    if run.plan_id not in dataset.plan_ids:
        raise DatasetError(
            "plan_not_in_dataset",
            f"Run {run.run_id} ran a plan this dataset never had.",
        )
    plan = store.plan(dataset.customer, run.plan_id)
    if plan is None:
        raise DatasetError("plan_not_found", f"Run {run.run_id}'s plan is not stored.")
    if {f.name for f in plan.fields} != {f.name for f in dataset.schema}:
        raise DatasetError(
            "schema_changed",
            f"Run {run.run_id}'s plan has other fields; a new schema is a new dataset.",
        )
    if not set(plan.subjects) <= set(dataset.subjects):
        raise DatasetError(
            "subjects_not_in_dataset",
            f"Run {run.run_id} asked about subjects this dataset does not hold.",
        )


def _runs_and_facts(
    store: Store, account: str, run_ids: Sequence[str]
) -> tuple[list[RunRecord], list[FactRow]]:
    runs: list[RunRecord] = []
    facts: list[FactRow] = []
    for run_id in run_ids:
        run = store.run(account, run_id)
        if run is None:
            raise DatasetError("run_not_found", f"No stored run {run_id!r}.")
        runs.append(run)
        facts.extend(store.facts(account, run_id=run_id))
    return runs, facts


def adopted_runs_and_facts(
    store: Store, dataset: Dataset, *, upto: int | None = None
) -> tuple[list[RunRecord], list[FactRow]]:
    """The runs of the first ``upto`` versions (default: all) and their facts."""
    versions = dataset.versions if upto is None else dataset.versions[:upto]
    return _runs_and_facts(store, dataset.customer, [v.run_id for v in versions])


def version_rows(store: Store, dataset: Dataset, n: int) -> tuple[DatasetRow, ...]:
    """Version ``n`` rebuilt from its runs: version k is ``materialize(runs[:k])``.
    Free to rebuild, so a diff between versions needs no snapshot read."""
    if not 1 <= n <= len(dataset.versions):
        raise DatasetError(
            "version_not_found", f"Versions are 1..{len(dataset.versions)}."
        )
    runs, facts = adopted_runs_and_facts(store, dataset, upto=n)
    return materialize(
        dataset, runs, facts, now=dataset.versions[n - 1].materialized_at
    )


def adopt_run(
    deps: DatasetDeps, account: str, dataset_id: str, run_id: str
) -> DatasetVersion:
    """Adopt a delivered run as the dataset's next version. Idempotent: a run already
    adopted returns its version and writes nothing (but a pending snapshot is retried).

    Order: check; materialize over every adopted run plus this one; upsert the rows and
    drop rows of subjects no longer held; append the version as ``pending`` and save
    under the optimistic lock; then write the snapshot and mark it ``written``. A crash
    after the save leaves a correct table and a pending snapshot."""
    store = deps.store
    dataset = _load(store, account, dataset_id)
    run = store.run(account, run_id)
    if run is None:
        raise DatasetError("run_not_found", f"No stored run {run_id!r}.")
    held = next((v for v in dataset.versions if v.run_id == run_id), None)
    if held is not None:
        if held.snapshot_status == "pending" and deps.sink is not None:
            dataset = _write_pending(deps, dataset, {})
        return dataset.versions[held.n - 1]
    _check_run(store, dataset, run)

    now = deps.clock()
    runs, facts = _runs_and_facts(
        store, account, [v.run_id for v in dataset.versions] + [run_id]
    )
    rows = materialize(dataset, runs, facts, now=now)
    store.write_dataset_rows(account, dataset_id, rows)
    held_subjects = {
        r.subject for r in store.dataset_rows(account, dataset_id, limit=_ALL_ROWS)
    }
    gone = sorted(held_subjects - set(dataset.subjects))
    if gone:
        store.remove_dataset_rows(account, dataset_id, gone)

    version = DatasetVersion(
        n=len(dataset.versions) + 1,
        run_id=run.run_id,
        plan_id=run.plan_id,
        subjects_sha=subjects_sha(dataset.subjects),
        rows=len(rows),
        counts=cell_counts(rows),
        rows_sha256=rows_sha256(rows),
        materialized_at=now,
        snapshot_uri=None,
        snapshot_status="pending",
    )
    saved = replace(
        dataset,
        versions=(*dataset.versions, version),
        revision=dataset.revision + 1,
        updated_at=now,
    )
    store.save_dataset(saved, expect_revision=dataset.revision)
    if deps.sink is not None:
        saved = _write_pending(deps, saved, {version.n: rows})
    return saved.versions[-1]


def snapshot_pending(
    deps: DatasetDeps, account: str, dataset_id: str
) -> DatasetVersion | None:
    """Write every pending snapshot; return the last version written, or ``None`` when
    nothing was pending (or there is no sink)."""
    dataset = _load(deps.store, account, dataset_id)
    pending = [v.n for v in dataset.versions if v.snapshot_status == "pending"]
    if not pending or deps.sink is None:
        return None
    return _write_pending(deps, dataset, {}).versions[pending[-1] - 1]


def _write_pending(
    deps: DatasetDeps, dataset: Dataset, known: dict[int, tuple[DatasetRow, ...]]
) -> Dataset:
    """Write the snapshot of each pending version, saving after each one. A version's
    rows are ``known`` (just materialized) or rebuilt from its runs, and a rebuild must
    hash to the version's ``rows_sha256`` before it is written."""
    sink = deps.sink
    if sink is None:
        return dataset
    for version in dataset.versions:
        if version.snapshot_status != "pending":
            continue
        rows = known.get(version.n)
        if rows is None:
            run_ids = [v.run_id for v in dataset.versions[: version.n]]
            runs, facts = _runs_and_facts(deps.store, dataset.customer, run_ids)
            rows = materialize(dataset, runs, facts, now=version.materialized_at)
            if rows_sha256(rows) != version.rows_sha256:
                raise DatasetError(
                    "snapshot_mismatch",
                    f"Version {version.n} no longer rebuilds to the rows it recorded "
                    "(the subject set changed since); its snapshot stays pending.",
                )
        uri = _put_snapshot(sink, dataset, version, rows)
        written = replace(version, snapshot_uri=uri, snapshot_status="written")
        versions = list(dataset.versions)
        versions[version.n - 1] = written
        saved = replace(
            dataset,
            versions=tuple(versions),
            revision=dataset.revision + 1,
            updated_at=deps.clock(),
        )
        deps.store.save_dataset(saved, expect_revision=dataset.revision)
        dataset = saved
    return dataset


def _put_snapshot(
    sink: SnapshotSink,
    dataset: Dataset,
    version: DatasetVersion,
    rows: Sequence[DatasetRow],
) -> str:
    key = snapshot_key(dataset.customer, dataset.dataset_id, version.n)
    metadata = {
        "dataset_id": dataset.dataset_id,
        "version": str(version.n),
        "rows_sha256": version.rows_sha256,
        "schema_sha256": _schema_sha(dataset),
        "materialized_at": version.materialized_at,
    }
    data = parquet.table_bytes(
        snapshot_rows(dataset, rows, version.n), SNAPSHOT_COLUMNS, metadata=metadata
    )
    return put_or_adopt(sink, key, data, metadata, version)


def put_or_adopt(
    sink: SnapshotSink,
    key: str,
    data: bytes,
    metadata: dict[str, str],
    version: DatasetVersion,
) -> str:
    """Write once; on a clash, adopt the object when its ``rows_sha256`` is the
    version's (a retry wrote the same rows), else refuse. Never overwrites."""
    try:
        return sink.put_once(key, data, metadata)
    except SnapshotExistsError:
        found = sink.head_meta(key) or {}
        if found.get("rows_sha256") == version.rows_sha256:
            # A retry already wrote these same rows: adopt the object, never overwrite.
            return sink.uri_for(key)
        raise DatasetError(
            "snapshot_clash",
            f"Another snapshot already holds version {version.n}; it was not "
            "overwritten and this version keeps its current snapshot state.",
        ) from None
