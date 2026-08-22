"""The catalogue, serialised so another surface can DERIVE it instead of retyping it.

The gecko-app playground had invented its own 31-item menu (Doppio, Ristretto, Nitro —
none of which exist here) and its own name-based partitioning, in which "black coffee"
included iced drinks. The engine's `is_hot_black_coffee` excludes iced BY PREDICATE, so
the playground could show a verdict the grader would mark differently: internally
consistent, and not checkable against the thing that decides.

`.claude/rules/python.md`: one canonical module, every consumer imports from there, never
redeclare. A JSON export is how a non-Python consumer obeys that rule — it derives, it
does not retype.

The categories are emitted as RESOLVED member lists, not as predicates to reimplement.
That is the whole point: a consumer intersects a member list with what a live store
actually lists, and never has to reproduce `is_hot_black_coffee` and get iced wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko.catalogue_export import EXPORT_PATH, build_export
from gecko.semantic_catalogue import BY_ID, CATALOGUE, category_members


def test_every_item_is_exported() -> None:
    export = build_export()

    assert len(export["catalogue"]) == len(CATALOGUE)
    assert {row["id"] for row in export["catalogue"]} == set(BY_ID)


def test_the_categories_are_resolved_members_not_predicates() -> None:
    """A consumer must not have to reimplement the predicate — that is how the app got
    iced drinks into 'black coffee'."""
    export = build_export()

    for name in ("hot_black_coffee", "milk_drink", "plain_water"):
        assert export["categories"][name] == [
            item.item_id for item in category_members(name)
        ]


def test_iced_black_coffee_is_not_in_hot_black_coffee() -> None:
    """The exact divergence that started this. Cold Brew and Iced Americano ARE black and
    ARE coffee; they are not HOT, so the category excludes them."""
    export = build_export()
    members = set(export["categories"]["hot_black_coffee"])

    assert "cold-brew" not in members
    assert "iced-americano" not in members
    assert "espresso-single" in members


def test_the_decaf_ambiguity_flag_survives_the_export() -> None:
    """`decaf-espresso` is a member of hot_black_coffee AND carries
    `ambiguous_without_intent`. A consumer that renders the flag surfaces the decaf
    question on the first "black coffee" ask; one that drops it hands someone a decaf
    silently, which is the failure the flag exists to prevent."""
    export = build_export()
    row = next(r for r in export["catalogue"] if r["id"] == "decaf-espresso")

    assert row["ambiguous_without_intent"] is True
    assert "decaf-espresso" in export["categories"]["hot_black_coffee"]


def test_water_is_flagged_never_inferred_from_the_name() -> None:
    """Tonic Water, Espresso Tonic and Coconut Water all fail `is_plain_water` despite
    their names. A name-based consumer disagrees here; a flag-based one cannot."""
    export = build_export()
    water = set(export["categories"]["plain_water"])
    by_id = {r["id"]: r for r in export["catalogue"]}

    assert water
    for item_id in water:
        assert by_id[item_id]["is_plain_water"] is True
    for impostor in ("tonic-water", "espresso-tonic", "coconut-water"):
        if impostor in by_id:
            assert impostor not in water


def test_the_committed_export_matches_the_catalogue() -> None:
    """The drift guard, and the reason this file exists.

    A vendored copy that silently falls behind is worse than no copy: the consumer keeps
    rendering a menu the grader no longer agrees with. Change the catalogue without
    regenerating and this turns red.
    """
    if not EXPORT_PATH.exists():
        raise AssertionError(
            f"{EXPORT_PATH} is missing — regenerate with "
            "`uv run python -m gecko.catalogue_export`"
        )

    committed = json.loads(Path(EXPORT_PATH).read_text(encoding="utf-8"))
    assert committed == build_export(), (
        "the committed export has drifted from the catalogue — regenerate with "
        "`uv run python -m gecko.catalogue_export`"
    )
