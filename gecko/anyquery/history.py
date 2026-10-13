"""Stored runs of one plan, read back from the ``Store``: what changed, and a cell's
history. Free, no call. The diff is ``supplier.history.changes_since`` with the subject
column ``subject``; this module only chooses which rows it may see.

Only DELIVERED runs are read (:func:`is_delivered`): finished, not ``failed``, with a
payment in ``types.PAID``. A run that is unpaid, pending, credited (paid but nothing was
delivered, or its delivery failed after the facts were written) or still running is
withheld here exactly as the ``dataset`` tool withholds it, or a diff would hand over
facts the user did not pay for or was credited back. Values go out as ``value_untrusted_*``: sanitized and capped upstream text, data,
never instructions.
"""

from __future__ import annotations

from typing import Any

from gecko.orthogonal.supplier.history import RunNotFoundError
from gecko.orthogonal.supplier.history import changes_since as diff_rows

from .store import Store
from .types import PAID, PaymentStatus, RunRecord

__all__ = [
    "DELIVERED",
    "RunNotFoundError",
    "RunWithheldError",
    "cell_history",
    "changes_since",
    "is_delivered",
]

DELIVERED: frozenset[PaymentStatus] = PAID
#: How many runs of one plan a diff or a history looks back over.
MAX_RUNS = 500
SUBJECT_KEY = "subject"
_SAFE = (
    "run_id",
    "mode",
    "subject",
    "field",
    "source",
    "status",
    "fetched_at",
    "failover",
    "stale_since",
    "note",
)


class RunWithheldError(RunNotFoundError):
    code = "run_withheld"


def is_delivered(run: RunRecord) -> bool:
    """May this run's rows go out? Paid, and finished with something to show. A
    ``running`` run may be mid-write or interrupted; a ``failed`` one delivered nothing
    (an empty run is ``failed`` too, and is credited)."""
    return run.payment_status in PAID and run.status not in ("running", "failed")


def _delivered_run(store: Store, customer: str, run_id: str) -> RunRecord:
    run = store.run(customer, run_id)
    if run is None:
        raise RunNotFoundError(f"No stored run {run_id!r}. Ask for the dataset.")
    if not is_delivered(run):
        raise RunWithheldError(f"Run {run_id} is not paid, so its facts are withheld.")
    return run


def _delivered_rows(store: Store, customer: str, plan_id: str) -> list[dict[str, Any]]:
    paid = {
        r.run_id
        for r in store.runs(customer, plan_id=plan_id, limit=MAX_RUNS)
        if is_delivered(r)
    }
    return [r for r in store.plan_facts(customer, plan_id) if r.get("run_id") in paid]


def _value(row: dict[str, Any] | None) -> str | None:
    value = row.get("value") if row else None
    return value if isinstance(value, str) else None


def changes_since(
    store: Store, customer: str, run_id: str, *, to_run: str | None = None
) -> dict[str, Any]:
    """``run_id`` against ``to_run`` (default: the latest later delivered run of the
    same plan and mode), per (subject, field): added, changed, removed."""
    run = _delivered_run(store, customer, run_id)
    if to_run is not None:
        other = _delivered_run(store, customer, to_run)
        if other.plan_id != run.plan_id:
            raise RunNotFoundError(f"Run {to_run} belongs to another plan.")
    rows = _delivered_rows(store, customer, run.plan_id)
    found = diff_rows(
        rows, run_id, to_run=to_run, subject_key=SUBJECT_KEY, latest_hint="run_rows"
    )
    out = found.to_dict()
    out["plan_id"] = run.plan_id
    cells = {(r["run_id"], r[SUBJECT_KEY], r["field"]): r for r in rows}
    for change in out["changes"]:
        key = (change[SUBJECT_KEY], change["field"])
        before = cells.get((found.since_run, *key))
        after = cells.get((found.against_run, *key)) if found.against_run else None
        del change["summary_before"], change["summary_after"]
        change["value_untrusted_before"] = _value(before)
        change["value_untrusted_after"] = _value(after)
    return out


def cell_history(
    store: Store, customer: str, plan_id: str, subject: str, field_name: str
) -> list[dict[str, Any]]:
    """Every delivered fact of one (subject, field), oldest run first."""
    rows = sorted(
        (
            r
            for r in _delivered_rows(store, customer, plan_id)
            if r.get(SUBJECT_KEY) == subject and r.get("field") == field_name
        ),
        key=lambda r: str(r.get("run_id")),
    )
    return [
        {**{k: r.get(k) for k in _SAFE}, "value_untrusted": _value(r)} for r in rows
    ]
