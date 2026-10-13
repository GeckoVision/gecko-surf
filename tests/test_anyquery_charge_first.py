"""The money path, founder ruling 8 Oct (after the review): CHARGE BEFORE any paid call.

The payment is captured at the confirmed quote and seen to land before the runner makes a
single upstream call. A run that delivers no fresh or repaired fact earns a ledger credit
equal to what it cost, taken off the next run (no transfer). A delivery that fails after
the facts were written hands no rows out. A payment broadcast but not yet visible is
``pending``: re-checked by its signature, never charged again.

Fakes only: FakePlanner, MemoryStore, FakeBilling and the scripted runner of the surface
tests. No chain, no key, no money.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gecko.anyquery.billing import FakeBilling, available_discount
from gecko.anyquery.hosted import AnyQuerySurface
from gecko.anyquery.run import RunDeps, RunResult
from gecko.anyquery.store import StoreUnavailableError
from gecko.anyquery.surface import ANYQUERY_TOOLS
from gecko.anyquery.types import QueryPlan, Quote
from gecko.rpc import RpcError
from tests.test_anyquery_surface import (
    ACCOUNT,
    FIRST,
    QUERY,
    ScriptedRun,
    _call,
    _deps,
    _plan_and_run,
)


def _ledger(deps: Any, kind: str) -> list[Any]:
    return [e for e in deps.store.ledger(ACCOUNT) if e.kind == kind]


@dataclass
class Spy(ScriptedRun):
    """Records what the ledger held at the moment the runner was asked to run."""

    seen_charges: list[list[str]] | None = None

    def __call__(
        self, plan: QueryPlan, quote: Quote, deps: RunDeps, *, run_id: str
    ) -> RunResult:
        held = deps.store.ledger(plan.customer)
        self.seen_charges = [[e.status for e in held if e.kind == "charge"]]
        return super().__call__(plan, quote, deps, run_id=run_id)


def test_the_charge_has_landed_before_the_runner_makes_any_call(
    tmp_path: Path,
) -> None:
    runner = Spy([FIRST])
    deps = _deps(tmp_path, runner=runner)
    _planned, out = _plan_and_run(deps)
    assert runner.calls == 1
    assert runner.seen_charges == [["landed"]]
    assert out["run"]["payment_status"] == "landed"


def test_a_payment_that_does_not_land_never_reaches_the_runner(
    tmp_path: Path,
) -> None:
    runner = ScriptedRun([FIRST])
    deps = _deps(tmp_path, runner=runner)
    assert isinstance(deps.billing, FakeBilling)
    deps.billing.fail_capture = True
    _planned, out = _plan_and_run(deps)
    assert runner.calls == 0
    assert out["status"] == "not_run"
    assert (out["run"]["status"], out["run"]["payment_status"]) == ("failed", "failed")
    assert "nothing was run" in out["next"]
    assert _call(deps, "run_rows", run_id=out["run"]["run_id"])["code"] == "withheld"


def test_an_empty_run_is_charged_then_credited_and_the_credit_is_spent_once(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path, runner=ScriptedRun([{}, FIRST, FIRST]))
    planned, out = _plan_and_run(deps)
    paid = planned["quote"]["total_usdc_micro"]
    assert out["status"] == "not_delivered"
    assert out["run"]["payment_status"] == "credited"
    assert [c.amount_usdc_micro for c in _ledger(deps, "charge")] == [paid]
    [credit] = _ledger(deps, "run_credit")
    assert credit.amount_usdc_micro == paid and credit.status == "landed"
    assert credit.signature is None  # a ledger line, no transfer
    assert "credit on your next run" in out["next"]
    assert _call(deps, "run_rows", run_id=out["run"]["run_id"])["code"] == "withheld"
    assert available_discount(deps.store.ledger(ACCOUNT)) == paid

    # The next run is free up to the credit, and the credit is used up by it.
    second_plan, second = _plan_and_run(deps)
    assert second_plan["quote"]["total_usdc_micro"] == 0
    assert second["run"]["payment_status"] == "not_charged"
    assert len(_call(deps, "run_rows", run_id=second["run"]["run_id"])["rows"]) == 4
    assert available_discount(deps.store.ledger(ACCOUNT)) == 0
    third_plan, _third = _plan_and_run(deps)
    assert third_plan["quote"]["total_usdc_micro"] == paid


def test_a_store_error_after_the_facts_are_written_hands_no_rows_out(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    original = deps.store.finish_run
    calls = {"n": 0}

    def flaky_finish(run: Any) -> None:
        # The claim and the payment update pass; the runner's final record does not.
        calls["n"] += 1
        if run.status == "complete":
            raise StoreUnavailableError("mongodb+srv://user:hunter2@cluster refused")
        original(run)

    deps.store.finish_run = flaky_finish  # type: ignore[method-assign]
    planned, out = _plan_and_run(deps)
    deps.store.finish_run = original  # type: ignore[method-assign]

    run_id = out["run"]["run_id"]
    assert deps.store.facts(ACCOUNT, run_id=run_id)  # written, and still withheld
    assert out["status"] == "not_delivered"
    assert out["run"]["payment_status"] == "credited"
    assert "hunter2" not in repr(out)
    assert _call(deps, "run_rows", run_id=run_id)["code"] == "withheld"
    assert _call(deps, "run_rows")["code"] == "no_runs"
    assert _call(deps, "run_changes", run_id=run_id)["code"] == "withheld"
    [credit] = _ledger(deps, "run_credit")
    assert credit.amount_usdc_micro == planned["quote"]["total_usdc_micro"]


def test_a_broadcast_not_yet_visible_is_pending_rechecked_and_never_recharged(
    tmp_path: Path,
) -> None:
    runner = ScriptedRun([FIRST])
    deps = _deps(tmp_path, runner=runner)
    assert isinstance(deps.billing, FakeBilling)
    deps.billing.unseen_capture = True
    planned, out = _plan_and_run(deps)
    assert out["status"] == "payment_pending"
    assert out["run"]["payment_status"] == "pending"
    [charge] = _ledger(deps, "charge")
    assert charge.status == "pending" and charge.signature
    assert charge.signature in out["next"] and "never charges again" in out["next"]
    assert runner.calls == 0
    args = {
        "plan_id": planned["plan"]["plan_id"],
        "quote_id": planned["quote"]["quote_id"],
        "confirm": True,
    }

    still = _call(deps, "run_query", **args)
    assert still["status"] == "payment_pending" and runner.calls == 0
    assert deps.billing.rechecks == [charge.signature]

    deps.billing.unseen_capture = False  # the transaction becomes visible
    landed = _call(deps, "run_query", **args)
    assert landed["status"] == "ran" and landed["run"]["payment_status"] == "landed"
    assert runner.calls == 1
    [charge] = _ledger(deps, "charge")
    assert charge.status == "landed"
    again = _call(deps, "run_query", **args)
    assert again["repeated"] is True and runner.calls == 1
    assert len(_ledger(deps, "charge")) == 1


def test_a_capture_that_raises_anything_is_settled_from_the_ledger(
    tmp_path: Path,
) -> None:
    # A broadcast payment whose capture then raised something other than BillingError
    # (a 429 escaping a poll) must not escape the tool: the ledger says what happened.
    runner = ScriptedRun([FIRST])
    deps = _deps(tmp_path, runner=runner)
    assert isinstance(deps.billing, FakeBilling)
    deps.billing.unseen_capture = True
    capture = deps.billing.capture

    def raising_capture(payment: Any) -> Any:
        capture(payment)
        raise RpcError("getSignatureStatuses: 429 Too Many Requests")

    deps.billing.capture = raising_capture  # type: ignore[method-assign]
    _planned, out = _plan_and_run(deps)
    assert out["status"] == "payment_pending"
    [charge] = _ledger(deps, "charge")
    assert charge.signature and charge.signature in out["next"]
    assert runner.calls == 0


def test_run_query_says_charge_before_and_credit_on_empty(tmp_path: Path) -> None:
    [run_query] = [t for t in ANYQUERY_TOOLS if t["name"] == "run_query"]
    text = run_query["description"].lower()
    assert "before" in text and "charged 0" not in text
    assert "credit" in text and "next run" in text
    assert "pending" in text
    # The server instructions an agent reads first must tell the same money order.
    order = AnyQuerySurface(_deps(tmp_path)).instructions.lower()
    assert "before any paid call" in order
    assert "credit" in order and "next run" in order
    assert "0 if it found nothing" not in order and "then charges" not in order


def test_payments_note_says_charge_before_and_credit(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    _plan_and_run(deps)
    note = _call(deps, "payments")["note"]
    assert "before" in note and "credit" in note and "after it runs" not in note


def test_plan_query_quote_text_no_longer_promises_a_zero_charge(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    out = _call(deps, "plan_query", query=QUERY)
    assert "charged 0" not in out["quote"]["worst_case"]
    assert "credit" in out["quote"]["worst_case"]
