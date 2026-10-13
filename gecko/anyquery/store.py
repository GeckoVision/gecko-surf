"""Where any-query keeps plans, quotes, runs, facts, call rows and the payment ledger.

Every method takes the customer, and no method can read across customers: that is how
"never pooled" holds. Deleting a customer removes everything of theirs at once.
:class:`MemoryStore` enforces the same unique keys and errors as the Mongo store, so the
offline path refuses what production would refuse.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

from .types import (
    Bundle,
    Dataset,
    DatasetRow,
    FactRow,
    LedgerEntry,
    PriorValue,
    QueryPlan,
    Quote,
    RunRecord,
)

__all__ = [
    "DuplicateChargeError",
    "DuplicateDatasetError",
    "DuplicateFactError",
    "DuplicateRunError",
    "MemoryStore",
    "Store",
    "StoreError",
    "SETTLE_TO",
    "StaleDatasetError",
    "StoreUnavailableError",
    "UNIQUE_LEDGER_KINDS",
    "check_dataset_rows",
    "check_dataset_save",
    "check_settle",
    "fact_dict",
]

#: One entry of each of these kinds per run: the duplicate-charge guard, and the guard
#: that an undelivered run is credited once.
UNIQUE_LEDGER_KINDS = frozenset({"charge", "discount", "run_credit"})
#: A run may hold several ``settlement`` attempts (a failed one is retried), but only ONE
#: open (``pending``) attempt at a time: two settlers of one charge cannot both sign.
#: What a pending entry may move to. ``pending`` again only to record the signature of a
#: broadcast that is not yet visible.
SETTLE_TO = ("landed", "failed", "pending")
_GOOD = ("fresh", "repaired")


class StoreError(Exception):
    """A refused or failed store operation. Never carries a connection URI."""


class DuplicateRunError(StoreError):
    """A run already exists for this quote_id."""


class DuplicateChargeError(StoreError):
    """A charge or discount already exists for this run_id, or the signature is reused."""


class DuplicateFactError(StoreError):
    """A fact already exists for this (run_id, subject, field)."""


class DuplicateDatasetError(StoreError):
    """A dataset with this slug already exists for this customer."""


class StaleDatasetError(StoreError):
    """The dataset changed (or vanished) since it was read: the optimistic lock held."""


class StoreUnavailableError(StoreError):
    """The store could not answer. Fails closed; the message carries no URI."""


def same_question(plan: QueryPlan, query: str, subjects: Sequence[str]) -> bool:
    """The same question: the same sanitized text about the same subjects, in any order.
    With no subjects given, the planner read them from the text, so the text decides."""
    if plan.query != query:
        return False
    return not subjects or sorted(plan.subjects) == sorted(subjects)


class Store(Protocol):
    def save_plan(self, plan: QueryPlan) -> None: ...
    def plan(self, customer: str, plan_id: str) -> QueryPlan | None: ...
    def plan_for(
        self, customer: str, query: str, subjects: Sequence[str]
    ) -> QueryPlan | None: ...
    def save_bundle(self, bundle: Bundle) -> None: ...
    def bundle(self, customer: str, name: str) -> Bundle | None: ...
    def bundles(self, customer: str) -> list[Bundle]: ...
    def save_quote(self, quote: Quote) -> None: ...
    def quote(self, customer: str, quote_id: str) -> Quote | None: ...
    def start_run(self, run: RunRecord) -> None: ...
    def finish_run(self, run: RunRecord) -> None: ...
    def run(self, customer: str, run_id: str) -> RunRecord | None: ...
    def runs(
        self, customer: str, *, plan_id: str | None = None, limit: int = 20
    ) -> list[RunRecord]: ...
    def write_facts(self, customer: str, rows: Sequence[FactRow]) -> None: ...
    def facts(
        self,
        customer: str,
        *,
        run_id: str,
        subject: str | None = None,
        field: str | None = None,
    ) -> list[FactRow]: ...
    def plan_facts(self, customer: str, plan_id: str) -> list[dict[str, Any]]: ...
    def last_good(
        self, customer: str, plan_id: str
    ) -> dict[tuple[str, str], PriorValue]: ...
    def append_calls(
        self, customer: str, rows: Sequence[Mapping[str, Any]]
    ) -> None: ...
    def calls(
        self, customer: str, *, run_id: str | None = None
    ) -> list[dict[str, Any]]: ...
    def append_ledger(self, entry: LedgerEntry) -> None: ...
    def update_ledger(self, entry: LedgerEntry) -> None: ...
    def ledger(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]: ...
    def create_dataset(self, dataset: Dataset) -> None: ...
    def save_dataset(self, dataset: Dataset, *, expect_revision: int) -> None: ...
    def dataset(self, customer: str, dataset_id: str) -> Dataset | None: ...
    def datasets(self, customer: str) -> list[Dataset]: ...
    def write_dataset_rows(
        self, customer: str, dataset_id: str, rows: Sequence[DatasetRow]
    ) -> None: ...
    def remove_dataset_rows(
        self, customer: str, dataset_id: str, subjects: Sequence[str]
    ) -> None: ...
    def dataset_rows(
        self,
        customer: str,
        dataset_id: str,
        *,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[DatasetRow]: ...
    def delete_dataset(self, customer: str, dataset_id: str) -> None: ...
    def delete_customer(self, customer: str) -> None: ...


def check_dataset_save(dataset: Dataset, expect_revision: int) -> None:
    """A save moves the revision by exactly one, from the one the caller read."""
    if dataset.revision != expect_revision + 1:
        raise StaleDatasetError(
            f"dataset {dataset.dataset_id} must be saved at revision "
            f"{expect_revision + 1}, not {dataset.revision}"
        )


def check_dataset_rows(
    customer: str, dataset_id: str, rows: Sequence[DatasetRow]
) -> None:
    """Every row belongs to this customer's dataset, and a batch names a subject once."""
    seen: set[str] = set()
    for row in rows:
        if row.customer != customer:
            raise StoreError("a dataset row belongs to another customer")
        if row.dataset_id != dataset_id:
            raise StoreError("a dataset row belongs to another dataset")
        if row.subject in seen:
            raise StoreError("a batch of dataset rows names a subject twice")
        seen.add(row.subject)


