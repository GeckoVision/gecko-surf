"""The purchase-intent set is frozen, structurally honest, and NOT scored like the others.

Three things this file guards, in order of how badly they would bite:

1. **The freeze.** The intents cannot be edited after someone has seen a result. The
   existing golden sets already do this; the reason is the same and it is not paranoia —
   a set you can tune after measuring is a set that reports what you hoped.

2. **The separation.** These rows carry TWO expectations, retrieval (`expect_products`,
   `expect_stores`) and execution (`expect_plan`). A row that fails should say which
   half broke: "could not find Mochaccino" and "found it and the instruction has no
   quantity field" are different bugs with different owners.

3. **The spread.** Overlap here is MEASURED, never constrained. The older sets enforce
   zero overlap on their paraphrase archetype — correct for exercising the empty-drop
   path, and the reason a lexical 0.00 there is arithmetic rather than evidence. If every
   positive row in THIS set also lands at zero, we rebuilt that fixture by hand, and the
   test below fails rather than letting the number be quoted.

Not wired into `evaluate.evaluate_golden`: that harness scores `AgentApiClient` ops and
these score (store, product) pairs. Same word, different unit — putting them in one table
is the error `gecko/retrieval_metrics.py` exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "golden"
TASKS = FIXTURES / "purchase_intents.jsonl"
SNAPSHOT = FIXTURES / "purchase_menu_snapshot.json"

#: Frozen 2026-08-29, BEFORE any ranker was run against this set.
FROZEN_SHA256 = "b28c286f0bc1f8a87063b36556a6967ecec9fad47d76440e05426c9f8e0489b9"

PLANS = {"build", "ask", "swap_then_build", "refuse"}
ARCHETYPES = {
    "keyword_echo",
    "paraphrase_natural",
    "near_dup_disambiguation",
    "out_of_scope",
}


def _rows() -> list[dict]:
    return [
        json.loads(x)
        for x in TASKS.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def test_frozen() -> None:
    digest = hashlib.sha256(TASKS.read_bytes()).hexdigest()
    assert digest == FROZEN_SHA256, (
        "the purchase intents changed. That is allowed, but not silently: update "
        "FROZEN_SHA256 in the SAME commit that changes the file, so the diff shows an "
        "intent was edited rather than a number improving on its own."
    )


def test_every_row_carries_both_expectations() -> None:
    for row in _rows():
        assert row["archetype"] in ARCHETYPES, row["goal"]
        assert row["expect_plan"] in PLANS, row["goal"]
        assert row["why"].strip(), f"no rationale: {row['goal']}"
        assert row["author"] in {"founder", "gecko"}, row["goal"]


def test_out_of_scope_expects_nothing_and_refuses() -> None:
    for row in _rows():
        if row["archetype"] == "out_of_scope":
            assert row["expect_products"] == [] and row["expect_stores"] == []
            assert row["expect_plan"] == "refuse", row["goal"]
        else:
            assert row["expect_products"], row["goal"]


def test_labels_name_products_that_exist_on_the_snapshot() -> None:
    menu = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    known = {p for products in menu.values() for p in products}
    for row in _rows():
        for product in row["expect_products"]:
            assert product in known, (
                f"{row['goal']!r} expects {product!r}, not on the menu"
            )
        for store in row["expect_stores"]:
            assert store in menu, f"{row['goal']!r} expects store {store!r}"


def test_the_set_has_a_spread_not_a_constraint() -> None:
    """The whole reason this set exists. A rebuilt zero-overlap fixture fails here."""
    from gecko.catalog import _tokens
    from gecko.lexnorm import fold_tokens

    menu = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    folded = [set(fold_tokens(set(_tokens(p)))) for ps in menu.values() for p in ps]
    spread = [
        max(
            (len(set(fold_tokens(set(_tokens(r["goal"])))) & t) for t in folded),
            default=0,
        )
        for r in _rows()
        if r["archetype"] != "out_of_scope"
    ]
    assert len(set(spread)) > 1, (
        f"no variance in overlap: {spread} — this measures one case"
    )
    assert spread.count(0) < len(spread), (
        "every positive row is at zero — the old fixture, by hand"
    )


def test_the_founder_wrote_some_of_them() -> None:
    """Author-coupling guard. Every intent written by whoever also read the menu inherits
    its vocabulary; rows from outside that context are the only ones that can surprise us."""
    assert sum(1 for r in _rows() if r["author"] == "founder") >= 5


@pytest.mark.parametrize("path", [TASKS, SNAPSHOT])
def test_readable(path: Path) -> None:
    assert path.exists() and path.stat().st_size > 0
