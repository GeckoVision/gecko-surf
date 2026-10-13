"""Shared builders for the dataset tests: a plan, its runs and their facts, seeded into
either store. Values here are fake; none is a real person's contact detail."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from gecko.anyquery.mongo import MongoStore
from gecko.anyquery.store import MemoryStore, Store
from gecko.anyquery.types import (
    Bundle,
    FactRow,
    FieldState,
    PaymentStatus,
    PlannedField,
    QueryPlan,
    RunRecord,
    RunStatus,
)
from gecko.modes import CallMode
from tests.anyquery_fake_mongo import FakeMongoClient

ACCT = "acct-a"
OTHER = "acct-b"
PLAN = "plan-a"
NOW = "2026-10-12T20:00:00Z"
FIELDS = ("work_email", "mobile_phone", "job_title", "current_company")
SUBJECTS = ("ada lovelace", "alan turing")
SOURCE = "orthogonal:fiber:POST:/people/lookup"


def plan(
    customer: str = ACCT,
    plan_id: str = PLAN,
    subjects: Sequence[str] = SUBJECTS,
    fields: Sequence[str] = FIELDS,
) -> QueryPlan:
    return QueryPlan(
        plan_id=plan_id,
        customer=customer,
        query="work email, mobile, title and company for these leads",
        subjects=tuple(subjects),
        fields=tuple(PlannedField(f, f"the {f}", "text", ()) for f in fields),
        unplanned=(),
        planner="fake",
        planner_model=None,
        catalog_sha="sha",
        created_at="2026-10-12T19:20:00Z",
    )


def run(
    run_id: str,
    *,
    customer: str = ACCT,
    plan_id: str = PLAN,
    mode: CallMode = "live",
    status: RunStatus = "partial",
    payment: PaymentStatus = "landed",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        customer=customer,
        plan_id=plan_id,
        quote_id="q-" + run_id,
        mode=mode,
        status=status,
        payment_status=payment,
        started_at="2026-10-12T19:24:15Z",
        finished_at="2026-10-12T19:25:00Z",
        upstream_quoted_cents=10,
        upstream_charged_cents=5,
        calls=3,
        skipped_calls=0,
        counts={},
        stop_code=None,
        notices=(),
    )


def fact(
    run_id: str,
    subject: str,
    field: str,
    status: FieldState,
    value: str | None = None,
    *,
    fetched_at: str | None = None,
    customer: str = ACCT,
    plan_id: str = PLAN,
    mode: CallMode = "live",
    failover: bool = False,
    note: str = "",
) -> FactRow:
    good = status in ("fresh", "repaired")
    return FactRow(
        customer=customer,
        plan_id=plan_id,
        run_id=run_id,
        mode=mode,
        subject=subject,
        field=field,
        value=value,
        source=SOURCE if good else "",
        fetched_at=fetched_at or _time_of(run_id),
        status=status,
        previous=None,
        failover=failover,
        note=note,
    )


def _time_of(run_id: str) -> str:
    # run ids are YYYYMMDD-HHMMSS-hex
    day, clock, _hex = run_id.split("-")
    return f"{day[:4]}-{day[4:6]}-{day[6:]}T{clock[:2]}:{clock[2:4]}:{clock[4:]}Z"


R1 = "20261012-192415-867e15"
R2 = "20261013-090000-aaaaaa"


def lead_facts(run_id: str = R1, customer: str = ACCT) -> list[FactRow]:
    """The shape of the live run: 5 repaired, 3 no data."""
    a, b = SUBJECTS
    return [
        fact(run_id, a, "work_email", "no data", customer=customer, note="no source"),
        fact(run_id, b, "work_email", "no data", customer=customer, note="no source"),
        fact(
            run_id, a, "mobile_phone", "repaired", "+44 20 7946 0001", customer=customer
        ),
        fact(run_id, b, "mobile_phone", "no data", customer=customer, note="empty"),
        fact(run_id, a, "job_title", "repaired", "analyst", customer=customer),
        fact(run_id, b, "job_title", "repaired", "mathematician", customer=customer),
        fact(run_id, a, "current_company", "repaired", "engine co", customer=customer),
        fact(run_id, b, "current_company", "repaired", "bletchley", customer=customer),
    ]


def seed(
    store: Store,
    customer: str = ACCT,
    runs: Sequence[RunRecord] | None = None,
    facts: Sequence[FactRow] | None = None,
) -> None:
    """A bundle ``lead-contacts`` on ``plan-a``, and run R1 with its eight facts."""
    store.save_plan(plan(customer))
    store.save_bundle(
        Bundle(customer, "lead-contacts", PLAN, plan(customer).query, NOW)
    )
    for r in runs if runs is not None else [run(R1, customer=customer)]:
        store.start_run(r)
    store.write_facts(
        customer, facts if facts is not None else lead_facts(customer=customer)
    )


def memory_store() -> MemoryStore:
    return MemoryStore()


def mongo_store() -> MongoStore:
    return MongoStore(FakeMongoClient())


STORES: dict[str, Callable[[], Store]] = {
    "memory": memory_store,
    "mongo": mongo_store,
}