def fact_dict(row: FactRow) -> dict[str, Any]:
    """A fact as the row shape ``supplier.history`` reads (``subject_key="subject"``)."""
    return asdict(row)


def check_settle(held_signature: str | None, entry: LedgerEntry) -> None:
    """The rules of a settle that need no store: where a pending entry may go, and that
    a signature, once recorded, is never swapped for another."""
    if entry.status not in SETTLE_TO:
        raise StoreError("a ledger entry only moves from pending to landed or failed")
    if entry.status == "pending" and entry.signature is None:
        raise StoreError("a pending entry is only updated to record its signature")
    if held_signature is not None and entry.signature != held_signature:
        raise StoreError("a recorded signature is never replaced")


def _require_customer(customer: str) -> str:
    if not isinstance(customer, str) or not customer.strip():
        raise StoreError("customer must be a non-empty account id")
    return customer


@dataclass
class _Tenant:
    plans: dict[str, QueryPlan] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    facts: dict[tuple[str, str, str], FactRow] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    ledger: dict[str, LedgerEntry] = field(default_factory=dict)
    bundles: dict[str, Bundle] = field(default_factory=dict)
    datasets: dict[str, Dataset] = field(default_factory=dict)
    dataset_rows: dict[tuple[str, str], DatasetRow] = field(default_factory=dict)


