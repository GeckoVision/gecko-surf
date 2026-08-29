"""The evaluator's contract: two populations, never one, and an injectable arm.

Offline against the frozen snapshot — no RPC, no chain.
"""

from __future__ import annotations

from gecko.purchase_intent_eval import evaluate, load_menu, load_rows, substring_arm


def test_populations_are_separate() -> None:
    """A correct refusal is not a recall hit. Summing them would let an honest 'no'
    cancel a retrieval miss, which is the error the whole set exists to prevent."""
    r = evaluate()
    assert r.positives.n == 9, "9 positive rows"
    assert r.refusals[1] == 3, "3 out-of-scope rows, on their own denominator"
    assert r.positives.n + r.refusals[1] == len(load_rows())


def test_paraphrase_is_not_zero_on_a_measured_set() -> None:
    """The number the enforced-overlap fixture could never show.

    `tests/test_golden_set.py` requires its paraphrase archetype to share NO token with
    its target, so a lexical arm scores 0.00 there by arithmetic. On a set whose overlap
    was measured instead, the same class of query is mostly reachable. If this ever drops
    to zero, either the arm broke or someone re-introduced the tautology.
    """
    assert evaluate().paraphrase.recall_at[3] > 0.0


def test_the_arm_is_injectable_and_the_denominators_do_not_move() -> None:
    """Two arms must be comparable by construction: same rows, same populations."""
    blind = evaluate(arm=lambda goal, menu: [])
    base = evaluate()
    assert blind.positives.n == base.positives.n
    assert blind.positives.recall_at[3] == 0.0
    # An arm that returns nothing refuses everything, including the rows it should have
    # answered — which is why refusal rate is never quoted without recall beside it.
    assert blind.refusals == (3, 3)


def test_substring_arm_prefers_the_more_specific_name() -> None:
    """'sparkling water' must not lose to 'Water' on a shared token."""
    hits = substring_arm("I'd like a sparkling water", load_menu())
    assert hits and hits[0][1] == "Sparkling water"


def test_every_expected_product_exists_on_the_snapshot() -> None:
    menu = load_menu()
    known = {p for products in menu.values() for p in products}
    for row in load_rows():
        assert set(row.expect_products) <= known, row.goal


def test_out_of_scope_rows_expect_nothing() -> None:
    for row in load_rows():
        if row.archetype == "out_of_scope":
            assert not row.expect_products and row.expect_plan == "refuse"
