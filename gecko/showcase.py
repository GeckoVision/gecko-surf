"""The confusable showcase: which catalogue items go on the LIVE geckocoffee store.

A `let_me_buy` store holds at most **20 products** — a COUNT, measured on a fork against
a virgin store with zero purchases and all 3,681 bytes free, not a byte budget (see
`scripts/rehearse_store_update.py`). The catalogue carries 31. So the live store cannot be
the catalogue, and something has to be left off.

WHAT THIS MODULE IS. The selection, and the reason for each one, in the only place that
may state it. Names and prices are NOT retyped here — every item is an id resolved against
`semantic_catalogue`, so the showcase cannot silently disagree with the grader's catalogue
about what an item costs or what it is called.

ADD-ONLY, AND THAT IS THE WHOLE SAFETY PROPERTY. There is no edit instruction: a price
change is delete-then-add. A plan that deletes first can strand a live store with real
purchase history half-updated, and the only recovery is more deletes and adds. So the
showcase is expressed as items to ADD ALONGSIDE what is already there. A revert then costs
that one product and never the menu.

WHAT THE CHAIN CARRIES, and it decides the selection. A product on chain is `name`,
`price`, `mint`, `decimals` — the typed attributes that make category membership derivable
live in `semantic_catalogue` and reach an agent through the comprehended surface. A
browsing agent therefore sees NAMES AND PRICES ONLY, so items are chosen for the trap
their NAME carries, not the trap their attributes carry.

Control plane only: item ids, names and prices are the store's public menu.
"""

from __future__ import annotations

from dataclasses import dataclass

from .semantic_catalogue import BY_ID, MenuItem, UnknownItemError

__all__ = [
    "MAX_PRODUCTS",
    "SHOWCASE",
    "ShowcasePick",
    "end_state",
    "showcase_items",
    "to_add",
]

#: MEASURED 2026-08-21 on a fork: `add_product` refuses the 21st with
#: `VectorLimitReached` (6008) even on a store with no purchases and every byte free.
MAX_PRODUCTS = 20


@dataclass(frozen=True)
class ShowcasePick:
    """One chosen item and why it earned a slot. `why` is the trap, not a description."""

    item_id: str
    trap: str
    why: str


#: The sixteen. Chosen against two mechanical filters before any judgment: an item whose
#: attribute signature is shared by a survivor must earn its slot on its NAME alone, and
#: the three items the spec's own §1 table leaves without a stated rationale (Latte,
#: Cortado, Hot Chocolate) are filler by the spec's own account.
SHOWCASE: tuple[ShowcasePick, ...] = (
    ShowcasePick(
        "brewed-coffee",
        "default",
        "the house default for an unqualified 'black coffee'",
    ),
    ShowcasePick(
        "espresso-double",
        "cardinality",
        "foil to the live Espresso — the parenthetical is the trap",
    ),
    ShowcasePick(
        "decaf-espresso",
        "ambiguity",
        "the only in-category item flagged ambiguous_without_intent",
    ),
    ShowcasePick(
        "oat-cappuccino",
        "conditional",
        "arms the oat conditional; 3x breaks the budget where dairy fits",
    ),
    ShowcasePick(
        "espresso-macchiato",
        "referent",
        "one-word flip against Latte Macchiato — only works as a pair",
    ),
    ShowcasePick("latte-macchiato", "referent", "the other half of the flip"),
    ShowcasePick(
        "mocha", "prohibition", "the sweetened prohibition, beside the live Mochaccino"
    ),
    ShowcasePick(
        "dirty-chai",
        "inversion",
        "coffee hiding in a chai name — the inverse of Chai Latte",
    ),
    ShowcasePick("chai-latte", "name-lies", "'latte' with zero coffee, sweetened"),
    ShowcasePick(
        "matcha-latte",
        "name-lies",
        "'latte', zero coffee, unsweetened — splits the word from both",
    ),
    ShowcasePick(
        "babyccino", "name-lies", "'-ccino' with no coffee; the suffix is the attack"
    ),
    ShowcasePick(
        "cold-brew", "temperature", "black coffee excluded on temperature alone"
    ),
    ShowcasePick(
        "still-water",
        "default",
        "the water default, named by scenario 1 — absent from the live store",
    ),
    ShowcasePick(
        "tonic-water", "flag-test", "'water' excluded ONLY by the plain-water flag"
    ),
    ShowcasePick(
        "coconut-water", "flag-test", "same flag path plus ambiguous_without_intent"
    ),
    ShowcasePick(
        "espresso-tonic", "name-lies", "contains water and espresso; is neither"
    ),
)


def showcase_items() -> tuple[MenuItem, ...]:
    """The picks resolved against the catalogue — the single source of names and prices."""
    resolved = []
    for pick in SHOWCASE:
        item = BY_ID.get(pick.item_id)
        if item is None:
            raise UnknownItemError(
                f"showcase names {pick.item_id!r}, which the catalogue does not carry"
            )
        resolved.append(item)
    return tuple(resolved)


def to_add(live_names: tuple[str, ...]) -> tuple[MenuItem, ...]:
    """The showcase items not already on the store, BY BYTE-EXACT NAME.

    Name matching in the program is byte-exact and case-sensitive — measured, not assumed:
    'Sparkling water' and 'Sparkling Water' coexist as two products. So this only skips a
    true collision (which would revert with `ProductAlreadyExists`, 6001). A near-twin that
    would merely be confusing is the selection's problem, not this function's, and the
    selection excludes those deliberately.
    """
    live = set(live_names)
    return tuple(item for item in showcase_items() if item.name not in live)


def end_state(live: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    """`(name, price_lamports)` for the store AFTER the add-only sequence.

    The live entries are carried through UNCHANGED — add-only never re-prices what is
    already there, which is why the live Cappuccino stays at its own price rather than the
    catalogue's.
    """
    result = list(live)
    live_names = tuple(name for name, _ in live)
    result.extend((item.name, item.price_lamports) for item in to_add(live_names))
    if len(result) > MAX_PRODUCTS:
        raise ValueError(
            f"end state is {len(result)} products; the program's cap is {MAX_PRODUCTS} "
            "and add_product would revert with VectorLimitReached (6008)"
        )
    return tuple(result)
