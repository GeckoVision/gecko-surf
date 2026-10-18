"""The any-query contract: config refusals, MemoryStore's unique keys and tenancy, the
Mongo layout as data, the billing maths and FakeBilling, the tool-shape guard."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest

from gecko.anyquery.charge import (
    BillingError,
    QuoteOverCapError,
    available_discount,
    build_quote,
    iso_plus,
    rebate_credit_micro,
)
from gecko.anyquery.charge_fake import FakeBilling
from gecko.anyquery.config import AnyQueryConfig, ConfigError, is_pubkey, load_config
from gecko.anyquery.planner import FakePlanner, PlanRejectedError
from gecko.anyquery.run import RUN_ID_RE, new_run_id
from gecko.anyquery.store import (
    DuplicateChargeError,
    DuplicateFactError,
    DuplicateRunError,
    MemoryStore,
    StoreError,
)
from gecko.anyquery.store_mongo import COLLECTIONS, db_name_for, ensure_indexes
from gecko.anyquery.surface import ANYQUERY_TOOLS, FORBIDDEN_ARGUMENTS
from gecko.anyquery.views import quote_view
from gecko.anyquery.types import (
    CandidateSet,
    InputBinding,
    FactRow,
    LedgerEntry,
    Payment,
    PlanDraft,
    PlannedField,
    PlannedSource,
    PlanRequest,
    QueryPlan,
    RunRecord,
)
from gecko.orthogonal.supplier.history import changes_since

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SYSTEM = "11111111111111111111111111111111"
NOW = "2026-10-08T12:00:00Z"


def _source(rank: int, cents: int, price_from: str = "pinned") -> PlannedSource:
    return PlannedSource(
        rank=rank,
        candidate_id=f"orthogonal:api{rank}:GET:/x",
        catalog="orthogonal",
        api=f"api{rank}",
        path="/x",
        price_cents=cents,
        field_path="data.name",
        inputs=(),
        storage="allowed",
        price_from=price_from,  # type: ignore[arg-type]
    )


def _plan(customer: str = "acct-a", *sources: PlannedSource) -> QueryPlan:
    field = PlannedField("funding", "last round", "text", sources or (_source(0, 3),))
    return QueryPlan(
        plan_id="plan-1",
        customer=customer,
        query="funding for these",
        subjects=("acme", "globex"),
        fields=(field,),
        unplanned=(),
        planner="fake",
        planner_model=None,
        catalog_sha="sha",
        created_at=NOW,
    )


def _run(run_id: str = "20261008-120000-aaaaaa", quote_id: str = "q1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        customer="acct-a",
        plan_id="plan-1",
        quote_id=quote_id,
        mode="recorded",
        status="running",
        payment_status="not_due",
        started_at=NOW,
        finished_at=None,
        upstream_quoted_cents=16,
        upstream_charged_cents=0,
        calls=0,
        skipped_calls=0,
        counts={},
        stop_code=None,
        notices=(),
    )


def _fact(run_id: str, value: str | None, status: str = "fresh", **kw: Any) -> FactRow:
    base = FactRow(
        customer="acct-a",
        plan_id="plan-1",
        run_id=run_id,
        mode="recorded",
        subject="acme",
        field="funding",
        value=value,
        source="orthogonal:api0:GET:/x",
        fetched_at=NOW,
        status=status,  # type: ignore[arg-type]
        previous=None,
    )
    return replace(base, **kw)


def _entry(entry_id: str, **kw: Any) -> LedgerEntry:
    base = LedgerEntry(
        entry_id=entry_id,
        customer="acct-a",
        kind="charge",
        run_id="r1",
        quote_id="q1",
        amount_usdc_micro=100,
        status="pending",
        pay_with="USDC",
        signature=None,
        slot=None,
        send_label="fake",
        rebate_requested=False,
        rebate_lamports=None,
        split_user_pct=None,
        sol_usdc_rate_micro=None,
        created_at=NOW,
        note="",
    )
    return replace(base, **kw)


def _payment(amount: int, discount: int = 0, pay_with: str = "USDC") -> Payment:
    return Payment(
        customer="acct-a",
        run_id="r1",
        quote_id="q1",
        amount_usdc_micro=amount,
        discount_usdc_micro=discount,
        pay_with=pay_with,  # type: ignore[arg-type]
        payer=SYSTEM,
        payee_usdc_ata=USDC_MINT,
    )


# -- config ---------------------------------------------------------------------------


def test_config_defaults_to_fake_billing_with_wallet_creation_off() -> None:
    config = load_config({})
    assert config.billing == "fake"
    assert config.wallet_create_enabled is False


@pytest.mark.parametrize(
    "env",
    [
        {"GECKO_ANYQUERY_BILLING": "solana", "GECKO_REBATE_SPLIT_USER_PCT": "50"},
        {"GECKO_ANYQUERY_BILLING": "solana", "GECKO_FEE_WALLET": USDC_MINT},
        {
            "GECKO_ANYQUERY_BILLING": "solana",
            "GECKO_FEE_WALLET": USDC_MINT,
            "GECKO_REBATE_SPLIT_USER_PCT": "50",
        },
        {"GECKO_FEE_WALLET": "not-a-key-0OIl"},
        {"GECKO_RELAY_FEE_PAYER": "not-a-key-0OIl"},
        {"GECKO_RELAY_SWEEP_ABOVE_LAMPORTS": "0"},
        {
            "GECKO_RELAY_LOW_BELOW_LAMPORTS": "200000000",
            "GECKO_RELAY_SWEEP_ABOVE_LAMPORTS": "100000000",
        },
        {"GECKO_REBATE_SPLIT_USER_PCT": "101"},
        {"GECKO_ANYQUERY_BILLING": "stripe"},
    ],
)
def test_config_refuses_an_incomplete_or_invalid_solana_setup(
    env: dict[str, str],
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(env)
    assert "not-a-key" not in str(caught.value)


def test_config_loads_a_complete_solana_setup() -> None:
    config = load_config(
        {
            "GECKO_ANYQUERY_BILLING": "solana",
            "GECKO_FEE_WALLET": USDC_MINT,
            "GECKO_RELAY_FEE_PAYER": SYSTEM,
            "GECKO_REBATE_SPLIT_USER_PCT": "50",
            "GECKO_ANYQUERY_WALLET_CREATE": "true",
        }
    )
    assert (config.fee_wallet, config.rebate_split_user_pct) == (USDC_MINT, 50)
    assert config.relay_fee_payer == SYSTEM
    assert config.wallet_create_enabled is True


def test_gecko_fee_defaults_to_a_flat_ten_cents_per_run() -> None:
    """A flat $0.10 per run on top of the data."""
    config = load_config({})
    assert (config.gecko_fee_floor_usdc_micro, config.gecko_margin_pct) == (100_000, 0)
    tuned = load_config(
        {
            "GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO": "50000",
            "GECKO_ANYQUERY_MARGIN_PCT": "15",
        }
    )
    assert (tuned.gecko_fee_floor_usdc_micro, tuned.gecko_margin_pct) == (50_000, 15)


@pytest.mark.parametrize(
    "env",
    [
        {"GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO": "-1"},
        {"GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO": "5000001"},
        {"GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO": "ten cents"},
        {"GECKO_ANYQUERY_MARGIN_PCT": "-1"},
        {"GECKO_ANYQUERY_MARGIN_PCT": "101"},
    ],
)
def test_gecko_fee_settings_out_of_range_are_refused(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(env)
    assert next(iter(env)) in str(caught.value)


def test_quote_charges_the_flat_fee_on_top_of_the_data() -> None:
    plan = _plan("acct-a", _source(0, 3, "live"))
    quote = build_quote(plan, load_config({}), available=0, quote_id="q", now=NOW)
    assert quote.upstream_max_usdc_micro == 60_000  # 2 subjects x 3 cents
    assert quote.gecko_fee_usdc_micro == 100_000
    assert quote.total_usdc_micro == 160_000


def test_quote_view_shows_the_fee_apart_from_the_data() -> None:
    plan = _plan("acct-a", _source(0, 3, "live"))
    quote = build_quote(plan, load_config({}), available=0, quote_id="q", now=NOW)
    text = quote_view(quote)["price_text"]
    assert "0.060000 USDC for the data" in text
    assert "0.100000 USDC Gecko fee" in text
    assert "0.160000 USDC" in text


def test_relay_balance_reminders_default_to_the_founders_numbers() -> None:
    """Low below 0.01 SOL, sweep above 0.1 SOL."""
    config = load_config({})
    assert config.relay_low_below_lamports == 10_000_000
    assert config.relay_sweep_above_lamports == 100_000_000
    tuned = load_config({"GECKO_RELAY_SWEEP_ABOVE_LAMPORTS": "500000000"})
    assert tuned.relay_sweep_above_lamports == 500_000_000


def test_is_pubkey_checks_32_bytes_not_just_the_alphabet() -> None:
    assert is_pubkey(SYSTEM) and is_pubkey(USDC_MINT)
    assert not is_pubkey("1" * 31)
    assert not is_pubkey("z" * 44)  # base58 alphabet, but decodes past 32 bytes


# -- store ----------------------------------------------------------------------------


def test_store_never_answers_one_customer_with_anothers_data() -> None:
    store = MemoryStore()
    store.save_plan(_plan("acct-a"))
    assert store.plan("acct-a", "plan-1") is not None
    assert store.plan("acct-b", "plan-1") is None
    store.delete_customer("acct-a")
    assert store.plan("acct-a", "plan-1") is None


def test_one_run_per_quote() -> None:
    store = MemoryStore()
    store.start_run(_run())
    with pytest.raises(DuplicateRunError):
        store.start_run(_run(run_id="20261008-120001-bbbbbb"))
    with pytest.raises(StoreError):
        store.finish_run(_run(run_id="20261008-120002-cccccc", quote_id="q9"))
    store.finish_run(replace(_run(), status="complete"))
    assert [r.status for r in store.runs("acct-a")] == ["complete"]


def test_write_facts_is_all_or_nothing_and_unique_per_cell() -> None:
    store = MemoryStore()
    run = "20261008-120000-aaaaaa"
    with pytest.raises(DuplicateFactError):
        store.write_facts("acct-a", [_fact(run, "A"), _fact(run, "B")])
    assert store.facts("acct-a", run_id=run) == []
    with pytest.raises(StoreError):
        store.write_facts("acct-b", [_fact(run, "A")])


def test_plan_facts_feed_history_changes_since() -> None:
    store = MemoryStore()
    first, second = "20261008-120000-aaaaaa", "20261015-120000-bbbbbb"
    store.write_facts("acct-a", [_fact(first, "Seed $2M")])
    store.write_facts("acct-a", [_fact(second, "Series A $10M")])
    changes = changes_since(
        store.plan_facts("acct-a", "plan-1"), first, subject_key="subject"
    )
    assert changes.against_run == second
    assert [(c.change, c.what) for c in changes.changes] == [("changed", ("value",))]


def test_last_good_keeps_the_latest_good_status_even_without_a_stored_value() -> None:
    store = MemoryStore()
    store.write_facts("acct-a", [_fact("20261008-120000-aaaaaa", "old")])
    store.write_facts("acct-a", [_fact("20261009-120000-bbbbbb", None, "repaired")])
    store.write_facts("acct-a", [_fact("20261010-120000-cccccc", None, "stale")])
    prior = store.last_good("acct-a", "plan-1")[("acme", "funding")]
    assert (prior.run_id, prior.value, prior.status) == (
        "20261009-120000-bbbbbb",
        None,
        "repaired",
    )


def test_ledger_refuses_a_second_charge_and_a_reused_signature() -> None:
    store = MemoryStore()
    store.append_ledger(_entry("e1", signature="sig1"))
    with pytest.raises(DuplicateChargeError):
        store.append_ledger(_entry("e2"))
    with pytest.raises(DuplicateChargeError):
        store.append_ledger(_entry("e3", kind="rebate_credit", signature="sig1"))
    store.append_ledger(_entry("e4", kind="rebate_credit", status="landed"))


def test_ledger_entries_only_settle_from_pending() -> None:
    store = MemoryStore()
    store.append_ledger(_entry("e1"))
    store.update_ledger(
        _entry("e1", status="landed", signature="s", amount_usdc_micro=1)
    )
    settled = store.ledger("acct-a")[0]
    assert (settled.status, settled.signature, settled.amount_usdc_micro) == (
        "landed",
        "s",
        100,
    )
    with pytest.raises(StoreError):
        store.update_ledger(_entry("e1", status="failed"))


# -- mongo layout ---------------------------------------------------------------------


def test_db_name_hides_the_account_and_is_stable() -> None:
    name = db_name_for("acct-a")
    assert name == db_name_for("acct-a") != db_name_for("acct-b")
    assert name.startswith("gq_") and len(name) == 27 and "acct" not in name


class _FakeCollection:
    def __init__(self) -> None:
        self.indexes: dict[str, tuple[list[Any], dict[str, Any]]] = {}

    def create_index(self, keys: list[Any], **options: Any) -> str:
        self.indexes[options["name"]] = (keys, options)
        return str(options["name"])


def test_ensure_indexes_is_idempotent_and_never_sparse_on_a_nullable_signature() -> (
    None
):
    db: dict[str, _FakeCollection] = {}

    class _Db:
        def __getitem__(self, name: str) -> _FakeCollection:
            return db.setdefault(name, _FakeCollection())

    ensure_indexes(_Db())
    ensure_indexes(_Db())
    assert set(db) == {c.name for c in COLLECTIONS}
    assert len(db) == 9  # + datasets, dataset_rows (DATASET-DESIGN.md section 2)
    _keys, sig = db["payments"].indexes["one_entry_per_signature"]
    assert sig["unique"] and "sparse" not in sig
    assert sig["partialFilterExpression"] == {"signature": {"$type": "string"}}
    assert db["runs"].indexes["one_run_per_quote"][1]["unique"]


# -- billing --------------------------------------------------------------------------


def test_quote_is_worst_case_failover_plus_fee_minus_discount() -> None:
    plan = _plan("acct-a", _source(0, 3, "live"), _source(1, 5))
    config = AnyQueryConfig(gecko_margin_pct=10, gecko_fee_floor_usdc_micro=1_000)
    quote = build_quote(plan, config, available=6_000, quote_id="q", now=NOW)
    assert quote.upstream_max_cents == 16  # 2 subjects x (3 + 5)
    assert quote.upstream_max_usdc_micro == 160_000
    assert quote.gecko_fee_usdc_micro == 16_000
    assert (quote.discount_usdc_micro, quote.total_usdc_micro) == (6_000, 170_000)
    assert (quote.max_calls, quote.price_from) == (4, "mixed")
    assert quote.expires_at == "2026-10-08T12:15:00Z"


def _shared(rank: int, field_path: str, cents: int = 4) -> PlannedSource:
    return PlannedSource(
        rank=rank,
        candidate_id="orthogonal:scrapecreators:GET:/v1/tiktok/profile",
        catalog="orthogonal",
        api="scrapecreators",
        path="/v1/tiktok/profile",
        price_cents=cents,
        field_path=field_path,
        inputs=(InputBinding("handle", "subject"),),
        storage="allowed",
    )


def test_quote_prices_distinct_calls_not_fields() -> None:
    """Live, 9 Oct: 7 fields from 2 endpoint calls were quoted per field ($0.28).
    Six fields read one endpoint here; the run makes that call once per subject."""
    names = ("followers", "following", "likes", "videos", "bio", "verified")
    fields = tuple(
        PlannedField(name, name, "text", (_shared(0, f"data.{name}"),))
        for name in names
    )
    failover = replace(
        _source(1, 5), inputs=(InputBinding("q", "subject"),), field_path="data.n"
    )
    seventh = PlannedField("news", "news", "text", (_source(0, 3), failover))
    plan = replace(_plan(), fields=(*fields, seventh))
    quote = build_quote(
        plan,
        AnyQueryConfig(gecko_fee_floor_usdc_micro=0),
        available=0,
        quote_id="q",
        now=NOW,
    )
    # Per subject: one shared call (4) + news rank 0 (3) + its failover (5) = 12 cents.
    assert quote.upstream_max_cents == 2 * 12
    assert quote.max_calls == 2 * 3


def test_two_sources_on_one_endpoint_with_different_literals_are_two_calls() -> None:
    """Each priced at the endpoint's highest listing, as the run's spend guard counts."""
    us = (InputBinding("region", "literal", "us"),)
    eu = (InputBinding("region", "literal", "eu"),)
    a = replace(_shared(0, "data.a", cents=4), inputs=us)
    b = replace(_shared(1, "data.a", cents=6), inputs=eu)
    plan = _plan("acct-a", a, b)
    quote = build_quote(
        plan,
        AnyQueryConfig(gecko_fee_floor_usdc_micro=0),
        available=0,
        quote_id="q",
        now=NOW,
    )
    assert (quote.upstream_max_cents, quote.max_calls) == (2 * 12, 4)


def test_quote_over_the_run_ceiling_is_refused() -> None:
    with pytest.raises(QuoteOverCapError):
        build_quote(
            _plan("acct-a", _source(0, 300)),
            AnyQueryConfig(max_run_usdc_micro=1_000_000),
            available=0,
            quote_id="q",
            now=NOW,
        )


def test_discount_and_rebate_maths() -> None:
    entries = [
        _entry("a", kind="rebate_credit", amount_usdc_micro=500, status="landed"),
        _entry("b", kind="discount", amount_usdc_micro=200, status="landed"),
        _entry("c", kind="discount", amount_usdc_micro=999, status="failed"),
    ]
    assert available_discount(entries) == 300
    # 0.01 SOL rebate, user keeps 50%, 150 USDC/SOL -> 0.75 USDC.
    assert rebate_credit_micro(10_000_000, 50, 150_000_000) == 750_000
    with pytest.raises(BillingError):
        rebate_credit_micro(1, 101, 1)
    assert iso_plus("2026-10-08T23:59:30+00:00", 60) == "2026-10-09T00:00:30Z"


def test_preflight_names_why_a_wallet_cannot_pay() -> None:
    store = MemoryStore()
    no_fee = AnyQueryConfig(gecko_fee_floor_usdc_micro=0)
    billing = FakeBilling(no_fee, store, wallets={"acct-a": (SYSTEM, 100_000)})
    quote = billing.quote(_plan(), now=NOW)  # 60_000 micro
    assert billing.preflight("acct-a", quote, "USDC").ok
    assert billing.preflight("acct-b", quote, "USDC").reason.startswith("no wallet")
    poor = FakeBilling(no_fee, store, wallets={"acct-a": (SYSTEM, 10_000)})
    assert poor.preflight("acct-a", quote, "USDC").reason == "needs 0.06 USDC, has 0.01"


def test_capture_lands_once_and_records_the_discount_spent() -> None:
    store = MemoryStore()
    billing = FakeBilling(AnyQueryConfig(), store)
    entry = billing.capture(_payment(54_000, discount=6_000))
    assert (entry.status, entry.amount_usdc_micro) == ("landed", 54_000)
    kinds = sorted(e.kind for e in store.ledger("acct-a"))
    assert kinds == ["charge", "discount"]
    assert available_discount(store.ledger("acct-a")) == 0
    with pytest.raises(DuplicateChargeError):
        billing.capture(_payment(54_000))


def test_a_run_that_delivered_nothing_is_not_charged_and_keeps_its_discount() -> None:
    store = MemoryStore()
    entry = FakeBilling(AnyQueryConfig(), store).capture(_payment(0, discount=0))
    assert (entry.status, entry.amount_usdc_micro) == ("not_charged", 0)
    assert [e.kind for e in store.ledger("acct-a")] == ["charge"]


def test_a_failed_capture_is_recorded_failed_and_spends_no_discount() -> None:
    store = MemoryStore()
    billing = FakeBilling(AnyQueryConfig(), store, fail_capture=True)
    assert billing.capture(_payment(54_000, discount=6_000)).status == "failed"
    assert [e.kind for e in store.ledger("acct-a")] == ["charge"]


def test_a_sol_rebate_is_credited_once_and_discounts_the_next_quote() -> None:
    store = MemoryStore()
    config = AnyQueryConfig(rebate_split_user_pct=50)
    billing = FakeBilling(config, store, rebates={"r1": 10_000_000})
    # The user's transfer is final and asks no rebate; the settlement swap does.
    assert not billing.capture(_payment(54_000, pay_with="SOL")).rebate_requested
    [settled] = [e for e in store.ledger("acct-a") if e.kind == "settlement"]
    assert settled.rebate_requested and settled.status == "landed"
    credits = billing.observe_rebates("acct-a")
    assert [c.amount_usdc_micro for c in credits] == [750_000]
    assert billing.observe_rebates("acct-a") == []
    quote = billing.quote(_plan(), now=NOW)
    # 60_000 of data + the 100_000 flat fee, all covered by the 750_000 credit.
    assert (quote.discount_usdc_micro, quote.total_usdc_micro) == (160_000, 0)


# -- planner, run ids, tool shapes ---------------------------------------------------


def test_fake_planner_answers_fixtures_and_refuses_the_rest() -> None:
    draft = PlanDraft(subjects=("acme",), fields=(), unplanned=(), notes=())
    planner = FakePlanner({"funding for acme": draft})
    empty = CandidateSet((), "sha")
    assert planner.draft(PlanRequest("acct-a", " funding for acme "), empty) is draft
    with pytest.raises(PlanRejectedError):
        planner.draft(PlanRequest("acct-a", "something else"), empty)


def test_run_ids_sort_in_time_order() -> None:
    early = new_run_id(dt.datetime(2026, 10, 8, 9, 5, tzinfo=dt.UTC), "a1b2c3")
    late = new_run_id(dt.datetime(2026, 10, 8, 10, 0, tzinfo=dt.UTC), "000000")
    assert early == "20261008-090500-a1b2c3" and RUN_ID_RE.match(new_run_id())
    assert early < late
    with pytest.raises(ValueError):
        new_run_id(token="XYZ")


def test_no_tool_takes_a_customer_wallet_price_address_or_plan_body() -> None:
    assert [t["name"] for t in ANYQUERY_TOOLS] == [
        "plan_query",
        "run_query",
        "run_rows",
        "run_changes",
        "create_dataset",
        "refresh_dataset",
        "datasets",
        "save_bundle",
        "bundles",
        "payments",
    ]
    for tool in ANYQUERY_TOOLS:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert not set(schema["properties"]) & FORBIDDEN_ARGUMENTS, tool["name"]
