"""The confusable storefront catalogue — names that lie, attributes that don't.

Spec: docs/specs/2026-08-20-semantic-scenarios.md. The rule this module enforces:
**category membership is derivable from typed attributes only, never from names.**
The item names are chosen to punish keyword matching (Chai Latte contains no
coffee; Espresso Tonic contains water and is not one; Cold Brew is black but not
hot). Category sets are COMPUTED from attributes — they are never stored, so there
is exactly one source of truth for membership.

The catalogue doubles as the seed for the hosted geckocoffee LetMeBuy store:
:func:`to_store_config` emits the store shape with attributes riding in item
metadata. Mints are assigned at seeding time by the store, not invented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Temperature = Literal["hot", "iced", "none"]
Milk = Literal["none", "dairy", "oat"]


@dataclass(frozen=True)
class MenuItem:
    item_id: str
    name: str
    contains_coffee: bool
    temperature: Temperature
    milk: Milk
    sweetened: bool
    decaf: bool
    price_lamports: int
    flags: tuple[str, ...] = field(default=())


def _item(
    item_id: str,
    name: str,
    *,
    coffee: bool,
    temp: Temperature,
    milk: Milk,
    sweet: bool = False,
    decaf: bool = False,
    price: int,
    flags: tuple[str, ...] = (),
) -> MenuItem:
    return MenuItem(
        item_id=item_id,
        name=name,
        contains_coffee=coffee,
        temperature=temp,
        milk=milk,
        sweetened=sweet,
        decaf=decaf,
        price_lamports=price,
        flags=flags,
    )


# The 31 items of the spec's §1 table, verbatim.
CATALOGUE: tuple[MenuItem, ...] = (
    _item(
        "brewed-coffee",
        "Brewed Coffee (drip)",
        coffee=True,
        temp="hot",
        milk="none",
        price=3_000_000,
    ),
    _item(
        "americano",
        "Caffè Americano",
        coffee=True,
        temp="hot",
        milk="none",
        price=3_500_000,
    ),
    _item(
        "espresso-single",
        "Espresso (single)",
        coffee=True,
        temp="hot",
        milk="none",
        price=2_500_000,
    ),
    _item(
        "espresso-double",
        "Espresso (double)",
        coffee=True,
        temp="hot",
        milk="none",
        price=3_200_000,
    ),
    _item(
        "long-black",
        "Long Black",
        coffee=True,
        temp="hot",
        milk="none",
        price=3_500_000,
    ),
    _item("red-eye", "Red Eye", coffee=True, temp="hot", milk="none", price=4_200_000),
    _item(
        "decaf-espresso",
        "Decaf Espresso",
        coffee=True,
        temp="hot",
        milk="none",
        decaf=True,
        price=2_500_000,
        flags=("ambiguous_without_intent",),
    ),
    _item(
        "cappuccino",
        "Cappuccino",
        coffee=True,
        temp="hot",
        milk="dairy",
        price=4_000_000,
    ),
    _item(
        "oat-cappuccino",
        "Oat Cappuccino",
        coffee=True,
        temp="hot",
        milk="oat",
        price=6_000_000,
    ),
    _item("latte", "Latte", coffee=True, temp="hot", milk="dairy", price=4_200_000),
    _item(
        "oat-latte", "Oat Latte", coffee=True, temp="hot", milk="oat", price=6_200_000
    ),
    _item(
        "flat-white",
        "Flat White",
        coffee=True,
        temp="hot",
        milk="dairy",
        price=4_300_000,
    ),
    _item("cortado", "Cortado", coffee=True, temp="hot", milk="dairy", price=4_000_000),
    _item(
        "espresso-macchiato",
        "Espresso Macchiato",
        coffee=True,
        temp="hot",
        milk="dairy",
        price=3_000_000,
    ),
    _item(
        "latte-macchiato",
        "Latte Macchiato",
        coffee=True,
        temp="hot",
        milk="dairy",
        price=4_400_000,
    ),
    _item(
        "mocha",
        "Mocha",
        coffee=True,
        temp="hot",
        milk="dairy",
        sweet=True,
        price=4_800_000,
    ),
    _item(
        "sweet-iced-latte",
        "Sweetened Iced Latte",
        coffee=True,
        temp="iced",
        milk="dairy",
        sweet=True,
        price=4_600_000,
    ),
    _item(
        "iced-americano",
        "Iced Americano",
        coffee=True,
        temp="iced",
        milk="none",
        price=3_800_000,
    ),
    _item(
        "cold-brew", "Cold Brew", coffee=True, temp="iced", milk="none", price=4_500_000
    ),
    _item(
        "affogato",
        "Affogato",
        coffee=True,
        temp="iced",
        milk="dairy",
        sweet=True,
        price=5_500_000,
    ),
    _item(
        "dirty-chai",
        "Dirty Chai",
        coffee=True,
        temp="hot",
        milk="dairy",
        sweet=True,
        price=5_000_000,
    ),
    _item(
        "chai-latte",
        "Chai Latte",
        coffee=False,
        temp="hot",
        milk="dairy",
        sweet=True,
        price=4_500_000,
    ),
    _item(
        "matcha-latte",
        "Matcha Latte",
        coffee=False,
        temp="hot",
        milk="dairy",
        price=5_000_000,
    ),
    _item(
        "turmeric-latte",
        "Turmeric Latte",
        coffee=False,
        temp="hot",
        milk="dairy",
        price=4_800_000,
    ),
    _item(
        "babyccino",
        "Babyccino",
        coffee=False,
        temp="hot",
        milk="dairy",
        price=1_500_000,
    ),
    _item(
        "hot-chocolate",
        "Hot Chocolate",
        coffee=False,
        temp="hot",
        milk="dairy",
        sweet=True,
        price=4_000_000,
    ),
    _item(
        "still-water",
        "Still Water",
        coffee=False,
        temp="none",
        milk="none",
        price=1_500_000,
        flags=("is_plain_water",),
    ),
    _item(
        "sparkling-water",
        "Sparkling Water",
        coffee=False,
        temp="none",
        milk="none",
        price=2_000_000,
        flags=("is_plain_water",),
    ),
    _item(
        "tonic-water",
        "Tonic Water",
        coffee=False,
        temp="none",
        milk="none",
        sweet=True,
        price=2_500_000,
    ),
    _item(
        "coconut-water",
        "Coconut Water",
        coffee=False,
        temp="none",
        milk="none",
        sweet=True,
        price=3_500_000,
        flags=("ambiguous_without_intent",),
    ),
    _item(
        "espresso-tonic",
        "Espresso Tonic",
        coffee=True,
        temp="iced",
        milk="none",
        sweet=True,
        price=5_200_000,
    ),
)

BY_ID: dict[str, MenuItem] = {item.item_id: item for item in CATALOGUE}

# The house defaults an unqualified request resolves to (spec §1 category notes).
BLACK_COFFEE_DEFAULT = "brewed-coffee"
WATER_DEFAULT = "still-water"


class UnknownItemError(Exception):
    """Raised for an item_id the catalogue does not carry — fail loudly, not None."""


def get_item(item_id: str) -> MenuItem:
    item = BY_ID.get(item_id)
    if item is None:
        raise UnknownItemError(f"unknown item_id {item_id!r}; valid: {sorted(BY_ID)}")
    return item


def is_hot_black_coffee(item: MenuItem) -> bool:
    """hot black coffee = coffee AND hot AND no milk AND not sweetened.

    Note what is absent: any name check. ``decaf-espresso`` IS a member — it
    carries ``ambiguous_without_intent`` so a resolver must surface it rather
    than silently hand a decaf to someone asking for coffee.
    """
    return (
        item.contains_coffee
        and item.temperature == "hot"
        and item.milk == "none"
        and not item.sweetened
    )


def is_plain_water(item: MenuItem) -> bool:
    """water = not coffee, no milk, and the explicit plain-water flag.

    The flag is required on purpose: Tonic Water and Espresso Tonic both fail
    here, and Coconut Water fails despite the name — "a water" does not mean it.
    """
    return (
        not item.contains_coffee
        and item.milk == "none"
        and "is_plain_water" in item.flags
    )


def is_milk_drink(item: MenuItem) -> bool:
    """A coffee drink with milk — the set scenario 2's oat conditional binds to."""
    return item.contains_coffee and item.milk != "none"


def category_members(category: str) -> tuple[MenuItem, ...]:
    predicate = {
        "hot_black_coffee": is_hot_black_coffee,
        "plain_water": is_plain_water,
        "milk_drink": is_milk_drink,
    }.get(category)
    if predicate is None:
        raise UnknownItemError(f"unknown category {category!r}")
    return tuple(item for item in CATALOGUE if predicate(item))


def to_store_config(store_name: str = "geckocoffee") -> dict[str, object]:
    """The LetMeBuy-shaped seed for the hosted store.

    Attributes ride in item metadata so the hosted surface carries the same
    single source of truth the grader uses. Mints are assigned by the store at
    seeding time — this config deliberately has no opinion about them.
    """
    return {
        "store": store_name,
        "items": [
            {
                "item_id": item.item_id,
                "name": item.name,
                "price_lamports": item.price_lamports,
                "metadata": {
                    "contains_coffee": item.contains_coffee,
                    "temperature": item.temperature,
                    "milk": item.milk,
                    "sweetened": item.sweetened,
                    "decaf": item.decaf,
                    "flags": list(item.flags),
                },
            }
            for item in CATALOGUE
        ],
    }
