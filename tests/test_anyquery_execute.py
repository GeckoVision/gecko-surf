"""execute(): a plan runs through the Orthogonal core with a plan-scoped allowlist,
capped at its quote, repairs a broken field from the next ranked source, and leaves
facts, call rows and a run record in the customer's store. All offline: a scripted
transport stands in for Orthogonal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from gecko.access import public_session
from gecko.anyquery.execute import RunRefusedError, execute, priors_from
from gecko.anyquery.history import RunWithheldError, cell_history, changes_since
from gecko.anyquery.mongo import MongoStore
from gecko.anyquery.run import RunDeps, claim_record
from gecko.anyquery.store import DuplicateRunError, MemoryStore, Store
from gecko.anyquery.types import (
    InputBinding,
    PlannedField,
    PlannedSource,
    QueryPlan,
    Quote,
)
from gecko.caller import PreparedRequest
from gecko.orthogonal.api import OrthogonalApi
from gecko.orthogonal.errors import NotAllowedError
from gecko.orthogonal.job import JobDeps
from gecko.orthogonal.spec import CRUST_IDENTIFY, RUN_OP
from gecko.orthogonal.spend import MemoryLedger
from tests.anyquery_fake_mongo import FakeMongoClient

NOW = "2026-10-08T12:00:00Z"
EPOCH = 1_791_460_800.0  # 2026-10-08T12:00:00Z
LATER = "2026-10-08T13:00:00Z"


Reply = tuple[int, Any]


def _ok(data: Any, cents: int = 2, rid: str = "req") -> Reply:
    return 200, {"success": True, "priceCents": cents, "requestId": rid, "data": data}


@dataclass
class Upstream:
    """Scripted Orthogonal: (api, path) -> a function of the request body."""

    answers: dict[tuple[str, str], Callable[[dict[str, Any]], Reply]]
    seen: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, req: PreparedRequest) -> Reply:
        body = req.json_body
        assert isinstance(body, dict)
        self.seen.append(body)
        answer = self.answers.get((body["api"], body["path"]))
        if answer is None:
            return 404, {}
        return answer(body)


def _source(
    rank: int,
    api: str,
    path: str,
    field_path: str,
    *,
    method: str = "GET",
    cents: int = 2,
    params: tuple[str, ...] = ("company",),
    storage: str = "allowed",
    catalog: str = "orthogonal",
) -> PlannedSource:
    return PlannedSource(
        rank=rank,
        candidate_id=f"{catalog}:{api}:{method}:{path}",
        catalog=catalog,  # type: ignore[arg-type]
        api=api,
        path=path,
        price_cents=cents,
        field_path=field_path,
        inputs=tuple(InputBinding(p, "subject") for p in params),
        storage=storage,  # type: ignore[arg-type]
    )


FUND_A = _source(0, "fundco", "/v1/funding", "rounds[].amount")
FUND_B = _source(1, "otherfund", "/v2/money", "last.amount_usd")
HIRE = _source(0, "fundco", "/v1/funding", "hiring.open_roles")


def _plan(*fields: PlannedField, subjects: tuple[str, ...] = ("acme",)) -> QueryPlan:
    return QueryPlan(
        plan_id="plan-1",
        customer="acct-a",
        query="funding for these",
        subjects=subjects,
        fields=fields
        or (PlannedField("funding", "last round size", "number", (FUND_A, FUND_B)),),
        unplanned=(),
        planner="fake",
        planner_model=None,
        catalog_sha="sha",
        created_at=NOW,
    )


def _quote(plan: QueryPlan, quote_id: str = "q1", cents: int | None = None) -> Quote:
    worst = len(plan.subjects) * sum(
        s.price_cents for f in plan.fields for s in f.sources
    )
    upstream = worst if cents is None else cents
    return Quote(
        quote_id=quote_id,
        plan_id=plan.plan_id,
        customer=plan.customer,
        upstream_max_cents=upstream,
        upstream_max_usdc_micro=upstream * 10_000,
        gecko_fee_usdc_micro=0,
        discount_usdc_micro=0,
        total_usdc_micro=upstream * 10_000,
        max_calls=1,
        price_from="pinned",
        created_at=NOW,
        expires_at=LATER,
    )


def _deps(
    upstream: Upstream, store: Store, tmp_path: Path, *, mode: str = "recorded"
) -> RunDeps:
    base = JobDeps(
        api=OrthogonalApi(transport=upstream, session=public_session()),
        ledger=MemoryLedger(),
        folder=tmp_path,
        mode=mode,  # type: ignore[arg-type]
        clock=lambda: EPOCH,
        total_cap=1_000,
    )
    return RunDeps(
        base=base,
        mode=mode,
        store=store,
        public_fetch=lambda url: (404, ""),  # type: ignore[arg-type]
    )


def _execute(plan: QueryPlan, quote: Quote, deps: RunDeps, *, run_id: str) -> Any:
    """The caller's half of the contract: claim the quote and see it paid, then run."""
    deps.store.start_run(
        claim_record(
            plan, quote, run_id=run_id, mode=deps.mode, now=NOW, payment_status="landed"
        )
    )
    return execute(plan, quote, deps, run_id=run_id)


