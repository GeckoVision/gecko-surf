"""The showcase selection: 20 slots, add-only, and names that are never retyped."""

from __future__ import annotations

import pytest

from gecko.semantic_catalogue import BY_ID
from gecko.showcase import (
    MAX_PRODUCTS,
    SHOWCASE,
    end_state,
    showcase_items,
    to_add,
)

#: geckocoffee as it stands on mainnet — 4 products, read 2026-08-21.
LIVE: tuple[tuple[str, int], ...] = (
    ("Espresso", 100_000),
    ("Sparkling water", 50_000),
    ("Cappuccino", 150_000),
    ("Mochaccino", 200_000),
)
LIVE_NAMES = tuple(name for name, _ in LIVE)


def test_the_showcase_fills_the_store_exactly() -> None:
    """4 live + 16 adds = 20, the measured cap. One more would revert with 6008."""
    assert len(SHOWCASE) == 16
    assert len(end_state(LIVE)) == MAX_PRODUCTS


def test_every_pick_resolves_against_the_catalogue() -> None:
    """Names and prices come from the catalogue, so the two cannot disagree."""
    items = showcase_items()
    assert len(items) == len(SHOWCASE)
    for pick, item in zip(SHOWCASE, items):
        assert item is BY_ID[pick.item_id]


def test_no_pick_collides_with_a_live_name() -> None:
    """A byte-exact collision reverts (6001) mid-sequence — the selection avoids all 16."""
    assert len(to_add(LIVE_NAMES)) == 16


def test_a_collision_is_skipped_rather_than_reverting() -> None:
    """Cappuccino is byte-identical to the live one, so it is never offered as an add."""
    assert "Cappuccino" not in {item.name for item in showcase_items()}
    with_cappuccino = to_add(("Espresso",))
    assert all(item.name != "Cappuccino" for item in with_cappuccino)


def test_name_matching_is_byte_exact_not_case_folded() -> None:
    """MEASURED on a fork: 'Sparkling water' and 'Sparkling Water' coexist as two products.

    So a live name that differs only in case must NOT suppress an add — the guard here is
    against pretending the program is more forgiving than it is.
    """
    assert to_add(("still water",)) == to_add(())


def test_add_only_never_reprices_a_live_product() -> None:
    """The live Cappuccino keeps its own price; the catalogue's 0.160 is not applied."""
    priced = dict(end_state(LIVE))
    assert priced["Cappuccino"] == 150_000
    assert BY_ID["cappuccino"].price_lamports == 160_000


def test_the_cap_is_enforced_rather_than_discovered_on_chain() -> None:
    """A store with no free slots refuses locally instead of stranding itself mid-run."""
    full = tuple((f"Item{n:02d}", 1_000) for n in range(MAX_PRODUCTS))
    with pytest.raises(ValueError, match="VectorLimitReached"):
        end_state(full)
