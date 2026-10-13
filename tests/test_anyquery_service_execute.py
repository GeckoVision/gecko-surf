"""The service (lane 4) driving the real runner (lane 2), offline.

These tests pin the reading after the charge-first ruling (8 Oct): the service owns the
preflight, the claim (one run per quote), the capture BEFORE any call, and the credit
for an undelivered run; ``execute`` checks the claim is paid and owns the call rows, the
facts and the final record.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gecko.access import public_session
from gecko.anyquery.billing import FakeBilling
from gecko.anyquery.config import AnyQueryConfig
from gecko.anyquery.execute import execute
from gecko.anyquery.hosted import payee_usdc_ata
from gecko.anyquery.planner import FakePlanner, Planner
from gecko.anyquery.run import RunDeps
from gecko.anyquery.service import AnyQueryDeps, call_anyquery_tool
from gecko.anyquery.store import MemoryStore
from gecko.anyquery.types import (
    CandidateSet,
    InputBinding,
    PlanDraft,
    PlannedField,
    PlannedSource,
    PlanRequest,
    QueryPlan,
)
from gecko.caller import PreparedRequest
from gecko.orthogonal.api import OrthogonalApi
from gecko.orthogonal.job import JobDeps
from gecko.orthogonal.spend import MemoryLedger

ACCOUNT = "did:gecko:alice"
PAYER = "4jccRjipAcqhXfxVSmw1ZsJF7rTmhJt5pjYGFR8cZCU5"
QUERY = "funding for acme and globex"


@dataclass
class Clock:
    """One clock for the service (ISO) and the Orthogonal core (epoch)."""

    moment: dt.datetime = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)

    def iso(self) -> str:
        return self.moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def epoch(self) -> float:
        return self.moment.timestamp()

    def advance(self, seconds: int) -> None:
        self.moment += dt.timedelta(seconds=seconds)


@dataclass
class Upstream:
    answers: dict[tuple[str, str], Callable[[], Any]]
    seen: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, req: PreparedRequest) -> tuple[int, Any]:
        body = req.json_body
        assert isinstance(body, dict)
        self.seen.append(body)
        answer = self.answers.get((body["api"], body["path"]))
        if answer is None:
            return 404, {}
        return 200, {
            "success": True,
            "priceCents": 2,
            "requestId": "r",
            "data": answer(),
        }


SOURCE = PlannedSource(
    rank=0,
    candidate_id="orthogonal:fundco:GET:/v1/funding",
    catalog="orthogonal",
    api="fundco",
    path="/v1/funding",
    price_cents=2,
    field_path="rounds[].amount",
    inputs=(InputBinding("company", "subject"),),
    storage="allowed",
)
DRAFT = PlanDraft(
    subjects=("acme", "globex"),
    fields=(PlannedField("funding", "last round size", "number", (SOURCE,)),),
    unplanned=(),
    notes=(),
)


def _validate(
    draft: PlanDraft,
    request: PlanRequest,
    candidates: CandidateSet,
    config: AnyQueryConfig,
    *,
    planner: Planner,
    now: str,
) -> QueryPlan:
    return QueryPlan(
        plan_id="plan-1",
        customer=request.customer,
        query=request.query,
        subjects=draft.subjects,
        fields=draft.fields,
        unplanned=(),
        planner=planner.kind,
        planner_model=None,
        catalog_sha=candidates.snapshot_sha,
        created_at=now,
    )


def _deps(
    tmp_path: Path, upstream: Upstream, clock: Clock
) -> tuple[AnyQueryDeps, MemoryStore]:
    config = AnyQueryConfig()
    store = MemoryStore()
    billing = FakeBilling(
        config=config, store=store, wallets={ACCOUNT: (PAYER, 50_000_000)}
    )
    billing.clock = clock.iso
    base = JobDeps(
        api=OrthogonalApi(transport=upstream, session=public_session()),
        ledger=MemoryLedger(),
        folder=tmp_path,
        mode="recorded",
        clock=clock.epoch,
        total_cap=1_000,
    )
    deps = AnyQueryDeps(
        config=config,
        store=store,
        billing=billing,
        planner=FakePlanner(drafts={QUERY: DRAFT}),
        candidates_for=lambda request, catalog, k=40: catalog,
        validate_plan=_validate,
        catalog=lambda: CandidateSet((), "sha"),
        execute=execute,
        run_deps=RunDeps(
            base=base,
            mode="recorded",
            store=store,
            public_fetch=lambda _url: (404, ""),  # type: ignore[arg-type,return-value]
        ),
        payee_usdc_ata=lambda: payee_usdc_ata(config),
        clock=clock.iso,
    )
    return deps, store


def _run(deps: AnyQueryDeps) -> dict[str, Any]:
    planned = call_anyquery_tool(deps, ACCOUNT, "plan_query", {"query": QUERY})
    return call_anyquery_tool(
        deps,
        ACCOUNT,
        "run_query",
        {
            "plan_id": planned["plan"]["plan_id"],
            "quote_id": planned["quote"]["quote_id"],
            "confirm": True,
        },
    )


def test_a_confirmed_run_through_the_real_runner_completes_and_is_paid_once(
    tmp_path: Path,
) -> None:
    clock = Clock()
    upstream = Upstream(
        {("fundco", "/v1/funding"): lambda: {"rounds": [{"amount": 5}]}}
    )
    deps, store = _deps(tmp_path, upstream, clock)

    ran = _run(deps)

    assert ran["run"]["status"] == "complete", ran
    assert ran["run"]["payment_status"] == "landed"
    run_id = ran["run"]["run_id"]
    [record] = store.runs(ACCOUNT)
    assert record.run_id == run_id
    # Facts and call rows are written once, by the runner.
    assert len(store.facts(ACCOUNT, run_id=run_id)) == 2
    assert len(store.calls(ACCOUNT, run_id=run_id)) == 2
    charges = [e for e in store.ledger(ACCOUNT) if e.kind == "charge"]
    assert [c.status for c in charges] == ["landed"]


def test_confirming_the_same_quote_twice_runs_once_through_the_real_runner(
    tmp_path: Path,
) -> None:
    clock = Clock()
    upstream = Upstream(
        {("fundco", "/v1/funding"): lambda: {"rounds": [{"amount": 5}]}}
    )
    deps, store = _deps(tmp_path, upstream, clock)
    planned = call_anyquery_tool(deps, ACCOUNT, "plan_query", {"query": QUERY})
    args = {
        "plan_id": planned["plan"]["plan_id"],
        "quote_id": planned["quote"]["quote_id"],
        "confirm": True,
    }

    first = call_anyquery_tool(deps, ACCOUNT, "run_query", args)
    again = call_anyquery_tool(deps, ACCOUNT, "run_query", args)

    assert again["repeated"] is True
    assert again["run"]["run_id"] == first["run"]["run_id"]
    assert len(upstream.seen) == 2  # two subjects, one run
    assert len([e for e in store.ledger(ACCOUNT) if e.kind == "charge"]) == 1


def test_a_runner_crash_after_the_charge_is_a_failed_credited_run(
    tmp_path: Path,
) -> None:
    clock = Clock()
    secret = "mongodb+srv://user:hunter2@cluster"

    def boom() -> Any:
        raise RuntimeError(secret)

    deps, store = _deps(tmp_path, Upstream({("fundco", "/v1/funding"): boom}), clock)
    original = store.write_facts

    def broken_write(*_a: Any, **_k: Any) -> None:
        raise RuntimeError(secret)

    store.write_facts = broken_write  # type: ignore[method-assign]
    ran = _run(deps)
    store.write_facts = original  # type: ignore[method-assign]

    assert ran["run"]["status"] == "failed"
    assert ran["run"]["payment_status"] == "credited"
    assert secret not in repr(ran)
    [record] = store.runs(ACCOUNT)
    assert (record.status, record.payment_status) == ("failed", "credited")
    kinds = sorted((e.kind, e.amount_usdc_micro) for e in store.ledger(ACCOUNT))
    [(_, charged), (_, credited)] = kinds
    assert [k for k, _ in kinds] == ["charge", "run_credit"] and charged == credited


def test_a_paid_run_the_runner_refuses_makes_no_call_and_is_credited(
    tmp_path: Path,
) -> None:
    clock = Clock()
    upstream = Upstream({})
    deps, store = _deps(tmp_path, upstream, clock)
    planned = call_anyquery_tool(deps, ACCOUNT, "plan_query", {"query": QUERY})
    # The run's mode and the core's disagree: the runner refuses after the charge.
    deps.run_deps.mode = "live"
    out = call_anyquery_tool(
        deps,
        ACCOUNT,
        "run_query",
        {
            "plan_id": planned["plan"]["plan_id"],
            "quote_id": planned["quote"]["quote_id"],
            "confirm": True,
        },
    )

    assert out["status"] == "not_delivered", out
    assert out["run"]["payment_status"] == "credited"
    assert any("not run" in n for n in out["run"]["notices"])
    assert upstream.seen == []
    credit = out["credit"]
    assert credit["amount_usdc_micro"] == planned["quote"]["total_usdc_micro"]


def test_run_changes_across_two_runs_of_the_real_runner(tmp_path: Path) -> None:
    clock = Clock()
    amounts = iter([5, 8])
    state = {"amount": 5}

    def funding() -> Any:
        return {"rounds": [{"amount": state["amount"]}]}

    deps, _store = _deps(
        tmp_path, Upstream({("fundco", "/v1/funding"): funding}), clock
    )
    state["amount"] = next(amounts)
    first = _run(deps)
    clock.advance(3_600)
    state["amount"] = next(amounts)
    second = _run(deps)

    assert second["run"]["status"] == "complete"
    out = call_anyquery_tool(
        deps,
        ACCOUNT,
        "run_changes",
        {"run_id": first["run"]["run_id"]},
    )
    assert "error" not in out, out
    assert [(c["subject"], c["field"]) for c in out["changes"]] == [
        ("acme", "funding"),
        ("globex", "funding"),
    ]