def _funding(amount: Any) -> Callable[[dict[str, Any]], Reply]:
    return lambda body: _ok(
        {"rounds": [{"amount": amount}], "hiring": {"open_roles": 4}}
    )


# -- the plain run ---------------------------------------------------------------------


def test_a_run_stores_fresh_facts_call_rows_and_a_complete_record(
    tmp_path: Path,
) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(5_000_000)})
    plan = _plan(subjects=("acme", "globex"))
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )

    assert [(f.subject, f.value, f.status) for f in result.facts] == [
        ("acme", "5000000", "fresh"),
        ("globex", "5000000", "fresh"),
    ]
    assert result.facts[0].source == FUND_A.candidate_id
    assert result.facts[0].fetched_at == NOW and result.facts[0].previous is None
    # the subject goes out as a query string on a GET source
    assert upstream.seen[0] == {
        "api": "fundco",
        "path": "/v1/funding",
        "query": {"company": "acme"},
    }
    record = store.run("acct-a", "20261008-120000-aaaaaa")
    assert record == result.record
    # The final record keeps the claim's payment: execute never decides it.
    assert (record.status, record.payment_status) == ("complete", "landed")
    assert (record.calls, record.upstream_charged_cents) == (2, 4)
    assert record.counts == {"fresh": 2, "repaired": 0, "stale": 0, "no data": 0}
    assert store.facts("acct-a", run_id=record.run_id) == list(result.facts)
    calls = store.calls("acct-a", run_id=record.run_id)
    assert len(calls) == 2 and calls[0]["plan_id"] == "plan-1"
    # call rows keep names and counts, never values, and never the subject in clear
    flat = repr(calls)
    assert "5000000" not in flat and "acme" not in flat
    assert "rounds" in calls[0]["output_fields"]


def test_two_fields_on_one_request_share_one_call(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(7)})
    plan = _plan(
        PlannedField("funding", "round", "number", (FUND_A,)),
        PlannedField("hiring", "open roles", "number", (HIRE,)),
    )
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    assert [f.value for f in result.facts] == ["7", "4"]
    assert len(upstream.seen) == 1 and result.record.upstream_charged_cents == 2


@pytest.mark.parametrize("storage", ["unknown", "forbidden"])
def test_every_source_value_is_stored_for_the_customer_whatever_its_storage_label(
    tmp_path: Path, storage: str
) -> None:
    """Founder ruling, 8 Oct: store every source's values for the customer who asked.
    ``storage`` is metadata on the source now; it gates nothing."""
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(9)})
    labelled = replace(FUND_A, storage=storage)  # type: ignore[arg-type]
    plan = _plan(PlannedField("funding", "round", "number", (labelled,)))
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    assert (result.facts[0].value, result.facts[0].status) == ("9", "fresh")
    assert store.last_good("acct-a", "plan-1")[("acme", "funding")].value == "9"


