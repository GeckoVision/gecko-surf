"""The semantic catalogue, serialised so a non-Python consumer can DERIVE it.

`.claude/rules/python.md` requires one canonical module that every consumer imports from,
never a redeclaration. A surface written in another language cannot import
:mod:`gecko.semantic_catalogue`, and the failure that follows is not hypothetical: the
gecko-app playground carried its own 31-item menu (Doppio, Ristretto, Nitro — none of
which exist here) and its own name-based partitioning in which "black coffee" included
iced drinks. `is_hot_black_coffee` excludes iced BY PREDICATE, so the page could show a
verdict :mod:`gecko.semantic_grader` would mark differently — internally consistent, and
uncheckable against the thing that decides.

This export is how such a consumer obeys the rule: it derives, it does not retype.

**Categories are emitted as RESOLVED member lists, never as predicates to reimplement.**
That is the load-bearing decision. A consumer intersects a member list with whatever a
live store actually lists; it never reproduces `is_hot_black_coffee` and gets iced wrong,
and it never reproduces `is_plain_water` and lets Espresso Tonic count as water.

**The flags ride along** because they change what a correct answer looks like.
``ambiguous_without_intent`` on ``decaf-espresso`` is why a resolver must surface the decaf
question rather than silently hand someone a decaf, and ``is_plain_water`` is an attribute
precisely so Tonic Water and Coconut Water cannot qualify on their names.

Regenerate with ``uv run python -m gecko.catalogue_export``. A committed copy that drifts
is worse than none — the consumer keeps rendering a menu the grader no longer agrees with
— so ``tests/test_catalogue_export.py`` fails when the two disagree.

**Control plane.** Menu structure only: ids, display names, attributes, prices in the
catalogue's own units. No account, no address, no balance, no key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .semantic_catalogue import CATALOGUE, MenuItem, category_members

__all__ = ["CATEGORIES", "EXPORT_PATH", "build_export", "main"]

#: The categories a consumer is expected to partition by. Emitted resolved; adding one here
#: is the ONLY sanctioned way for a consumer to gain a new partition, because the
#: alternative is that it invents the predicate itself.
CATEGORIES = ("hot_black_coffee", "milk_drink", "plain_water")

#: A stable path, so a consumer can fetch or vendor one file rather than track a function.
EXPORT_PATH = Path(__file__).with_name("semantic_catalogue.export.json")


def _row(item: MenuItem) -> dict[str, Any]:
    return {
        "id": item.item_id,
        "display": item.name,
        "contains_coffee": item.contains_coffee,
        "temp": item.temperature,
        "milk": item.milk,
        "sweetened": item.sweetened,
        "decaf": item.decaf,
        "price_lamports": item.price_lamports,
        # Flags are promoted to named booleans rather than shipped as an opaque tuple: a
        # consumer that has to parse a flag list is one refactor away from missing one,
        # and both of these change what a correct answer is.
        "is_plain_water": "is_plain_water" in item.flags,
        "ambiguous_without_intent": "ambiguous_without_intent" in item.flags,
    }


def build_export() -> dict[str, Any]:
    """The full export as data. Deterministic — catalogue order throughout, no timestamp,
    so the committed file only changes when the catalogue does."""
    return {
        "catalogue": [_row(item) for item in CATALOGUE],
        "categories": {
            name: [item.item_id for item in category_members(name)]
            for name in CATEGORIES
        },
        "about": {
            "source": "gecko.semantic_catalogue",
            "regenerate": "uv run python -m gecko.catalogue_export",
            "categories_are": (
                "RESOLVED member lists, not predicates. Intersect a list with what the "
                "live store actually lists; do not reimplement the predicate. "
                "hot_black_coffee excludes iced drinks by definition — Cold Brew and "
                "Iced Americano are black and are coffee, and are not members."
            ),
            "flags_are": (
                "load-bearing. `ambiguous_without_intent` means a resolver must surface "
                "the ambiguity rather than pick; `is_plain_water` is an attribute, so "
                "Tonic Water, Espresso Tonic and Coconut Water are not water."
            ),
            "carries_no": "accounts, addresses, balances, keys",
        },
    }


def main() -> int:
    EXPORT_PATH.write_text(
        json.dumps(build_export(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {EXPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
