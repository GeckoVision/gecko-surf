"""MongoStore against the in-memory fake: the same refusals as MemoryStore, one database
per customer, a connection string from the keychain only and never in an error."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from gecko.anyquery.mongo import MONGO_REF, MongoStore, mongo_store_from_keychain
from gecko.anyquery.store import (
    DuplicateChargeError,
    DuplicateFactError,
    DuplicateRunError,
    MemoryStore,
    Store,
    StoreError,
    StoreUnavailableError,
)
from gecko.anyquery.store_mongo import COLLECTIONS, db_name_for
from gecko.anyquery.types import (
    FactRow,
    InputBinding,
    LedgerEntry,
    PlannedField,
    PlannedSource,
    PriorValue,
    QueryPlan,
    Quote,
    RunRecord,
    Unplanned,
)
from gecko.credentials import ChainResolver, KeyringBackend
from tests.anyquery_fake_mongo import FakeMongoClient, FakeUnavailableError

NOW = "2026-10-08T12:00:00Z"
SECRET_URI = "mongodb+srv://user:hunter2@cluster.example.net/"


def _plan(customer: str = "acct-a") -> QueryPlan:
    source = PlannedSource(
        rank=0,
        candidate_id="orthogonal:crustdata:POST:/company/identify",
        catalog="orthogonal",
        api="crustdata",
        path="/company/identify",
        price_cents=3,
        field_path="data[].name",
        inputs=(InputBinding("domains", "subject"), InputBinding("x", "literal", "y")),
        storage="allowed",
        price_from="live",
    )
    return QueryPlan(
        plan_id="plan-1",
        customer=customer,
        query="who are these",
        subjects=("acme.com",),
        fields=(PlannedField("name", "legal name", "text", (source,)),),
        unplanned=(Unplanned("funding", "no catalog endpoint declares it"),),
        planner="fake",
        planner_model=None,
        catalog_sha="sha",
        created_at=NOW,
    )


def _quote(customer: str = "acct-a", quote_id: str = "q1") -> Quote:
    return Quote(
        quote_id, "plan-1", customer, 3, 30_000, 0, 0, 30_000, 1, "live", NOW, NOW
    )


def _run(run_id: str, quote_id: str = "q1", customer: str = "acct-a") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        customer=customer,
        plan_id="plan-1",
        quote_id=quote_id,
        mode="recorded",
        status="running",
        payment_status="not_due",
        started_at=NOW,
        finished_at=None,
        upstream_quoted_cents=3,
        upstream_charged_cents=0,
        calls=0,
        skipped_calls=0,
        counts={"fresh": 0, "repaired": 0, "stale": 0, "no data": 0},
        stop_code=None,
        notices=("one", "two"),
    )


def _fact(run_id: str, subject: str = "acme.com", **kw: Any) -> FactRow:
    base: dict[str, Any] = {
        "customer": "acct-a",
        "plan_id": "plan-1",
        "run_id": run_id,
        "mode": "recorded",
        "subject": subject,
        "field": "name",
        "value": "Acme",
        "source": "orthogonal:crustdata:POST:/company/identify",
        "fetched_at": NOW,
        "status": "fresh",
        "previous": None,
    }
    return FactRow(**{**base, **kw})


def _entry(entry_id: str, **kw: Any) -> LedgerEntry:
    base: dict[str, Any] = {
        "entry_id": entry_id,
        "customer": "acct-a",
        "kind": "charge",
        "run_id": "r1",
        "quote_id": "q1",
        "amount_usdc_micro": 100,
        "status": "pending",
        "pay_with": "USDC",
        "signature": None,
        "slot": None,
        "send_label": "fake",
        "rebate_requested": False,
        "rebate_lamports": None,
        "split_user_pct": None,
        "sol_usdc_rate_micro": None,
        "created_at": NOW,
        "note": "",
    }
    return LedgerEntry(**{**base, **kw})


def _memory() -> Store:
    return MemoryStore()


def _mongo() -> Store:
    return MongoStore(FakeMongoClient())


@pytest.fixture(params=[_memory, _mongo], ids=["memory", "mongo"])
def store(request: pytest.FixtureRequest) -> Store:
    make: Callable[[], Store] = request.param
    return make()


# -- parity: the two stores refuse and answer the same -------------------------------


def test_plans_and_quotes_round_trip_with_nested_shapes(store: Store) -> None:
    plan, quote = _plan(), _quote()
    store.save_plan(plan)
    store.save_quote(quote)
    assert store.plan("acct-a", "plan-1") == plan
    assert store.quote("acct-a", "q1") == quote
    assert store.plan("acct-b", "plan-1") is None


def test_plan_for_finds_the_latest_plan_of_the_same_question(store: Store) -> None:
    old = replace(_plan(), plan_id="plan-old", subjects=("acme.com", "globex.com"))
    new = replace(old, plan_id="plan-new", subjects=("globex.com", "acme.com"),
                  created_at="2026-10-11T00:00:00Z")  # fmt: skip
    other = replace(_plan(), plan_id="plan-other", query="something else")
    for plan in (old, new, other):
        store.save_plan(plan)
    found = store.plan_for("acct-a", "who are these", ["acme.com", "globex.com"])
    assert found is not None and found.plan_id == "plan-new"
    assert store.plan_for("acct-a", "who are these", []) == found
    assert store.plan_for("acct-a", "who are these", ["acme.com"]) is None
    assert store.plan_for("acct-b", "who are these", ["acme.com", "globex.com"]) is None


def test_one_run_per_quote_and_per_id(store: Store) -> None:
    store.start_run(_run("r1"))
    with pytest.raises(DuplicateRunError):
        store.start_run(_run("r1", quote_id="q2"))
    with pytest.raises(DuplicateRunError, match="quote q1"):
        store.start_run(_run("r2"))
    assert store.run("acct-a", "r1") == _run("r1")


def test_finish_run_needs_a_started_run_with_the_same_quote(store: Store) -> None:
    with pytest.raises(StoreError):
        store.finish_run(_run("r1"))
    store.start_run(_run("r1"))
    with pytest.raises(StoreError):
        store.finish_run(_run("r1", quote_id="q9"))
    done = replace(_run("r1"), status="complete", payment_status="landed")
    store.finish_run(done)
    assert store.run("acct-a", "r1") == done


def test_runs_newest_first_filtered_and_limited(store: Store) -> None:
    for i, run_id in enumerate(["20261008-1", "20261009-1", "20261007-1"]):
        store.start_run(_run(run_id, quote_id=f"q{i}"))
    assert [r.run_id for r in store.runs("acct-a")] == [
        "20261009-1",
        "20261008-1",
        "20261007-1",
    ]
    assert len(store.runs("acct-a", limit=1)) == 1
    assert store.runs("acct-a", limit=0) == []
    assert store.runs("acct-a", plan_id="other") == []


def test_a_refused_fact_batch_writes_nothing(store: Store) -> None:
    store.write_facts("acct-a", [_fact("r1")])
    with pytest.raises(DuplicateFactError):
        store.write_facts("acct-a", [_fact("r1", subject="b.com"), _fact("r1")])
    with pytest.raises(DuplicateFactError):
        store.write_facts("acct-a", [_fact("r2"), _fact("r2")])
    assert [f.subject for f in store.facts("acct-a", run_id="r1")] == ["acme.com"]
    assert store.facts("acct-a", run_id="r2") == []
    with pytest.raises(StoreError):
        store.write_facts("acct-b", [_fact("r3")])


def test_facts_filters_plan_facts_and_last_good(store: Store) -> None:
    prior = PriorValue("r1", "Acme", "fresh")
    store.write_facts(
        "acct-a",
        [
            _fact("r1"),
            _fact("r1", subject="b.com", value=None, status="no data", source=""),
            _fact("r2", value=None, status="repaired", previous=prior),
            _fact("r2", subject="b.com", status="stale", stale_since="r2"),
        ],
    )
    assert store.facts("acct-a", run_id="r2", subject="acme.com")[0].previous == prior
    assert len(store.facts("acct-a", run_id="r1", field="name")) == 2
    rows = store.plan_facts("acct-a", "plan-1")
    assert [(r["run_id"], r["subject"]) for r in rows] == [
        ("r1", "acme.com"),
        ("r1", "b.com"),
        ("r2", "acme.com"),
        ("r2", "b.com"),
    ]
    assert rows[2]["previous"] == {"run_id": "r1", "value": "Acme", "status": "fresh"}
    # a good status with no stored value still counts: the source answered
    assert store.last_good("acct-a", "plan-1") == {
        ("acme.com", "name"): PriorValue("r2", None, "repaired")
    }


def test_calls_are_kept_in_order_and_copied(store: Store) -> None:
    rows = [{"run_id": "r1", "api": "a", "n": 1}, {"run_id": "r2", "api": "b", "n": 2}]
    store.append_calls("acct-a", rows)
    assert store.calls("acct-a") == rows
    assert store.calls("acct-a", run_id="r2") == [rows[1]]
    assert "_id" not in rows[0]


def test_ledger_one_charge_per_run_and_one_entry_per_signature(store: Store) -> None:
    store.append_ledger(_entry("e1"))
    store.append_ledger(_entry("e2", kind="discount"))
    store.append_ledger(_entry("e3", kind="rebate_credit", run_id=None))
    store.append_ledger(_entry("e4", kind="rebate_credit", run_id=None))
    with pytest.raises(DuplicateChargeError):
        store.append_ledger(_entry("e5"))
    with pytest.raises(StoreError):
        store.append_ledger(_entry("e1", run_id="r9"))
    with pytest.raises(StoreError):
        store.append_ledger(_entry("e6", run_id="r9", amount_usdc_micro=-1))
    store.append_ledger(_entry("e7", run_id="r7", signature="sig"))
    with pytest.raises(DuplicateChargeError):
        store.append_ledger(_entry("e8", run_id="r8", signature="sig"))


def test_ledger_settles_once_from_pending(store: Store) -> None:
    store.append_ledger(_entry("e1"))
    store.append_ledger(_entry("e2", run_id="r2", signature="taken"))
    with pytest.raises(DuplicateChargeError):
        store.update_ledger(_entry("e1", status="landed", signature="taken"))
    store.update_ledger(_entry("e1", status="landed", signature="s1", slot=7))
    settled = [e for e in store.ledger("acct-a") if e.entry_id == "e1"][0]
    assert (settled.status, settled.signature, settled.slot) == ("landed", "s1", 7)
    with pytest.raises(StoreError):
        store.update_ledger(_entry("e1", status="failed"))
    with pytest.raises(StoreError):
        store.update_ledger(_entry("nope", status="landed"))


def test_ledger_one_run_credit_per_run(store: Store) -> None:
    store.append_ledger(_entry("e1", status="landed", signature="s1"))
    store.append_ledger(_entry("c1", kind="run_credit", status="landed"))
    with pytest.raises(DuplicateChargeError):
        store.append_ledger(_entry("c2", kind="run_credit", status="landed"))
    store.append_ledger(_entry("c3", kind="run_credit", run_id="r2", status="landed"))


def test_a_pending_entry_records_its_signature_and_never_swaps_it(
    store: Store,
) -> None:
    store.append_ledger(_entry("e1"))
    with pytest.raises(StoreError):
        store.update_ledger(_entry("e1", status="pending"))  # nothing to record
    store.update_ledger(_entry("e1", status="pending", signature="s1"))
    held = [e for e in store.ledger("acct-a") if e.entry_id == "e1"][0]
    assert (held.status, held.signature) == ("pending", "s1")
    with pytest.raises(StoreError):
        store.update_ledger(_entry("e1", status="landed", signature="s2"))
    store.update_ledger(_entry("e1", status="landed", signature="s1", slot=9))
    settled = [e for e in store.ledger("acct-a") if e.entry_id == "e1"][0]
    assert (settled.status, settled.signature, settled.slot) == ("landed", "s1", 9)


def test_one_open_settlement_per_run_and_a_failed_one_may_be_retried(
    store: Store,
) -> None:
    """Option B, 9 Oct: a settlement may be retried after it fails, but two settlers of
    one charge can never both hold an open (pending) attempt."""
    store.append_ledger(_entry("c1", status="landed", signature="pay", pay_with="SOL"))
    first = _entry("s1", kind="settlement", pay_with="SOL", amount_lamports=135_000)
    store.append_ledger(first)
    with pytest.raises(DuplicateChargeError, match="already open"):
        store.append_ledger(_entry("s2", kind="settlement", pay_with="SOL"))
    store.update_ledger(_entry("s1", kind="settlement", status="failed"))
    store.append_ledger(_entry("s3", kind="settlement", pay_with="SOL"))
    held = {e.entry_id: e for e in store.ledger("acct-a")}
    assert held["s1"].status == "failed" and held["s3"].status == "pending"
    # The lamports a SOL entry moved round-trip; an older entry without them reads None.
    assert held["s1"].amount_lamports == 135_000
    assert held["c1"].amount_lamports is None


def test_ledger_newest_first(store: Store) -> None:
    store.append_ledger(_entry("a", created_at="2026-10-08T10:00:00Z"))
    store.append_ledger(_entry("b", run_id="r2", created_at="2026-10-08T11:00:00Z"))
    assert [e.entry_id for e in store.ledger("acct-a")] == ["b", "a"]
    assert [e.entry_id for e in store.ledger("acct-a", limit=1)] == ["b"]


def test_customers_never_see_each_other_and_delete_is_whole(store: Store) -> None:
    store.save_plan(_plan("acct-a"))
    store.save_plan(_plan("acct-b"))
    store.start_run(_run("r1", customer="acct-b"))
    store.delete_customer("acct-b")
    assert store.plan("acct-b", "plan-1") is None
    assert store.run("acct-b", "r1") is None
    assert store.plan("acct-a", "plan-1") == _plan("acct-a")
    with pytest.raises(StoreError):
        store.plan("", "plan-1")
    with pytest.raises(StoreError):
        store.delete_customer(" ")


# -- Mongo only -------------------------------------------------------------------------


def test_one_database_per_customer_with_every_index() -> None:
    client = FakeMongoClient()
    store = MongoStore(client)
    assert store.plan("acct-a", "p") is None
    assert client.database_names() == set()  # a read creates nothing
    store.save_plan(_plan("acct-a"))
    store.save_plan(_plan("acct-b"))
    names = {db_name_for("acct-a"), db_name_for("acct-b")}
    assert client.database_names() == names
    db = client[db_name_for("acct-a")]
    for spec in COLLECTIONS:
        assert {i.name for i in spec.indexes} <= set(db[spec.name].indexes)
    store.delete_customer("acct-a")
    assert client.dropped == [db_name_for("acct-a")]
    assert client.database_names() == {db_name_for("acct-b")}


def test_indexes_are_ensured_once_per_database_per_process() -> None:
    client = FakeMongoClient()
    store = MongoStore(client)
    store.save_plan(_plan())
    before = client.client_ops
    store.save_plan(_plan())
    assert client.client_ops - before == 1  # the write, no index calls


def test_an_unreachable_store_fails_closed_without_the_uri() -> None:
    client = FakeMongoClient(uri=SECRET_URI)
    store = MongoStore(client, unavailable=(FakeUnavailableError,))
    store.save_plan(_plan())
    client.down = True
    for action in (
        lambda: store.plan("acct-a", "plan-1"),
        lambda: store.save_plan(_plan()),
        lambda: store.start_run(_run("r1")),
        lambda: store.write_facts("acct-a", [_fact("r1")]),
        lambda: store.append_ledger(_entry("e1")),
        lambda: store.delete_customer("acct-a"),
    ):
        with pytest.raises(StoreUnavailableError) as caught:
            action()
        assert "hunter2" not in str(caught.value)
        assert caught.value.__cause__ is None


class _Keyring:
    def __init__(self, secrets: dict[tuple[str, str], str]) -> None:
        self.secrets = secrets

    def get_password(self, service: str, username: str) -> str | None:
        return self.secrets.get((service, username))

    def get_keyring(self) -> object:
        return object()


#: The keychain entry ``gecko auth set mongodb`` writes: service ``gecko:<slot>``.
_SLOT = {(f"gecko:{MONGO_REF.slot()}", "gecko"): SECRET_URI}


def _resolver(secrets: dict[tuple[str, str], str]) -> ChainResolver:
    backend = KeyringBackend(module=_Keyring(secrets))
    return ChainResolver([backend])


def test_the_connection_string_comes_from_the_keychain_slot(monkeypatch: Any) -> None:
    monkeypatch.setenv("GECKO_CRED_MONGODB_ANYQUERY", "mongodb://from-env/")
    monkeypatch.setenv("MONGODB_URI", "mongodb://from-env/")
    seen: list[str] = []
    client = FakeMongoClient()

    def connect(uri: str) -> FakeMongoClient:
        seen.append(uri)
        return client

    store = mongo_store_from_keychain(resolver=_resolver(_SLOT), connect=connect)
    assert seen == [SECRET_URI]
    assert store.client is client


def test_no_keychain_entry_is_refused_and_env_is_never_read(monkeypatch: Any) -> None:
    monkeypatch.setenv("GECKO_CRED_MONGODB_ANYQUERY", SECRET_URI)
    monkeypatch.setenv("MONGODB_URI", SECRET_URI)
    with pytest.raises(StoreUnavailableError) as caught:
        mongo_store_from_keychain(
            resolver=_resolver({}), connect=lambda uri: pytest.fail("connected")
        )
    assert "MONGODB_URI" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_a_failing_connect_never_echoes_the_uri() -> None:
    def connect(uri: str) -> Any:
        raise ValueError(f"bad uri {uri}")

    with pytest.raises(StoreUnavailableError) as caught:
        mongo_store_from_keychain(resolver=_resolver(_SLOT), connect=connect)
    assert "hunter2" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_pymongo_duplicate_key_errors_map_like_the_fake() -> None:
    errors = pytest.importorskip("pymongo.errors")
    from gecko.anyquery.mongo import _duplicate_index

    exc = errors.DuplicateKeyError(
        "E11000 duplicate key error collection: gq_x.runs index: one_run_per_quote",
        11000,
        {
            "errmsg": "E11000 duplicate key error collection: gq_x.runs index: "
            'one_run_per_quote dup key: { quote_id: "q1" }'
        },
    )
    assert _duplicate_index(exc) == "one_run_per_quote"
    assert _duplicate_index(errors.PyMongoError("down")) is None
