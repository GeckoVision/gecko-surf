"""Planning a storefront change: the cap, the ordering, and refusing to be quiet.

The acceptance case that matters is the last one. A plan that deletes hands someone a
sequence whose partial execution has no single-instruction recovery, and the tool's job is
to say so — a silent delete-first plan is the footgun this whole module exists to avoid.
"""

from __future__ import annotations

import base64

import pytest

from gecko.providers.let_me_buy import plan_store_change, plan_store_change_result
from gecko.showcase import showcase_items
from gecko.store_directory import StoreListing, StoreProduct, encode_store

STORE = "geckocoffee"
AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LIVE4 = (
    ("Espresso", 100_000),
    ("Sparkling water", 50_000),
    ("Cappuccino", 150_000),
    ("Mochaccino", 200_000),
)


def _fake_rpc(products: tuple[tuple[str, int], ...]):
    """A node serving one store — a light fake, not a mock of the whole RPC surface."""
    listing = StoreListing(
        address="unused",
        store_name=STORE,
        authority=AUTHORITY,
        total_purchases=21,
        products=tuple(
            StoreProduct(name=n, price_raw=p, decimals=6, mint=USDC)
            for n, p in products
        ),
    )
    data = base64.b64encode(encode_store(listing)).decode()

    def call(url: str, method: str, params: list) -> dict:
        assert method == "getAccountInfo"
        return {"result": {"value": {"data": [data, "base64"]}}}

    return call


def test_the_showcase_on_the_live_four_is_add_only() -> None:
    """The acceptance case the founder actually signed: 16 adds, no deletes, 20/20."""
    target = LIVE4 + tuple((i.name, i.price_lamports) for i in showcase_items())
    plan = plan_store_change(STORE, target, rpc_url="x", rpc_call=_fake_rpc(LIVE4))
    assert plan.refused is None
    assert plan.verdict.fits and plan.verdict.mode == "add-only"
    assert plan.verdict.cap == "20/20"
    assert not plan.verdict.stranding_risk
    assert len(plan.steps) == 16
    assert {s.instruction for s in plan.steps} == {"add_product"}


def test_a_target_over_the_cap_is_refused_and_names_it() -> None:
    """21 products refuses locally, before anything reaches a chain."""
    target = LIVE4 + tuple((f"Extra{n:02d}", 1_000) for n in range(17))
    plan = plan_store_change(STORE, target, rpc_url="x", rpc_call=_fake_rpc(LIVE4))
    assert plan.refused is not None
    assert "VectorLimitReached (6008)" in plan.refused
    assert "COUNT, not a byte budget" in plan.refused
    assert plan.verdict.cap == "21/20"
    assert not plan.verdict.fits
    assert plan.steps == ()  # a refused plan hands back no sequence to run


def test_a_rename_deletes_before_adding_and_says_there_is_no_recovery() -> None:
    """THE acceptance case. A price change is delete-then-add, and partial execution
    leaves the store with neither the old product nor the new one."""
    target = (
        ("Espresso", 100_000),
        ("Sparkling water", 50_000),
        ("Cappuccino", 160_000),
        ("Mochaccino", 200_000),
    )
    plan = plan_store_change(
        STORE, target, rpc_url="x", exact=True, rpc_call=_fake_rpc(LIVE4)
    )
    assert plan.refused is None
    assert plan.verdict.mode == "requires-deletes"
    assert plan.verdict.stranding_risk is True
    assert any("NO SINGLE INSTRUCTION restores it" in n for n in plan.verdict.notes)
    # The delete must precede its add, or the add reverts with ProductAlreadyExists.
    order = [(s.instruction, s.product) for s in plan.steps]
    assert order == [("delete_product", "Cappuccino"), ("add_product", "Cappuccino")]


def test_add_only_never_deletes_even_when_the_store_has_extras() -> None:
    """The default cannot produce a delete. That is the safety property, not a preference."""
    plan = plan_store_change(
        STORE, (("Latte", 168_000),), rpc_url="x", rpc_call=_fake_rpc(LIVE4)
    )
    assert plan.verdict.mode == "add-only"
    assert not plan.verdict.stranding_risk
    assert all(s.instruction == "add_product" for s in plan.steps)
    assert any("are left in place" in n for n in plan.verdict.notes)


def test_add_only_reports_a_price_difference_rather_than_silently_fixing_it() -> None:
    """Silently leaving a price wrong is as bad as silently deleting to fix it."""
    plan = plan_store_change(
        STORE, (("Cappuccino", 160_000),), rpc_url="x", rpc_call=_fake_rpc(LIVE4)
    )
    assert plan.steps == ()
    assert any("different price" in n and "exact=True" in n for n in plan.verdict.notes)


def test_names_are_byte_exact_so_a_case_variant_is_an_addition() -> None:
    """MEASURED: 'Sparkling Water' and 'Sparkling water' coexist as two products."""
    plan = plan_store_change(
        STORE, (("Sparkling Water", 80_000),), rpc_url="x", rpc_call=_fake_rpc(LIVE4)
    )
    assert [s.product for s in plan.steps] == ["Sparkling Water"]


def test_a_store_already_matching_plans_nothing() -> None:
    plan = plan_store_change(STORE, LIVE4, rpc_url="x", rpc_call=_fake_rpc(LIVE4))
    assert plan.verdict.mode == "no-change"
    assert plan.steps == ()


def test_the_result_mapping_carries_the_verdict_for_a_tool_surface() -> None:
    target = LIVE4 + tuple((i.name, i.price_lamports) for i in showcase_items())
    result = plan_store_change_result(
        plan_store_change(STORE, target, rpc_url="x", rpc_call=_fake_rpc(LIVE4))
    )
    assert result["refused"] is False
    assert result["verdict"]["mode"] == "add-only"
    assert result["verdict"]["cap"] == "20/20"
    assert len(result["steps"]) == 16


def test_a_missing_store_raises_rather_than_planning_against_nothing() -> None:
    def empty(url: str, method: str, params: list) -> dict:
        return {"result": {"value": None}}

    with pytest.raises(LookupError, match="no let_me_buy store account"):
        plan_store_change(STORE, LIVE4, rpc_url="x", rpc_call=empty)