@dataclass
class MemoryStore:
    """The offline store: one dict per customer, the same unique keys as Mongo."""

    _tenants: dict[str, _Tenant] = field(default_factory=dict)

    def _tenant(self, customer: str) -> _Tenant:
        return self._tenants.setdefault(_require_customer(customer), _Tenant())

    def _peek(self, customer: str) -> _Tenant:
        # A read never creates a tenant, as a read never creates a Mongo database.
        return self._tenants.get(_require_customer(customer)) or _Tenant()

    def save_plan(self, plan: QueryPlan) -> None:
        self._tenant(plan.customer).plans[plan.plan_id] = plan

    def plan(self, customer: str, plan_id: str) -> QueryPlan | None:
        return self._peek(customer).plans.get(plan_id)

    def plan_for(
        self, customer: str, query: str, subjects: Sequence[str]
    ) -> QueryPlan | None:
        same = [
            plan
            for plan in self._peek(customer).plans.values()
            if same_question(plan, query, subjects)
        ]
        return max(same, key=lambda plan: plan.created_at, default=None)

    def save_bundle(self, bundle: Bundle) -> None:
        self._tenant(bundle.customer).bundles[bundle.name] = bundle

    def bundle(self, customer: str, name: str) -> Bundle | None:
        return self._peek(customer).bundles.get(name)

    def bundles(self, customer: str) -> list[Bundle]:
        return sorted(self._peek(customer).bundles.values(), key=lambda b: b.name)

    def save_quote(self, quote: Quote) -> None:
        self._tenant(quote.customer).quotes[quote.quote_id] = quote

    def quote(self, customer: str, quote_id: str) -> Quote | None:
        return self._peek(customer).quotes.get(quote_id)

    def start_run(self, run: RunRecord) -> None:
        tenant = self._tenant(run.customer)
        if run.run_id in tenant.runs:
            raise DuplicateRunError(f"run {run.run_id} already exists")
        if any(r.quote_id == run.quote_id for r in tenant.runs.values()):
            raise DuplicateRunError(f"a run already exists for quote {run.quote_id}")
        tenant.runs[run.run_id] = run

    def finish_run(self, run: RunRecord) -> None:
        tenant = self._tenant(run.customer)
        held = tenant.runs.get(run.run_id)
        if held is None:
            raise StoreError(f"run {run.run_id} was never started")
        if held.quote_id != run.quote_id or held.plan_id != run.plan_id:
            raise StoreError("a finished run cannot change its quote or plan")
        tenant.runs[run.run_id] = run

    def run(self, customer: str, run_id: str) -> RunRecord | None:
        return self._peek(customer).runs.get(run_id)

    def runs(
        self, customer: str, *, plan_id: str | None = None, limit: int = 20
    ) -> list[RunRecord]:
        mine = [
            r
            for r in self._peek(customer).runs.values()
            if plan_id is None or r.plan_id == plan_id
        ]
        return sorted(mine, key=lambda r: r.run_id, reverse=True)[: max(limit, 0)]

    def write_facts(self, customer: str, rows: Sequence[FactRow]) -> None:
        tenant = self._tenant(customer)
        batch: dict[tuple[str, str, str], FactRow] = {}
        for row in rows:
            if row.customer != customer:
                raise StoreError("a fact row belongs to another customer")
            key = (row.run_id, row.subject, row.field)
            if key in tenant.facts or key in batch:
                raise DuplicateFactError(
                    f"fact ({row.run_id}, {row.subject!r}, {row.field}) already exists"
                )
            batch[key] = row
        tenant.facts.update(batch)

    def facts(
        self,
        customer: str,
        *,
        run_id: str,
        subject: str | None = None,
        field: str | None = None,
    ) -> list[FactRow]:
        return [
            r
            for (rid, subj, name), r in sorted(self._peek(customer).facts.items())
            if rid == run_id
            and (subject is None or subj == subject)
            and (field is None or name == field)
        ]

    def plan_facts(self, customer: str, plan_id: str) -> list[dict[str, Any]]:
        return [
            fact_dict(r)
            for _key, r in sorted(self._peek(customer).facts.items())
            if r.plan_id == plan_id
        ]

    def last_good(
        self, customer: str, plan_id: str
    ) -> dict[tuple[str, str], PriorValue]:
        # A good status counts even when the value is None: the source still answered
        # (rows from before the 8 Oct ruling hold None for unlabelled sources), and the
        # repair walk needs to know it did.
        out: dict[tuple[str, str], PriorValue] = {}
        for (run_id, subject, name), row in sorted(self._peek(customer).facts.items()):
            if row.plan_id == plan_id and row.status in _GOOD:
                out[(subject, name)] = PriorValue(run_id, row.value, row.status)
        return out

    def append_calls(self, customer: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._tenant(customer).calls.extend(copy.deepcopy(dict(r)) for r in rows)

    def calls(
        self, customer: str, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(r)
            for r in self._peek(customer).calls
            if run_id is None or r.get("run_id") == run_id
        ]

    def append_ledger(self, entry: LedgerEntry) -> None:
        tenant = self._tenant(entry.customer)
        if entry.amount_usdc_micro < 0:
            raise StoreError("ledger amounts are never negative; kind gives direction")
        if entry.entry_id in tenant.ledger:
            raise StoreError(f"ledger entry {entry.entry_id} already exists")
        for held in tenant.ledger.values():
            if (
                entry.kind in UNIQUE_LEDGER_KINDS
                and held.kind == entry.kind
                and held.run_id == entry.run_id
            ):
                raise DuplicateChargeError(
                    f"a {entry.kind} already exists for run {entry.run_id}"
                )
            if entry.signature is not None and held.signature == entry.signature:
                raise DuplicateChargeError("this signature is already in the ledger")
            if (
                entry.kind == "settlement"
                and entry.status == "pending"
                and held.kind == "settlement"
                and held.status == "pending"
                and held.run_id == entry.run_id
            ):
                raise DuplicateChargeError(
                    f"a settlement is already open for run {entry.run_id}"
                )
        tenant.ledger[entry.entry_id] = entry

    def update_ledger(self, entry: LedgerEntry) -> None:
        tenant = self._tenant(entry.customer)
        held = tenant.ledger.get(entry.entry_id)
        if held is None:
            raise StoreError(f"ledger entry {entry.entry_id} does not exist")
        check_settle(held.signature, entry)
        if held.status != "pending":
            raise StoreError(
                "a ledger entry only moves from pending to landed or failed"
            )
        settled = replace(
            held,
            status=entry.status,
            signature=entry.signature,
            slot=entry.slot,
            sol_usdc_rate_micro=entry.sol_usdc_rate_micro,
            note=entry.note,
        )
        if settled.signature is not None and any(
            e.signature == settled.signature
            for e in tenant.ledger.values()
            if e.entry_id != held.entry_id
        ):
            raise DuplicateChargeError("this signature is already in the ledger")
        tenant.ledger[held.entry_id] = settled

    def ledger(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]:
        entries = sorted(
            self._peek(customer).ledger.values(),
            key=lambda e: (e.created_at, e.entry_id),
            reverse=True,
        )
        return entries[: max(limit, 0)]

    def create_dataset(self, dataset: Dataset) -> None:
        tenant = self._tenant(dataset.customer)
        if dataset.dataset_id in tenant.datasets:
            raise DuplicateDatasetError(f"dataset {dataset.dataset_id} already exists")
        tenant.datasets[dataset.dataset_id] = dataset

    def save_dataset(self, dataset: Dataset, *, expect_revision: int) -> None:
        check_dataset_save(dataset, expect_revision)
        tenant = self._tenant(dataset.customer)
        held = tenant.datasets.get(dataset.dataset_id)
        if held is None or held.revision != expect_revision:
            raise StaleDatasetError(
                f"dataset {dataset.dataset_id} is not at revision {expect_revision}"
            )
        tenant.datasets[dataset.dataset_id] = dataset

    def dataset(self, customer: str, dataset_id: str) -> Dataset | None:
        return self._peek(customer).datasets.get(dataset_id)

    def datasets(self, customer: str) -> list[Dataset]:
        held = self._peek(customer).datasets
        return [held[k] for k in sorted(held)]

    def write_dataset_rows(
        self, customer: str, dataset_id: str, rows: Sequence[DatasetRow]
    ) -> None:
        check_dataset_rows(customer, dataset_id, rows)
        if not rows:
            return
        tenant = self._tenant(customer)
        for row in rows:
            tenant.dataset_rows[(dataset_id, row.subject)] = row

    def remove_dataset_rows(
        self, customer: str, dataset_id: str, subjects: Sequence[str]
    ) -> None:
        tenant = self._peek(customer)
        for subject in subjects:
            tenant.dataset_rows.pop((dataset_id, subject), None)

    def dataset_rows(
        self,
        customer: str,
        dataset_id: str,
        *,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[DatasetRow]:
        rows = [
            row
            for (did, subj), row in sorted(self._peek(customer).dataset_rows.items())
            if did == dataset_id and (subject is None or subj == subject)
        ]
        return rows[: max(limit, 0)]

    def delete_dataset(self, customer: str, dataset_id: str) -> None:
        tenant = self._peek(customer)
        tenant.datasets.pop(dataset_id, None)
        for key in [k for k in tenant.dataset_rows if k[0] == dataset_id]:
            del tenant.dataset_rows[key]

    def delete_customer(self, customer: str) -> None:
        self._tenants.pop(_require_customer(customer), None)
