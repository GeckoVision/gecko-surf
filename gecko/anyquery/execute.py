"""Run a validated plan at its confirmed quote: the ``Execute`` contract of ``run.py``.

Every upstream call goes through the Orthogonal core: ``Runner`` -> ``fetch_pages`` ->
``SpendGuard`` -> ``OrthogonalApi``. Two things are set per run, from code only:

* the allowlist is the plan's own targets (``calls.plan_targets``), so the key can reach
  what the user confirmed and nothing else;
* the per-run cap is ``quote.upstream_max_cents``, the worst case the user was shown.

CHARGE BEFORE ANY PAID CALL. The caller claims the quote and captures the payment
first; ``execute`` refuses, before any call, a run the store does not hold as claimed,
``running`` and paid (``types.PAID``). That check is here as well as in the service so a
second caller of ``execute`` cannot spend upstream money nobody paid for.

What is stored, per customer, through the ``Store``: one ``FactRow`` per (subject,
field), the call rows (field NAMES and counts, never values), and the final record,
which keeps the claim's payment status. Every source's value is stored for the customer
who asked (founder ruling, 8 Oct); the source's ``storage`` label is metadata, not a gate.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from gecko.orthogonal.job import JobDeps
from gecko.orthogonal.spec import Target
from gecko.orthogonal.spend import SpendGuard
from gecko.orthogonal.supplier.drift import baseline_from
from gecko.orthogonal.supplier.fields import FIELD_STATES
from gecko.orthogonal.supplier.runner import Runner

from .calls import plan_targets
from .repair import CellState, Outcome, Prior, resolve_cell
from .run import RUN_ID_RE, RunDeps, RunRefusedError, RunResult
from .store import StoreError
from .types import PAID, CallMode, FactRow, FieldState, PriorValue, QueryPlan, Quote
from .types import RunRecord, RunStatus

__all__ = ["RunRefusedError", "execute", "priors_from"]

_GOOD: tuple[FieldState, ...] = ("fresh", "repaired")


@dataclass
class _ScopedDeps:
    """``RunnerDeps`` for one run: the core with the plan's allowlist."""

    base: JobDeps
    mode: CallMode