def test_the_allowlist_is_the_plan_and_nothing_else(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    deps = _deps(upstream, store, tmp_path)
    plan = _plan()
    _execute(plan, _quote(plan), deps, run_id="20261008-120000-aaaaaa")
    from gecko.anyquery.calls import plan_targets

    scoped = replace(deps.base.api, targets=plan_targets(plan))
    with pytest.raises(NotAllowedError):
        scoped.call(RUN_OP, {"api": CRUST_IDENTIFY.api, "path": CRUST_IDENTIFY.path})
    with pytest.raises(NotAllowedError):
        scoped.call(
            RUN_OP, {"api": "fundco", "path": "/v1/funding", "query": {"x": "1"}}
        )
    # and the fixed jobs' client is unchanged: the plan's endpoint is not on it
    with pytest.raises(NotAllowedError):
        deps.base.api.call(RUN_OP, {"api": "fundco", "path": "/v1/funding"})


def test_a_post_source_sends_its_inputs_as_a_body_and_fills_a_domain_slot(
    tmp_path: Path,
) -> None:
    store = MemoryStore()
    post = _source(
        0, "crust", "/company/identify", "name", method="POST", params=("domain",)
    )
    slot = _source(0, "pl", "/v3/companies/{domain}/news", "items[].title", params=())
    upstream = Upstream(
        {
            ("crust", "/company/identify"): lambda b: _ok({"name": "Acme Inc"}),
            ("pl", "/v3/companies/acme.com/news"): lambda b: _ok(
                {"items": [{"title": "Raised"}]}
            ),
        }
    )
    plan = _plan(
        PlannedField("name", "legal name", "text", (post,)),
        PlannedField("news", "news", "text", (slot,)),
        subjects=("acme.com", "not a domain"),
    )
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    assert upstream.seen[0] == {
        "api": "crust",
        "path": "/company/identify",
        "body": {"domain": "acme.com"},
    }
    by = {(f.subject, f.field): f for f in result.facts}
    assert by[("acme.com", "news")].value == "Raised"
    refused = by[("not a domain", "news")]
    assert refused.status == "no data" and "plain domain" in refused.note
    assert len(upstream.seen) == 3  # the refused slot was never sent


def test_an_unwired_catalog_is_skipped_in_words(tmp_path: Path) -> None:
    store = MemoryStore()
    paysh = _source(1, "pay", "/x", "a", catalog="paysh")
    plan = _plan(PlannedField("funding", "round", "number", (FUND_A, paysh)))
    upstream = Upstream({("fundco", "/v1/funding"): lambda b: (500, {})})
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    fact = result.facts[0]
    assert fact.status == "no data" and "paysh sources are not wired" in fact.note
    assert result.record.status == "failed"


# -- repair ----------------------------------------------------------------------------


def test_a_failed_first_source_is_repaired_from_the_next(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream(
        {
            ("fundco", "/v1/funding"): lambda b: (500, {}),
            ("otherfund", "/v2/money"): lambda b: _ok(
                {"last": {"amount_usd": "5000000.0"}}
            ),
        }
    )
    plan = _plan()
    result = _execute(
        plan,
        _quote(plan),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    fact = result.facts[0]
    assert (fact.status, fact.value, fact.failover) == ("repaired", "5000000", True)
    assert fact.source == FUND_B.candidate_id
    assert "fundco /v1/funding failed (HTTP 500)" in fact.note
    assert result.record.status == "complete"


def _run_twice(
    tmp_path: Path,
    second: dict[tuple[str, str], Callable[[dict[str, Any]], Reply]],
    store: Store,
) -> tuple[Any, Any]:
    plan = _plan()
    first = Upstream({("fundco", "/v1/funding"): _funding(5_000_000)})
    one = _execute(
        plan,
        _quote(plan, "q1"),
        _deps(first, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    two = _execute(
        plan,
        _quote(plan, "q2"),
        _deps(Upstream(second), store, tmp_path),
        run_id="20261008-130000-bbbbbb",
    )
    return one, two


def test_a_drifted_source_is_passed_over_and_a_disagreeing_repair_goes_stale(
    tmp_path: Path,
) -> None:
    store = MemoryStore()
    _one, two = _run_twice(
        tmp_path,
        {
            # the reply no longer carries rounds[].amount: drift, not a value of 0
            ("fundco", "/v1/funding"): lambda b: _ok(
                {"funding_rounds": [{"amount": 1}]}
            ),
            ("otherfund", "/v2/money"): lambda b: _ok({"last": {"amount_usd": 9}}),
        },
        store,
    )
    fact = two.facts[0]
    assert fact.status == "stale" and fact.value == "5000000"
    assert fact.stale_since == "20261008-130000-bbbbbb"
    assert fact.source == FUND_A.candidate_id
    assert (
        fact.previous is not None and fact.previous.run_id == "20261008-120000-aaaaaa"
    )
    assert "drifted (field path missing)" in fact.note
    assert "differs from the last good value" in fact.note
    assert two.record.status == "partial"


def test_a_repair_that_agrees_with_the_last_value_is_published(tmp_path: Path) -> None:
    store = MemoryStore()
    _one, two = _run_twice(
        tmp_path,
        {
            ("fundco", "/v1/funding"): lambda b: (500, {}),
            ("otherfund", "/v2/money"): lambda b: _ok(
                {"last": {"amount_usd": 5_000_000}}
            ),
        },
        store,
    )
    assert (two.facts[0].status, two.facts[0].value) == ("repaired", "5000000")
    assert "agrees with the last good value" in two.facts[0].note


def test_stale_since_holds_across_runs(tmp_path: Path) -> None:
    store = MemoryStore()
    broken = {("fundco", "/v1/funding"): lambda b: (500, {})}
    _run_twice(tmp_path, broken, store)
    plan = _plan()
    three = _execute(
        plan,
        _quote(plan, "q3"),
        _deps(Upstream(broken), store, tmp_path),
        run_id="20261008-140000-cccccc",
    )
    assert three.facts[0].status == "stale"
    assert three.facts[0].stale_since == "20261008-130000-bbbbbb"


def test_a_recorded_value_is_never_a_live_snapshot() -> None:
    rows = [
        {
            "run_id": "1",
            "mode": "recorded",
            "subject": "a",
            "field": "f",
            "status": "fresh",
            "value": "x",
            "source": "s",
        },
    ]
    assert priors_from(rows, "live") == {}
    assert priors_from(rows, "recorded")[("a", "f")].last_good is not None


# -- money and refusals ----------------------------------------------------------------


def test_the_quote_caps_the_run_and_later_calls_are_skipped(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan(subjects=("a", "b", "c", "d"))
    result = _execute(
        plan,
        _quote(plan, cents=4),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    assert result.record.status == "stopped"
    assert result.record.stop_code == "spend_cap"
    assert result.record.upstream_charged_cents == 4
    # c is refused by the guard before sending; d is never tried
    assert len(upstream.seen) == 2 and result.record.skipped_calls == 1
    assert [f.status for f in result.facts] == ["fresh", "fresh", "no data", "no data"]


def test_one_quote_runs_once(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan()
    deps = _deps(upstream, store, tmp_path)
    _execute(plan, _quote(plan), deps, run_id="20261008-120000-aaaaaa")
    with pytest.raises(DuplicateRunError):
        _execute(plan, _quote(plan), deps, run_id="20261008-120001-aaaaab")
    with pytest.raises(RunRefusedError, match="already ran"):
        execute(plan, _quote(plan), deps, run_id="20261008-120000-aaaaaa")
    assert len(upstream.seen) == 1


@pytest.mark.parametrize("payment", ["pending", "failed", "credited", "not_due"])
def test_a_claim_that_is_not_paid_makes_no_call(tmp_path: Path, payment: str) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan()
    deps = _deps(upstream, store, tmp_path)
    run_id = "20261008-120000-aaaaaa"
    store.start_run(
        claim_record(
            plan,
            _quote(plan),
            run_id=run_id,
            mode=deps.mode,
            now=NOW,
            payment_status=payment,  # type: ignore[arg-type]
        )
    )
    with pytest.raises(RunRefusedError, match="not paid"):
        execute(plan, _quote(plan), deps, run_id=run_id)
    assert upstream.seen == [] and store.calls("acct-a") == []


def test_an_unclaimed_run_makes_no_call(tmp_path: Path) -> None:
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan()
    deps = _deps(upstream, MemoryStore(), tmp_path)
    with pytest.raises(RunRefusedError, match="not claimed"):
        execute(plan, _quote(plan), deps, run_id="20261008-120000-aaaaaa")
    assert upstream.seen == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, q, d, r: (p, replace(q, plan_id="other"), d, r),
        lambda p, q, d, r: (p, q, replace(d, mode="live"), r),
        lambda p, q, d, r: (p, q, d, "not-a-run-id"),
        lambda p, q, d, r: (replace(p, subjects=()), q, d, r),
    ],
    ids=["other-plan", "mode-mismatch", "bad-run-id", "no-subjects"],
)
def test_refusals_happen_before_anything_is_stored_or_spent(
    tmp_path: Path, mutate: Callable[..., tuple[Any, Any, Any, Any]]
) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan()
    args = mutate(
        plan, _quote(plan), _deps(upstream, store, tmp_path), "20261008-120000-aaaaaa"
    )
    with pytest.raises(RunRefusedError):
        execute(args[0], args[1], args[2], run_id=args[3])
    assert upstream.seen == [] and store.runs("acct-a") == []


def test_a_store_failure_marks_the_run_failed_and_keeps_the_spend(
    tmp_path: Path,
) -> None:
    class Exploding(MemoryStore):
        def write_facts(self, customer: str, rows: Any) -> None:
            raise RuntimeError("disk full")

    store = Exploding()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan()
    with pytest.raises(RuntimeError):
        _execute(
            plan,
            _quote(plan),
            _deps(upstream, store, tmp_path),
            run_id="20261008-120000-aaaaaa",
        )
    record = store.run("acct-a", "20261008-120000-aaaaaa")
    assert record is not None and record.status == "failed"
    assert record.upstream_charged_cents == 2
    assert len(store.calls("acct-a")) == 1


# -- on Mongo, end to end: run, deliver, ask what changed ------------------------------


def test_on_mongo_changes_since_reads_only_delivered_runs(tmp_path: Path) -> None:
    store = MongoStore(FakeMongoClient())
    plan = _plan(subjects=("acme", "globex"))
    first = Upstream({("fundco", "/v1/funding"): _funding(5)})
    one = _execute(
        plan,
        _quote(plan, "q1"),
        _deps(first, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    second = Upstream(
        {
            ("fundco", "/v1/funding"): lambda b: _ok(
                {"rounds": [{"amount": 6 if b["query"]["company"] == "acme" else 5}]}
            )
        }
    )
    two = _execute(
        plan,
        _quote(plan, "q2"),
        _deps(second, store, tmp_path),
        run_id="20261008-130000-bbbbbb",
    )

    # Both runs were paid before they ran; hide them as an undelivered run would be.
    store.finish_run(replace(one.record, payment_status="credited"))
    store.finish_run(replace(two.record, payment_status="credited"))
    with pytest.raises(RunWithheldError):
        changes_since(store, "acct-a", one.record.run_id)
    store.finish_run(replace(one.record, payment_status="landed"))
    # the second run is not paid yet, so there is nothing later to compare with
    assert changes_since(store, "acct-a", one.record.run_id)["against_run"] is None
    store.finish_run(replace(two.record, payment_status="landed"))

    diff = changes_since(store, "acct-a", one.record.run_id)
    assert diff["against_run"] == two.record.run_id and diff["plan_id"] == "plan-1"
    assert [(c["subject"], c["change"], c["what"]) for c in diff["changes"]] == [
        ("acme", "changed", ("value",))
    ]
    change = diff["changes"][0]
    assert (change["value_untrusted_before"], change["value_untrusted_after"]) == (
        "5",
        "6",
    )
    assert "summary_before" not in change

    history = cell_history(store, "acct-a", "plan-1", "acme", "funding")
    assert [(h["run_id"], h["value_untrusted"]) for h in history] == [
        ("20261008-120000-aaaaaa", "5"),
        ("20261008-130000-bbbbbb", "6"),
    ]
    assert cell_history(store, "acct-b", "plan-1", "acme", "funding") == []


# -- live bug, 9 Oct: a run stopped on its first call at 0 of cap 50 ---------------------


def _fiber_plan() -> QueryPlan:
    comments = _source(
        0,
        "fiber",
        "/v1/instagram/post-comments",
        "comments[].text",
        method="POST",
        cents=4,
        params=("url",),
    )
    details = _source(
        0,
        "fiber",
        "/v1/instagram/post-details",
        "likes",
        cents=4,
        params=("url",),
    )
    return _plan(
        PlannedField("comments", "comments", "text", (comments,)),
        PlannedField("likes", "likes", "number", (details,)),
        subjects=("https://instagram.com/p/x",),
    )


def _on_mongo_ledger(deps: RunDeps, client: FakeMongoClient) -> RunDeps:
    from gecko.anyquery.upstream_spend import MongoSpendLedger
    from tests.anyquery_fake_mongo import FakeDuplicateKeyError, FakeUnavailableError

    def ledger() -> MongoSpendLedger:
        # pymongo's DuplicateKeyError is a PyMongoError: the driver guard sees it.
        return MongoSpendLedger(
            client, unavailable=(FakeUnavailableError, FakeDuplicateKeyError)
        )

    ledger().add(0)  # the host-wide row exists, at 0, from an earlier task
    return replace(deps, base=replace(deps.base, ledger=ledger(), total_cap=50))


def test_the_live_fiber_run_is_not_stopped_at_zero_of_fifty(tmp_path: Path) -> None:
    plan = _fiber_plan()
    quote = _quote(plan, cents=8)
    upstream = Upstream(
        {
            ("fiber", "/v1/instagram/post-comments"): lambda b: _ok(
                {"comments": [{"text": "hi"}]}, cents=4, rid="c"
            ),
            ("fiber", "/v1/instagram/post-details"): lambda b: _ok(
                {"likes": 7}, cents=4, rid="d"
            ),
        }
    )
    deps = _on_mongo_ledger(
        _deps(upstream, MemoryStore(), tmp_path, mode="live"), FakeMongoClient()
    )
    result = _execute(plan, quote, deps, run_id="20261009-120000-aaaaaa")

    assert result.record.stop_code is None, result.record.notices
    assert result.record.status == "complete"
    assert result.record.upstream_charged_cents == 8
    assert [r["charged_cents"] for r in result.call_rows] == [4, 4]


def test_a_stopped_run_records_why_it_stopped(tmp_path: Path) -> None:
    store = MemoryStore()
    upstream = Upstream({("fundco", "/v1/funding"): _funding(1)})
    plan = _plan(subjects=("a", "b", "c"))
    result = _execute(
        plan,
        _quote(plan, cents=4),
        _deps(upstream, store, tmp_path),
        run_id="20261008-120000-aaaaaa",
    )
    assert result.record.stop_code == "spend_cap"
    assert any("capped at $0.04" in n for n in result.record.notices)
    assert (
        store.run("acct-a", "20261008-120000-aaaaaa").notices == result.record.notices
    )
