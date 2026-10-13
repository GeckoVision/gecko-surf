"""Datasets: the cell rules of ``materialize``, and ``adopt_run`` on both stores.

Every rule of DATASET-DESIGN.md section 3 has a test here: one row per subject, the four
cell cases, the input filter (a credited, undelivered or recorded run never leaks in), an
idempotent adopt, and no read across customers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gecko.anyquery.datasets import (
    DatasetDeps,
    DatasetError,
    adopt_run,
    dataset_row_id,
    materialize,
    new_dataset,
    rows_sha256,
    snapshot_pending,
)
from gecko.anyquery.snapshots import LocalFolderSink
from gecko.anyquery.store import (
    DuplicateDatasetError,
    StaleDatasetError,
    Store,
    StoreError,
)
from gecko.anyquery.types import Dataset, DatasetCell, DatasetRow
from tests.anyquery_dataset_fixtures import (
    ACCT,
    FIELDS,
    NOW,
    OTHER,
    PLAN,
    R1,
    R2,
    STORES,
    SUBJECTS,
    fact,
    lead_facts,
    plan,
    run,
    seed,
)

A, B = SUBJECTS


def _dataset(**changes: object) -> Dataset:
    made = new_dataset(
        plan(), dataset_id="lead-contacts", bundle="lead-contacts", mode="live", now=NOW
    )
    return replace(made, **changes)  # type: ignore[arg-type]


def _cell(rows: tuple[DatasetRow, ...], subject: str, field: str) -> DatasetCell:
    row = next(r for r in rows if r.subject == subject)
    return next(c for c in row.cells if c.field == field)


# -- new_dataset ----------------------------------------------------------------------


def test_new_dataset_pins_the_template_and_marks_contact_fields() -> None:
    ds = _dataset()
    assert ds.template_plan_id == PLAN and ds.plan_ids == (PLAN,)
    assert [f.name for f in ds.schema] == list(FIELDS)
    assert {f.name for f in ds.schema if f.pii} == {"work_email", "mobile_phone"}
    assert ds.versions == () and ds.revision == 0 and ds.subjects == SUBJECTS


@pytest.mark.parametrize("slug", ["Lead", "-lead", "a" * 41, "lead contacts", ""])
def test_new_dataset_refuses_a_bad_slug(slug: str) -> None:
    with pytest.raises(DatasetError):
        new_dataset(plan(), dataset_id=slug, bundle="b", mode="live", now=NOW)


def test_new_dataset_refuses_more_than_max_subjects() -> None:
    many = [f"s{i}" for i in range(26)]
    with pytest.raises(DatasetError):
        new_dataset(
            plan(subjects=many), dataset_id="x", bundle="x", mode="live", now=NOW
        )


# -- materialize: the cell rule --------------------------------------------------------


def test_no_fact_gives_no_data_not_fetched_yet_and_one_row_per_subject() -> None:
    rows = materialize(_dataset(), [], [], now=NOW)
    assert [r.subject for r in rows] == sorted(SUBJECTS)
    for row in rows:
        assert [c.field for c in row.cells] == list(FIELDS)
        for cell in row.cells:
            assert cell == DatasetCell(
                cell.field, None, "", None, None, "no data", None, False,
                "not fetched yet", None,
            )  # fmt: skip
        assert row.row_id == dataset_row_id("lead-contacts", row.subject)
        assert row.customer == ACCT and row.updated_at == NOW


def test_latest_good_is_taken_whole() -> None:
    facts = [
        fact(R1, A, "job_title", "fresh", "old", note="n1"),
        fact(R2, A, "job_title", "repaired", "new", failover=True, note="n2"),
    ]
    rows = materialize(_dataset(), [run(R1), run(R2)], facts, now=NOW)
    cell = _cell(rows, A, "job_title")
    assert (cell.value, cell.status, cell.run_id, cell.note) == (
        "new",
        "repaired",
        R2,
        "n2",
    )
    assert cell.fetched_at == cell.checked_at == "2026-10-13T09:00:00Z"
    assert cell.failover is True and cell.stale_since is None
    assert cell.source.startswith("orthogonal:")


def test_latest_bad_after_good_is_stale_with_the_good_value() -> None:
    r3 = "20261014-090000-bbbbbb"
    facts = [
        fact(R1, A, "job_title", "fresh", "analyst", failover=True),
        fact(R2, A, "job_title", "no data", note="first miss"),
        fact(r3, A, "job_title", "no data", note="second miss"),
    ]
    rows = materialize(_dataset(), [run(R1), run(R2), run(r3)], facts, now=NOW)
    cell = _cell(rows, A, "job_title")
    assert cell.status == "stale" and cell.value == "analyst" and cell.run_id == R1
    assert cell.fetched_at == "2026-10-12T19:24:15Z"
    assert cell.checked_at == "2026-10-14T09:00:00Z"
    assert cell.stale_since == "2026-10-13T09:00:00Z"  # the FIRST miss after good
    assert cell.note == "second miss" and cell.failover is False


def test_latest_bad_with_no_good_is_no_data_with_its_note() -> None:
    rows = materialize(
        _dataset(), [run(R1)], [fact(R1, A, "work_email", "no data", note="x")], now=NOW
    )
    cell = _cell(rows, A, "work_email")
    assert (cell.value, cell.source, cell.fetched_at, cell.run_id) == (
        None,
        "",
        None,
        None,
    )
    assert cell.status == "no data" and cell.checked_at == "2026-10-12T19:24:15Z"
    assert cell.note == "x" and cell.stale_since is None


def test_a_good_fact_with_no_value_still_counts_as_good() -> None:
    facts = [
        fact(R1, A, "job_title", "fresh", None),
        fact(R2, A, "job_title", "no data"),
    ]
    rows = materialize(_dataset(), [run(R1), run(R2)], facts, now=NOW)
    cell = _cell(rows, A, "job_title")
    assert cell.status == "stale" and cell.value is None and cell.run_id == R1


def test_a_stale_fact_in_the_input_is_not_good() -> None:
    facts = [fact(R1, A, "job_title", "stale", "kept by execute")]
    rows = materialize(_dataset(), [run(R1)], facts, now=NOW)
    assert _cell(rows, A, "job_title").status == "no data"


def test_order_is_by_run_id_not_input_order() -> None:
    facts = [
        fact(R2, A, "job_title", "fresh", "newer"),
        fact(R1, A, "job_title", "fresh", "older"),
    ]
    rows = materialize(_dataset(), [run(R2), run(R1)], facts, now=NOW)
    assert _cell(rows, A, "job_title").value == "newer"


@pytest.mark.parametrize(
    "bad_run",
    [
        run(R2, payment="credited", status="failed"),
        run(R2, payment="failed", status="failed"),
        run(R2, payment="landed", status="running"),
        run(R2, mode="recorded"),
    ],
    ids=["credited", "unpaid", "running", "recorded"],
)
def test_facts_of_a_withheld_or_other_mode_run_never_leak(bad_run: object) -> None:
    facts = [
        fact(R1, A, "job_title", "fresh", "kept"),
        fact(R2, A, "job_title", "fresh", "LEAK"),
    ]
    rows = materialize(_dataset(), [run(R1), bad_run], facts, now=NOW)  # type: ignore[list-item]
    assert _cell(rows, A, "job_title").value == "kept"


def test_facts_outside_runs_subjects_or_schema_are_dropped() -> None:
    facts = [
        fact(R2, A, "job_title", "fresh", "not in runs"),
        fact(R1, "someone else", "job_title", "fresh", "not a subject"),
        fact(R1, A, "salary", "fresh", "not a field"),
        fact(R1, A, "job_title", "fresh", "x", customer=OTHER),
    ]
    rows = materialize(_dataset(), [run(R1)], facts, now=NOW)
    assert {r.subject for r in rows} == set(SUBJECTS)
    assert _cell(rows, A, "job_title").status == "no data"
    assert all(c.field != "salary" for r in rows for c in r.cells)


def test_rows_sha_ignores_bookkeeping_but_not_cells() -> None:
    rows = materialize(_dataset(), [run(R1)], lead_facts(), now=NOW)
    later = materialize(_dataset(), [run(R1)], lead_facts(), now="2027-01-01T00:00:00Z")
    assert rows_sha256(rows) == rows_sha256(tuple(reversed(later)))
    changed = materialize(_dataset(), [], [], now=NOW)
    assert rows_sha256(rows) != rows_sha256(changed)


# -- adopt_run, on both stores ----------------------------------------------------------


@pytest.fixture(params=sorted(STORES))
def store(request: pytest.FixtureRequest) -> Store:
    made: Store = STORES[request.param]()
    return made


def _deps(store: Store, tmp_path: Path | None = None) -> DatasetDeps:
    sink = None if tmp_path is None else LocalFolderSink(tmp_path)
    return DatasetDeps(store=store, sink=sink, clock=lambda: NOW)


def _created(store: Store) -> Dataset:
    seed(store)
    ds = _dataset()
    store.create_dataset(ds)
    return ds


def test_adopt_makes_v1_with_one_row_per_subject(store: Store, tmp_path: Path) -> None:
    _created(store)
    version = adopt_run(_deps(store, tmp_path), ACCT, "lead-contacts", R1)
    assert version.n == 1 and version.run_id == R1 and version.plan_id == PLAN
    assert version.rows == 2
    assert version.counts == {"repaired": 5, "no data": 3}
    rows = store.dataset_rows(ACCT, "lead-contacts")
    assert [r.subject for r in rows] == sorted(SUBJECTS)
    assert sum(len(r.cells) for r in rows) == 8
    assert all(
        _cell(tuple(rows), s, "work_email").status == "no data" for s in SUBJECTS
    )
    assert rows_sha256(tuple(rows)) == version.rows_sha256
    held = store.dataset(ACCT, "lead-contacts")
    assert held is not None and held.versions[-1].snapshot_status == "written"
    assert held.versions[-1] == version


def test_values_are_stored_whole_not_masked(store: Store) -> None:
    _created(store)
    adopt_run(_deps(store), ACCT, "lead-contacts", R1)
    row = store.dataset_rows(ACCT, "lead-contacts", subject=A)[0]
    assert _cell((row,), A, "mobile_phone").value == "+44 20 7946 0001"


def test_adopt_twice_writes_once(store: Store, tmp_path: Path) -> None:
    _created(store)
    deps = _deps(store, tmp_path)
    first = adopt_run(deps, ACCT, "lead-contacts", R1)
    held = store.dataset(ACCT, "lead-contacts")
    rows = store.dataset_rows(ACCT, "lead-contacts")
    later = replace(deps, clock=lambda: "2027-01-01T00:00:00Z")
    assert adopt_run(later, ACCT, "lead-contacts", R1) == first
    assert store.dataset(ACCT, "lead-contacts") == held
    assert store.dataset_rows(ACCT, "lead-contacts") == rows


def test_a_second_run_makes_v2_and_staleness_crosses_runs(store: Store) -> None:
    _created(store)
    store.start_run(run(R2))
    store.write_facts(
        ACCT,
        [fact(R2, A, "job_title", "no data", note="miss")]
        + [fact(R2, B, f, "fresh", "v") for f in FIELDS],
    )
    deps = _deps(store)
    adopt_run(deps, ACCT, "lead-contacts", R1)
    v2 = adopt_run(deps, ACCT, "lead-contacts", R2)
    assert v2.n == 2
    rows = tuple(store.dataset_rows(ACCT, "lead-contacts"))
    cell = _cell(rows, A, "job_title")
    assert cell.status == "stale" and cell.value == "analyst" and cell.run_id == R1
    assert all(r.version == 2 for r in rows)
    held = store.dataset(ACCT, "lead-contacts")
    assert held is not None and [v.n for v in held.versions] == [1, 2]


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (run(R2, payment="credited", status="failed"), "run_withheld"),
        (run(R2, payment="failed", status="failed"), "run_withheld"),
        (run(R2, mode="recorded", payment="not_charged"), "wrong_mode"),
        (run(R2, plan_id="plan-other"), "plan_not_in_dataset"),
    ],
    ids=["credited", "unpaid", "recorded", "foreign-plan"],
)
def test_adopt_refuses_a_run_it_may_not_hold(
    store: Store, bad: object, code: str
) -> None:
    _created(store)
    store.start_run(bad)  # type: ignore[arg-type]
    held = store.dataset(ACCT, "lead-contacts")
    with pytest.raises(DatasetError) as err:
        adopt_run(_deps(store), ACCT, "lead-contacts", R2)
    assert err.value.code == code
    assert store.dataset(ACCT, "lead-contacts") == held
    assert store.dataset_rows(ACCT, "lead-contacts") == []


def test_adopt_refuses_a_plan_with_other_fields_or_subjects(store: Store) -> None:
    _created(store)
    store.save_plan(plan(plan_id="plan-wide", fields=(*FIELDS, "salary")))
    store.save_plan(plan(plan_id="plan-new", subjects=(A, "grace hopper")))
    for plan_id, run_id, code in [
        ("plan-wide", R2, "schema_changed"),
        ("plan-new", "20261014-000000-cccccc", "subjects_not_in_dataset"),
    ]:
        held = store.dataset(ACCT, "lead-contacts")
        assert held is not None
        store.save_dataset(
            replace(
                held, plan_ids=(*held.plan_ids, plan_id), revision=held.revision + 1
            ),
            expect_revision=held.revision,
        )
        store.start_run(run(run_id, plan_id=plan_id))
        with pytest.raises(DatasetError) as err:
            adopt_run(_deps(store), ACCT, "lead-contacts", run_id)
        assert err.value.code == code


def test_adopt_refuses_unknown_dataset_and_unknown_run(store: Store) -> None:
    seed(store)
    with pytest.raises(DatasetError) as err:
        adopt_run(_deps(store), ACCT, "lead-contacts", R1)
    assert err.value.code == "dataset_not_found"
    store.create_dataset(_dataset())
    with pytest.raises(DatasetError) as err:
        adopt_run(_deps(store), ACCT, "lead-contacts", "20990101-000000-ffffff")
    assert err.value.code == "run_not_found"


def test_no_read_or_adopt_across_customers(store: Store) -> None:
    _created(store)
    adopt_run(_deps(store), ACCT, "lead-contacts", R1)
    # The other customer holds the same slug and the same run id, with other values.
    seed(store, OTHER)
    store.create_dataset(replace(_dataset(), customer=OTHER))
    assert store.dataset_rows(OTHER, "lead-contacts") == []
    adopt_run(_deps(store), OTHER, "lead-contacts", R1)
    mine = store.dataset_rows(ACCT, "lead-contacts")
    theirs = store.dataset_rows(OTHER, "lead-contacts")
    assert {r.customer for r in mine} == {ACCT}
    assert {r.customer for r in theirs} == {OTHER}
    assert [d.dataset_id for d in store.datasets(OTHER)] == ["lead-contacts"]
    store.delete_dataset(OTHER, "lead-contacts")
    assert store.dataset(OTHER, "lead-contacts") is None
    assert store.dataset_rows(OTHER, "lead-contacts") == []
    assert store.dataset(ACCT, "lead-contacts") is not None
    assert len(store.dataset_rows(ACCT, "lead-contacts")) == 2


def test_a_pending_snapshot_is_retried(store: Store, tmp_path: Path) -> None:
    _created(store)
    version = adopt_run(_deps(store), ACCT, "lead-contacts", R1)  # no sink
    assert version.snapshot_status == "pending" and version.snapshot_uri is None
    written = snapshot_pending(_deps(store, tmp_path), ACCT, "lead-contacts")
    assert written is not None and written.snapshot_status == "written"
    assert written.snapshot_uri == "local://" + (
        f"{_hash(ACCT)}/lead-contacts/v=1.parquet"
    )
    assert snapshot_pending(_deps(store, tmp_path), ACCT, "lead-contacts") is None


def _hash(customer: str) -> str:
    from gecko.anyquery.store_mongo import db_name_for

    return db_name_for(customer, prefix="")


# -- the store methods ------------------------------------------------------------------


def test_create_refuses_a_duplicate_slug(store: Store) -> None:
    store.create_dataset(_dataset())
    with pytest.raises(DuplicateDatasetError):
        store.create_dataset(_dataset())


def test_save_holds_the_optimistic_lock(store: Store) -> None:
    ds = _dataset()
    store.create_dataset(ds)
    store.save_dataset(replace(ds, revision=1, bundle="b1"), expect_revision=0)
    with pytest.raises(StaleDatasetError):
        store.save_dataset(replace(ds, revision=1, bundle="b2"), expect_revision=0)
    with pytest.raises(StaleDatasetError):
        store.save_dataset(replace(ds, revision=3, bundle="b3"), expect_revision=1)
    held = store.dataset(ACCT, "lead-contacts")
    assert held is not None and held.bundle == "b1" and held.revision == 1


def test_save_of_a_missing_dataset_is_stale(store: Store) -> None:
    with pytest.raises(StaleDatasetError):
        store.save_dataset(replace(_dataset(), revision=1), expect_revision=0)


def test_rows_refuse_another_customer_or_dataset(store: Store) -> None:
    rows = materialize(_dataset(), [], [], now=NOW)
    with pytest.raises(StoreError):
        store.write_dataset_rows(OTHER, "lead-contacts", rows)
    with pytest.raises(StoreError):
        store.write_dataset_rows(ACCT, "other-set", rows)


def test_rows_upsert_remove_and_filter(store: Store) -> None:
    rows = materialize(_dataset(), [], [], now=NOW)
    store.write_dataset_rows(ACCT, "lead-contacts", rows)
    store.write_dataset_rows(ACCT, "lead-contacts", rows)  # upsert: still two
    assert len(store.dataset_rows(ACCT, "lead-contacts")) == 2
    assert [r.subject for r in store.dataset_rows(ACCT, "lead-contacts", limit=1)] == [
        sorted(SUBJECTS)[0]
    ]
    store.remove_dataset_rows(ACCT, "lead-contacts", [A])
    assert [r.subject for r in store.dataset_rows(ACCT, "lead-contacts")] == [B]
    assert store.dataset_rows(ACCT, "lead-contacts", subject=A) == []


def test_datasets_list_sorted(store: Store) -> None:
    for slug in ("zeta", "alpha"):
        store.create_dataset(_dataset(dataset_id=slug))
    assert [d.dataset_id for d in store.datasets(ACCT)] == ["alpha", "zeta"]
    assert store.datasets(OTHER) == []


def test_a_removed_subject_loses_its_row_on_the_next_adopt(store: Store) -> None:
    _created(store)
    deps = _deps(store)
    adopt_run(deps, ACCT, "lead-contacts", R1)
    held = store.dataset(ACCT, "lead-contacts")
    assert held is not None
    store.save_dataset(
        replace(held, subjects=(A,), revision=held.revision + 1),
        expect_revision=held.revision,
    )
    store.save_plan(plan(plan_id="plan-a-only", subjects=(A,)))
    held = store.dataset(ACCT, "lead-contacts")
    assert held is not None
    store.save_dataset(
        replace(
            held, plan_ids=(*held.plan_ids, "plan-a-only"), revision=held.revision + 1
        ),
        expect_revision=held.revision,
    )
    store.start_run(run(R2, plan_id="plan-a-only"))
    store.write_facts(
        ACCT, [fact(R2, A, "job_title", "fresh", "x", plan_id="plan-a-only")]
    )
    adopt_run(deps, ACCT, "lead-contacts", R2)
    assert [r.subject for r in store.dataset_rows(ACCT, "lead-contacts")] == [A]