def _iso(deps: RunDeps) -> str:
    moment = dt.datetime.fromtimestamp(deps.base.clock(), dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse_unless_runnable(
    plan: QueryPlan, quote: Quote, deps: RunDeps, run_id: str
) -> RunRecord:
    """The claimed, paid record, or a refusal before any call. The quote's expiry is
    not checked here: it bounds the CONFIRMATION, and a payment that landed after it
    (a broadcast re-checked later) has still bought this run."""
    if not RUN_ID_RE.match(run_id):
        raise RunRefusedError("the run id is not of the form YYYYMMDD-HHMMSS-xxxxxx")
    if quote.plan_id != plan.plan_id or quote.customer != plan.customer:
        raise RunRefusedError("this quote was not made for this plan")
    if deps.mode != deps.base.mode:
        raise RunRefusedError("the run's mode and the core's mode disagree")
    if not plan.subjects or not plan.fields:
        raise RunRefusedError("the plan has no subject or no field to fill")
    claimed = deps.store.run(plan.customer, run_id)
    if claimed is None:
        raise RunRefusedError("the run was not claimed; it is claimed and paid first")
    if claimed.quote_id != quote.quote_id or claimed.plan_id != plan.plan_id:
        raise RunRefusedError("the claimed run is for another quote")
    if claimed.mode != deps.mode:
        raise RunRefusedError("the claimed run's mode and the run's mode disagree")
    if claimed.status != "running":
        raise RunRefusedError("this run already ran")
    if claimed.payment_status not in PAID:
        raise RunRefusedError(
            "the run is not paid; no upstream call is made before the payment lands"
        )
    return claimed


def priors_from(rows: list[dict[str, Any]], mode: str) -> dict[tuple[str, str], Prior]:
    """Per (subject, field): the last good value of earlier runs IN THIS MODE, its
    source, and since when the latest rows have been stale.

    A good status counts even with a ``None`` value (the source answered with nothing
    at the path, or a row written before the 8 Oct storage ruling). ``Store.last_good`` is not used because it does not filter by mode, and a
    recorded value must never be the snapshot a live run is judged against.
    """
    good: dict[tuple[str, str], tuple[PriorValue, str]] = {}
    stale: dict[tuple[str, str], str | None] = {}
    ordered = sorted(
        (r for r in rows if r.get("mode") == mode),
        key=lambda r: (str(r.get("run_id")), str(r.get("subject"))),
    )
    for row in ordered:
        key = (str(row.get("subject")), str(row.get("field")))
        status, run_id = row.get("status"), str(row.get("run_id"))
        if status in _GOOD:
            value = row.get("value")
            prior = PriorValue(
                run_id, value if isinstance(value, str) else None, status
            )
            good[key] = (prior, str(row.get("source") or ""))
            stale[key] = None
        elif status == "stale":
            stale[key] = stale.get(key) or row.get("stale_since") or run_id
    out: dict[tuple[str, str], Prior] = {}
    for key in set(good) | set(stale):
        held = good.get(key)
        out[key] = Prior(
            held[0] if held else None, held[1] if held else "", stale.get(key)
        )
    return out


def _fact(
    plan: QueryPlan,
    run_id: str,
    mode: CallMode,
    subject: str,
    field_name: str,
    outcome: Outcome,
    prior: Prior | None,
    now: str,
) -> FactRow:
    res = outcome.resolution
    state = res.state
    if outcome.source is not None:
        source, value = outcome.source.candidate_id, outcome.value
    elif state == "stale" and prior is not None:
        source, value = prior.source, outcome.value
    else:
        source, value = "", None
    return FactRow(
        customer=plan.customer,
        plan_id=plan.plan_id,
        run_id=run_id,
        mode=mode,
        subject=subject,
        field=field_name,
        value=value,
        source=source,
        fetched_at=now,
        status=state,
        previous=prior.last_good if prior else None,
        agreement="no data" if state == "no data" else "only one",
        failover=state == "repaired",
        stale_since=res.stale_since,
        note=res.line() if res.trouble is not None else "",
    )


def _status(stopped: bool, counts: Counter[str]) -> RunStatus:
    """``failed`` only when the run has no value at all to show: a stale value is the
    last good one, kept and marked, and that is a partial answer, not none."""
    if stopped:
        return "stopped"
    if not any(counts[s] for s in _GOOD) and not counts["stale"]:
        return "failed"
    if counts["stale"] or counts["no data"]:
        return "partial"
    return "complete"


def execute(plan: QueryPlan, quote: Quote, deps: RunDeps, *, run_id: str) -> RunResult:
    """Run the claimed, paid run ``run_id`` of ``plan`` once at ``quote``. Raises
    ``RunRefusedError`` before any call when the run is not claimed and paid."""
    record = _refuse_unless_runnable(plan, quote, deps, run_id)
    targets = plan_targets(plan)
    runner: Runner[_ScopedDeps] = Runner(
        deps=_ScopedDeps(
            replace(deps.base, api=replace(deps.base.api, targets=targets)), deps.mode
        ),
        guard=SpendGuard(
            deps.base.ledger,
            per_run_cap=quote.upstream_max_cents,
            total_cap=deps.base.total_cap,
        ),
        quotes={t: t.pinned_cents for t in targets},
        run_id=run_id,
    )
    try:
        facts = _run_cells(plan, deps, runner, {(t.api, t.path): t for t in targets})
        call_rows = [{**row, "plan_id": plan.plan_id} for row in runner.ledger_rows]
        # Calls first: they are the spend record, and must outlive a failed fact write.
        if call_rows:
            deps.store.append_calls(plan.customer, call_rows)
        deps.store.write_facts(plan.customer, facts)
    except Exception:
        _finish_failed(deps, record, runner)
        raise
    counts: Counter[str] = Counter(f.status for f in facts)
    final = replace(
        record,
        status=_status(runner.stop is not None, counts),
        finished_at=_iso(deps),
        upstream_charged_cents=runner.guard.spent,
        calls=len(runner.calls),
        skipped_calls=runner.skipped,
        counts={s: counts[s] for s in FIELD_STATES},
        stop_code=runner.stop.code if runner.stop is not None else None,
        notices=_notices(runner),
    )
    deps.store.finish_run(final)
    return RunResult(final, tuple(facts), tuple(call_rows))


def _notices(runner: Runner[Any]) -> tuple[str, ...]:
    """The runner's notices and, for a stopped run, WHY it stopped: the stop code alone
    cannot tell a crossed cap from a ledger that did not answer. ``ListJobError``
    messages are our words, never a secret."""
    if runner.stop is None:
        return tuple(runner.notices)
    return (*runner.notices, f"stopped ({runner.stop.code}): {runner.stop.message}")


def _run_cells(
    plan: QueryPlan,
    deps: RunDeps,
    runner: Runner[_ScopedDeps],
    targets: dict[tuple[str, str], Target],
) -> list[FactRow]:
    priors = priors_from(deps.store.plan_facts(plan.customer, plan.plan_id), deps.mode)
    state = CellState(
        run=runner,
        baseline=baseline_from(
            deps.store.calls(plan.customer), mode=deps.mode, before_run=runner.run_id
        ),
        targets=targets,
    )
    facts: list[FactRow] = []
    for subject in plan.subjects:
        for planned in plan.fields:
            prior = priors.get((subject, planned.name))
            outcome = resolve_cell(state, planned, subject, prior)
            facts.append(
                _fact(
                    plan,
                    runner.run_id,
                    deps.mode,
                    subject,
                    planned.name,
                    outcome,
                    prior,
                    _iso(deps),
                )
            )
    return facts


def _finish_failed(deps: RunDeps, record: RunRecord, runner: Runner[Any]) -> None:
    """Mark the run failed with what it spent. A store that cannot even do that is
    left as ``running``; the original error is the one the caller sees."""
    failed = replace(
        record,
        status="failed",
        finished_at=_iso(deps),
        upstream_charged_cents=runner.guard.spent,
        calls=len(runner.calls),
        skipped_calls=runner.skipped,
        notices=("the run failed before its facts were stored",),
    )
    # Deliberately not re-raised: the caller must see the error that failed the run,
    # not a second one from the store that could not record it.
    with contextlib.suppress(StoreError):
        deps.store.finish_run(failed)
